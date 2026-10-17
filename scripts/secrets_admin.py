#!/usr/bin/env python3
"""Host commands and declarative resources for Odoo database credential rotation."""

import argparse
import base64
import contextlib
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time
import urllib.parse

import yaml

import openbao as b
from database import primary
from secrets_platform import apply, login, resources, token

spec = importlib.util.spec_from_file_location(
    'secret_worker', b.ROOT / 'infrastructure/secrets/worker.py'
)
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)
CONTROL = 'odoo_secrets_control'


def sql(query, database='postgres', cluster='odoo-ha'):
    return (
        b.k(
            '-n',
            'postgres-ha',
            'exec',
            '-i',
            primary(cluster),
            '-c',
            'database',
            '--',
            'psql',
            '-X',
            '-v',
            'ON_ERROR_STOP=1',
            '-U',
            'postgres',
            '-d',
            database,
            '-At',
            data=query.encode(),
        )
        .decode()
        .strip()
    )


def bootstrap_control(
    cluster='odoo-ha', application_database='odoo_dev', namespace='odoo-dev', ttl=86400, lead=900
):
    if not re.fullmatch('[a-z][a-z0-9_]+', application_database) or not re.fullmatch(
        '[a-z][a-z0-9-]+', namespace
    ):
        raise ValueError('Invalid isolated database or namespace')
    if cluster == 'odoo-ha' and (ttl != 86400 or lead != 900 or application_database != 'odoo_dev'):
        raise ValueError('Short TTL or alternate database is forbidden on the live cluster')
    private = b.PRIVATE / (
        'control.json' if cluster == 'odoo-ha' else 'control-' + cluster + '.json'
    )
    if not private.exists():
        if (
            sql(
                "SELECT count(*) FROM pg_roles WHERE rolname='odoo_secret_rotator'", cluster=cluster
            )
            != '0'
        ):
            raise RuntimeError('Restore the original control credentials instead of resetting them')
        b.private_write(
            private, json.dumps({name: secrets.token_urlsafe(36) for name in ('rotator', 'guard')})
        )
    passwords = json.loads(private.read_text())
    for name, limit in [('rotator', 4), ('guard', 2)]:
        role = 'odoo_secret_' + name
        if (
            sql("SELECT count(*) FROM pg_roles WHERE rolname='" + role + "'", cluster=cluster)
            == '0'
        ):
            verifier = w.scram_verifier(passwords[name])
            sql(
                f"CREATE ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT {limit} PASSWORD '{verifier}'",
                cluster=cluster,
            )
        else:
            sql(f'ALTER ROLE {role} CONNECTION LIMIT {limit}', cluster=cluster)
        if name == 'rotator':
            # The bounded issuance call carries a SCRAM verifier. Keep it out
            # of PostgreSQL statement/error context logs, including failures.
            sql(
                f"ALTER ROLE {role} SET log_statement='none'; "
                f"ALTER ROLE {role} SET log_min_error_statement='panic'; "
                f"ALTER ROLE {role} SET log_error_verbosity='terse'; "
                f'ALTER ROLE {role} SET log_parameter_max_length_on_error=0',
                cluster=cluster,
            )
    if (
        sql("SELECT count(*) FROM pg_database WHERE datname='" + CONTROL + "'", cluster=cluster)
        == '0'
    ):
        sql('CREATE DATABASE ' + CONTROL + ' OWNER postgres', cluster=cluster)
    sql(
        'REVOKE CONNECT ON DATABASE '
        + CONTROL
        + ' FROM PUBLIC; GRANT CONNECT ON DATABASE '
        + CONTROL
        + ' TO odoo_secret_rotator,odoo_secret_guard;',
        cluster=cluster,
    )
    source = (b.ROOT / 'infrastructure/secrets/bootstrap.sql').read_text()
    sql(
        source.replace('APP_DATABASE', application_database).replace('APP_NAMESPACE', namespace),
        CONTROL,
        cluster,
    )
    if cluster != 'odoo-ha':
        sql(
            f'UPDATE secret_control.configuration SET ttl_seconds={ttl},lead_seconds={lead}',
            CONTROL,
            cluster,
        )
    ca = base64.b64decode(
        b.obj('postgres-ha', 'secret', cluster + '-cluster-cert')['data']['ca.crt']
    )
    if cluster == 'odoo-ha':
        b.private_write(b.PRIVATE / 'postgres-ca.crt', ca)
        b.ensure_secret('secrets-system', 'rotator-db', {'password': passwords['rotator']})
        b.ensure_secret('secrets-system', 'guard-db', {'password': passwords['guard']})
        b.ensure_secret('secrets-system', 'postgres-ca', {'ca.crt': ca})
        b.ensure_secret(
            'secrets-system', 'openbao-ca', {'ca.crt': (b.PRIVATE / 'ca.crt').read_bytes()}
        )
        np = {
            'apiVersion': 'networking.k8s.io/v1',
            'kind': 'NetworkPolicy',
            'metadata': {'name': 'allow-secret-workers', 'namespace': 'postgres-ha'},
            'spec': {
                'podSelector': {
                    'matchLabels': {'postgres-operator.crunchydata.com/cluster': 'odoo-ha'}
                },
                'policyTypes': ['Ingress'],
                'ingress': [
                    {
                        'from': [
                            {
                                'namespaceSelector': {
                                    'matchLabels': {'kubernetes.io/metadata.name': 'secrets-system'}
                                }
                            }
                        ],
                        'ports': [{'protocol': 'TCP', 'port': 5432}],
                    }
                ],
            },
        }
        apply(np)
        (b.ROOT / 'infrastructure/secrets/postgres-network.yaml').write_text(
            yaml.safe_dump(np, sort_keys=False)
        )
    return passwords


def settings(mode='controller'):
    return {
        'mode': mode,
        'database_host': 'odoo-ha-primary.postgres-ha.svc',
        'database_port': 5432,
        'control_database': CONTROL,
        'database_user': 'odoo_secret_' + ('guard' if mode == 'guard' else 'rotator'),
        'database_password_file': '/run/db/password',
        'database_ca': '/run/postgres-ca/ca.crt',
        'application_database': 'odoo_dev',
        'application_namespace': 'odoo-dev',
        'bao_address': 'https://openbao.openbao.svc:8200',
        'bao_ca': '/run/openbao-ca/ca.crt',
        'bao_role': 'odoo-rotator',
        'bao_path': 'kv/data/odoo-dev/odoo-db/',
    }


def eso_rbac():
    cert_rules = [
        {
            'apiGroups': ['apiextensions.k8s.io'],
            'resources': ['customresourcedefinitions'],
            'verbs': ['get', 'list', 'watch'],
        },
        {
            'apiGroups': ['apiextensions.k8s.io'],
            'resources': ['customresourcedefinitions'],
            'resourceNames': [
                'externalsecrets.external-secrets.io',
                'secretstores.external-secrets.io',
            ],
            'verbs': ['update', 'patch'],
        },
        {
            'apiGroups': ['admissionregistration.k8s.io'],
            'resources': ['validatingwebhookconfigurations'],
            'verbs': ['get', 'list', 'watch'],
        },
        {
            'apiGroups': ['admissionregistration.k8s.io'],
            'resources': ['validatingwebhookconfigurations'],
            'resourceNames': ['secretstore-validate', 'externalsecret-validate'],
            'verbs': ['update', 'patch'],
        },
        {'apiGroups': [''], 'resources': ['endpoints'], 'verbs': ['get', 'list', 'watch']},
        {
            'apiGroups': ['discovery.k8s.io'],
            'resources': ['endpointslices'],
            'verbs': ['get', 'list', 'watch'],
        },
    ]
    role = {
        'apiVersion': 'rbac.authorization.k8s.io/v1',
        'kind': 'ClusterRole',
        'metadata': {'name': 'odoo-eso-cert-controller'},
        'rules': cert_rules,
    }
    binding = {
        'apiVersion': role['apiVersion'],
        'kind': 'ClusterRoleBinding',
        'metadata': {'name': role['metadata']['name']},
        'roleRef': {
            'apiGroup': 'rbac.authorization.k8s.io',
            'kind': 'ClusterRole',
            'name': role['metadata']['name'],
        },
        'subjects': [
            {
                'kind': 'ServiceAccount',
                'name': 'external-secrets-cert-controller',
                'namespace': 'external-secrets',
            }
        ],
    }
    local = {
        'apiVersion': role['apiVersion'],
        'kind': 'Role',
        'metadata': {'name': 'odoo-eso-cert-local', 'namespace': 'external-secrets'},
        'rules': [
            {'apiGroups': [''], 'resources': ['secrets'], 'verbs': ['get', 'list', 'watch']},
            {
                'apiGroups': [''],
                'resources': ['secrets'],
                'resourceNames': ['external-secrets-webhook'],
                'verbs': ['update', 'patch'],
            },
            {'apiGroups': [''], 'resources': ['events'], 'verbs': ['create', 'patch']},
            {
                'apiGroups': ['coordination.k8s.io'],
                'resources': ['leases'],
                'verbs': ['get', 'create', 'update', 'patch'],
            },
        ],
    }
    local_binding = {
        'apiVersion': role['apiVersion'],
        'kind': 'RoleBinding',
        'metadata': {'name': local['metadata']['name'], 'namespace': 'external-secrets'},
        'roleRef': {
            'apiGroup': 'rbac.authorization.k8s.io',
            'kind': 'Role',
            'name': local['metadata']['name'],
        },
        'subjects': binding['subjects'],
    }
    auth = {
        'apiVersion': role['apiVersion'],
        'kind': 'Role',
        'metadata': {'name': 'eso-odoo-token', 'namespace': 'odoo-dev'},
        'rules': [
            {
                'apiGroups': [''],
                'resources': ['serviceaccounts/token'],
                'resourceNames': ['eso-odoo'],
                'verbs': ['create'],
            }
        ],
    }
    auth_binding = {
        'apiVersion': role['apiVersion'],
        'kind': 'RoleBinding',
        'metadata': auth['metadata'],
        'roleRef': {
            'apiGroup': 'rbac.authorization.k8s.io',
            'kind': 'Role',
            'name': 'eso-odoo-token',
        },
        'subjects': [
            {'kind': 'ServiceAccount', 'name': 'external-secrets', 'namespace': 'external-secrets'}
        ],
    }
    return [role, binding, local, local_binding, auth, auth_binding]


def runtime_resources():
    image = json.loads((b.ROOT / 'infrastructure/secrets/images.lock.json').read_text())['runtime'][
        'image'
    ]
    script = (b.ROOT / 'infrastructure/secrets/worker.py').read_text()
    documents = [
        {
            'apiVersion': 'v1',
            'kind': 'ConfigMap',
            'metadata': {'name': 'secret-worker', 'namespace': 'secrets-system'},
            'data': {
                'worker.py': script,
                'controller.json': json.dumps(settings()),
                'guard.json': json.dumps(settings('guard')),
            },
        }
    ]
    for mode, name, replicas in [
        ('controller', 'secret-rotator', 1),
        ('guard', 'secret-expiry-guard', 2),
    ]:
        labels = {'app.kubernetes.io/name': name, 'odoo.io/secrets-worker': mode}
        documents.append(
            {
                'apiVersion': 'v1',
                'kind': 'ServiceAccount',
                'metadata': {'name': name, 'namespace': 'secrets-system'},
                'automountServiceAccountToken': False,
            }
        )
        volumes = [
            {
                'name': 'code',
                'configMap': {
                    'name': 'secret-worker',
                    'items': [
                        {'key': 'worker.py', 'path': 'worker.py'},
                        {'key': mode + '.json', 'path': 'settings.json'},
                    ],
                },
            },
            {
                'name': 'db',
                'secret': {
                    'secretName': 'guard-db' if mode == 'guard' else 'rotator-db',
                    'defaultMode': 0o440,
                },
            },
            {'name': 'postgres-ca', 'secret': {'secretName': 'postgres-ca', 'defaultMode': 0o444}},
            {'name': 'tmp', 'emptyDir': {}},
        ]
        mounts = [
            {'name': 'code', 'mountPath': '/opt/odoo-secrets', 'readOnly': True},
            {'name': 'db', 'mountPath': '/run/db', 'readOnly': True},
            {'name': 'postgres-ca', 'mountPath': '/run/postgres-ca', 'readOnly': True},
            {'name': 'tmp', 'mountPath': '/tmp'},
        ]
        if mode == 'controller':
            volumes += [
                {
                    'name': 'openbao-ca',
                    'secret': {'secretName': 'openbao-ca', 'defaultMode': 0o444},
                },
                {
                    'name': 'bao-auth',
                    'projected': {
                        'sources': [
                            {
                                'serviceAccountToken': {
                                    'path': 'token',
                                    'audience': 'openbao',
                                    'expirationSeconds': 900,
                                }
                            }
                        ]
                    },
                },
                {
                    'name': 'api',
                    'projected': {
                        'sources': [
                            {'serviceAccountToken': {'path': 'token', 'expirationSeconds': 900}},
                            {
                                'configMap': {
                                    'name': 'kube-root-ca.crt',
                                    'items': [{'key': 'ca.crt', 'path': 'ca.crt'}],
                                }
                            },
                        ]
                    },
                },
            ]
            mounts += [
                {'name': 'openbao-ca', 'mountPath': '/run/openbao-ca', 'readOnly': True},
                {'name': 'bao-auth', 'mountPath': '/run/bao-auth', 'readOnly': True},
                {'name': 'api', 'mountPath': '/run/api', 'readOnly': True},
            ]
        pod = {
            'serviceAccountName': name,
            'automountServiceAccountToken': False,
            'securityContext': {
                'runAsUser': 1000,
                'runAsGroup': 1000,
                'runAsNonRoot': True,
                'fsGroup': 1000,
                'seccompProfile': {'type': 'RuntimeDefault'},
            },
            'terminationGracePeriodSeconds': 30,
            'affinity': {
                'nodeAffinity': {
                    'requiredDuringSchedulingIgnoredDuringExecution': {
                        'nodeSelectorTerms': [
                            {
                                'matchExpressions': [
                                    {
                                        'key': 'node-role.kubernetes.io/control-plane',
                                        'operator': 'DoesNotExist',
                                    }
                                ]
                            }
                        ]
                    }
                },
                'podAntiAffinity': {
                    'requiredDuringSchedulingIgnoredDuringExecution': [
                        {
                            'topologyKey': 'kubernetes.io/hostname',
                            'labelSelector': {'matchLabels': labels},
                        }
                    ]
                },
            },
            'containers': [
                {
                    'name': mode,
                    'image': image,
                    'imagePullPolicy': 'IfNotPresent',
                    'command': ['python3', '/opt/odoo-secrets/worker.py'],
                    'ports': [{'name': 'metrics', 'containerPort': 9117}],
                    'resources': resources('50m', '64Mi', '250m', '128Mi'),
                    'securityContext': {
                        'allowPrivilegeEscalation': False,
                        'readOnlyRootFilesystem': True,
                        'capabilities': {'drop': ['ALL']},
                    },
                    'volumeMounts': mounts,
                    'readinessProbe': {
                        'httpGet': {'path': '/ready', 'port': 'metrics'},
                        'periodSeconds': 2,
                        'timeoutSeconds': 5,
                    },
                    'livenessProbe': {
                        'tcpSocket': {'port': 'metrics'},
                        'periodSeconds': 10,
                        'failureThreshold': 6,
                    },
                }
            ],
            'volumes': volumes,
        }
        documents.append(
            {
                'apiVersion': 'apps/v1',
                'kind': 'Deployment',
                'metadata': {'name': name, 'namespace': 'secrets-system'},
                'spec': {
                    'replicas': replicas,
                    'strategy': {
                        'type': 'RollingUpdate',
                        'rollingUpdate': {'maxSurge': 0, 'maxUnavailable': 1},
                    },
                    'selector': {'matchLabels': labels},
                    'template': {
                        'metadata': {
                            'labels': labels,
                            'annotations': {
                                'checksum/code': __import__('hashlib')
                                .sha256(script.encode())
                                .hexdigest()
                            },
                        },
                        'spec': pod,
                    },
                },
            }
        )
        if mode == 'guard':
            documents.append(
                {
                    'apiVersion': 'policy/v1',
                    'kind': 'PodDisruptionBudget',
                    'metadata': {'name': name, 'namespace': 'secrets-system'},
                    'spec': {'minAvailable': 1, 'selector': {'matchLabels': labels}},
                }
            )
        documents.append(
            {
                'apiVersion': 'v1',
                'kind': 'Service',
                'metadata': {
                    'name': name,
                    'namespace': 'secrets-system',
                    'labels': {'odoo.io/secret-metrics': 'true'},
                },
                'spec': {
                    'selector': labels,
                    'ports': [{'name': 'metrics', 'port': 9117, 'targetPort': 'metrics'}],
                },
            }
        )
    role = {
        'apiVersion': 'rbac.authorization.k8s.io/v1',
        'kind': 'Role',
        'metadata': {'name': 'secret-rotation', 'namespace': 'odoo-dev'},
        'rules': [
            {
                'apiGroups': ['apps'],
                'resources': ['deployments'],
                'resourceNames': ['odoo', 'odoo-cron'],
                'verbs': ['get', 'patch'],
            },
            {'apiGroups': [''], 'resources': ['pods'], 'verbs': ['get', 'list']},
            {
                'apiGroups': [''],
                'resources': ['secrets'],
                'resourceNames': ['odoo-ha-database'],
                'verbs': ['get'],
            },
            {
                'apiGroups': ['external-secrets.io'],
                'resources': ['externalsecrets'],
                'resourceNames': ['odoo-ha-database'],
                'verbs': ['get', 'patch'],
            },
        ],
    }
    documents += [
        role,
        {
            'apiVersion': role['apiVersion'],
            'kind': 'RoleBinding',
            'metadata': role['metadata'],
            'roleRef': {
                'apiGroup': 'rbac.authorization.k8s.io',
                'kind': 'Role',
                'name': role['metadata']['name'],
            },
            'subjects': [
                {'kind': 'ServiceAccount', 'name': 'secret-rotator', 'namespace': 'secrets-system'}
            ],
        },
    ]
    documents.append(
        {
            'apiVersion': 'networking.k8s.io/v1',
            'kind': 'NetworkPolicy',
            'metadata': {'name': 'allow-credential-identity', 'namespace': 'odoo-dev'},
            'spec': {
                'podSelector': {'matchLabels': {'app.kubernetes.io/name': 'odoo'}},
                'policyTypes': ['Ingress'],
                'ingress': [
                    {
                        'from': [
                            {
                                'namespaceSelector': {
                                    'matchLabels': {'kubernetes.io/metadata.name': 'secrets-system'}
                                }
                            }
                        ],
                        'ports': [{'protocol': 'TCP', 'port': 9116}],
                    }
                ],
            },
        }
    )
    documents.append(
        {
            'apiVersion': 'monitoring.coreos.com/v1',
            'kind': 'ServiceMonitor',
            'metadata': {
                'name': 'secret-workers',
                'namespace': 'secrets-system',
                'labels': {'release': 'monitoring'},
            },
            'spec': {
                'selector': {'matchLabels': {'odoo.io/secret-metrics': 'true'}},
                'endpoints': [{'port': 'metrics', 'interval': '5s', 'scrapeTimeout': '4s'}],
            },
        }
    )
    documents.append(
        {
            'apiVersion': 'networking.k8s.io/v1',
            'kind': 'NetworkPolicy',
            'metadata': {'name': 'rotation-egress', 'namespace': 'secrets-system'},
            'spec': {
                'podSelector': {'matchLabels': {'odoo.io/secrets-worker': 'controller'}},
                'policyTypes': ['Egress'],
                'egress': [
                    {
                        'to': [
                            {
                                'namespaceSelector': {
                                    'matchLabels': {'kubernetes.io/metadata.name': 'openbao'}
                                }
                            }
                        ],
                        'ports': [{'port': 8200, 'protocol': 'TCP'}],
                    },
                    {
                        'to': [
                            {
                                'namespaceSelector': {
                                    'matchLabels': {'kubernetes.io/metadata.name': 'odoo-dev'}
                                }
                            }
                        ],
                        'ports': [{'port': 9116, 'protocol': 'TCP'}],
                    },
                ],
            },
        }
    )
    for document in documents:
        if document['kind'] in ('Role', 'RoleBinding'):
            document['metadata'].setdefault('annotations', {})['argocd.argoproj.io/hook'] = 'Skip'
    return documents


def external_secret_resources(
    namespace='odoo-dev',
    address='https://openbao.openbao.svc:8200',
    role='odoo-eso',
    path='odoo-dev/odoo-db/active',
):
    template = '{{ dict "username" .username "password" .password "generation" .generation "issued_at" .issued_at "expires_at" .expires_at | toJson }}'
    store = {
        'apiVersion': 'external-secrets.io/v1',
        'kind': 'SecretStore',
        'metadata': {'name': 'openbao-odoo', 'namespace': namespace},
        'spec': {
            'provider': {
                'vault': {
                    'server': address,
                    'path': 'kv',
                    'version': 'v2',
                    'caProvider': {'type': 'Secret', 'name': 'openbao-ca', 'key': 'ca.crt'},
                    'auth': {
                        'kubernetes': {
                            'mountPath': 'kubernetes',
                            'role': role,
                            'serviceAccountRef': {'name': 'eso-odoo', 'audiences': ['openbao']},
                        }
                    },
                }
            }
        },
    }
    external = {
        'apiVersion': 'external-secrets.io/v1',
        'kind': 'ExternalSecret',
        'metadata': {
            'name': 'odoo-ha-database',
            'namespace': namespace,
            'annotations': b.PROTECT
            if hasattr(b, 'PROTECT')
            else {'argocd.argoproj.io/sync-options': 'Prune=false,Delete=false'},
        },
        'spec': {
            'refreshPolicy': 'Periodic',
            'refreshInterval': '30s',
            'secretStoreRef': {'name': 'openbao-odoo', 'kind': 'SecretStore'},
            'target': {
                'name': 'odoo-ha-database',
                'creationPolicy': 'Orphan',
                'deletionPolicy': 'Retain',
                'template': {
                    'engineVersion': 'v2',
                    'mergePolicy': 'Merge',
                    'data': {'credentials.json': template},
                    'metadata': {
                        'annotations': {
                            'argocd.argoproj.io/sync-options': 'Prune=false,Delete=false'
                        }
                    },
                },
            },
            'data': [
                {'secretKey': key, 'remoteRef': {'key': path, 'property': key}}
                for key in ('username', 'password', 'generation', 'issued_at', 'expires_at')
            ],
        },
    }
    return [store, external]


def render_snapshot_job():
    image = json.loads((b.ROOT / 'infrastructure/secrets/images.lock.json').read_text())['runtime'][
        'image'
    ]
    script = (b.ROOT / 'infrastructure/secrets/snapshot.py').read_text()
    b.ensure_secret(
        'openbao', 'openbao-snapshot-key', {'key': (b.PRIVATE / 'snapshot.key').read_bytes()}
    )
    documents = [
        {
            'apiVersion': 'v1',
            'kind': 'ConfigMap',
            'metadata': {'name': 'openbao-snapshot-script', 'namespace': 'openbao'},
            'data': {'snapshot.py': script},
        },
        {
            'apiVersion': 'v1',
            'kind': 'PersistentVolumeClaim',
            'metadata': {
                'name': 'openbao-snapshots',
                'namespace': 'openbao',
                'annotations': {'argocd.argoproj.io/sync-options': 'Prune=false,Delete=false'},
            },
            'spec': {
                'accessModes': ['ReadWriteOnce'],
                'resources': {'requests': {'storage': '1Gi'}},
            },
        },
    ]
    pod = {
        'serviceAccountName': 'bao-bootstrap',
        'automountServiceAccountToken': False,
        'restartPolicy': 'Never',
        'securityContext': {
            'runAsUser': 1000,
            'runAsGroup': 1000,
            'runAsNonRoot': True,
            'fsGroup': 1000,
            'seccompProfile': {'type': 'RuntimeDefault'},
        },
        'containers': [
            {
                'name': 'snapshot',
                'image': image,
                'command': ['python3', '/opt/snapshot/snapshot.py'],
                'resources': resources('25m', '64Mi', '250m', '128Mi'),
                'securityContext': {
                    'allowPrivilegeEscalation': False,
                    'readOnlyRootFilesystem': True,
                    'capabilities': {'drop': ['ALL']},
                },
                'volumeMounts': [
                    {'name': name, 'mountPath': path, 'readOnly': name != 'backups'}
                    for name, path in [
                        ('script', '/opt/snapshot'),
                        ('auth', '/run/auth'),
                        ('ca', '/run/ca'),
                        ('key', '/run/key'),
                        ('backups', '/backups'),
                    ]
                ],
            }
        ],
        'volumes': [
            {'name': 'script', 'configMap': {'name': 'openbao-snapshot-script'}},
            {
                'name': 'auth',
                'projected': {
                    'sources': [
                        {
                            'serviceAccountToken': {
                                'path': 'token',
                                'audience': 'openbao',
                                'expirationSeconds': 900,
                            }
                        }
                    ]
                },
            },
            {'name': 'ca', 'secret': {'secretName': 'openbao-ca', 'defaultMode': 0o444}},
            {'name': 'key', 'secret': {'secretName': 'openbao-snapshot-key', 'defaultMode': 0o440}},
            {'name': 'backups', 'persistentVolumeClaim': {'claimName': 'openbao-snapshots'}},
        ],
    }
    documents.append(
        {
            'apiVersion': 'batch/v1',
            'kind': 'CronJob',
            'metadata': {'name': 'openbao-snapshot', 'namespace': 'openbao'},
            'spec': {
                'schedule': '15 3 * * *',
                'timeZone': 'Etc/UTC',
                'concurrencyPolicy': 'Forbid',
                'startingDeadlineSeconds': 3600,
                'successfulJobsHistoryLimit': 2,
                'failedJobsHistoryLimit': 2,
                'jobTemplate': {
                    'spec': {
                        'backoffLimit': 1,
                        'activeDeadlineSeconds': 120,
                        'template': {
                            'metadata': {'labels': {'odoo.io/openbao-snapshot': 'true'}},
                            'spec': pod,
                        },
                    }
                },
            },
        }
    )
    for document in documents:
        apply(document)
    if not b.exists('openbao', 'job', 'openbao-snapshot-bootstrap'):
        b.k(
            '-n',
            'openbao',
            'create',
            'job',
            '--from=cronjob/openbao-snapshot',
            'openbao-snapshot-bootstrap',
        )
    b.k(
        '-n',
        'openbao',
        'wait',
        '--for=condition=complete',
        'job/openbao-snapshot-bootstrap',
        '--timeout=150s',
        timeout=155,
    )
    for claim in json.loads(b.k('-n', 'openbao', 'get', 'pvc', '-o', 'json'))['items']:
        if claim['spec'].get('volumeName'):
            b.k(
                'patch',
                'pv',
                claim['spec']['volumeName'],
                '--type=merge',
                '-p',
                '{"spec":{"persistentVolumeReclaimPolicy":"Retain"}}',
            )
    text = yaml.safe_dump_all(documents, sort_keys=False)
    (b.ROOT / 'infrastructure/secrets/snapshot-job.yaml').write_text(text)
    directory = b.ROOT / 'gitops/platform/openbao'
    (directory / 'snapshot.yaml').write_text(text)
    (directory / 'templates/snapshot.yaml').write_text('{{ .Files.Get "snapshot.yaml" }}\n')
    return documents


def render_runtime():
    documents = runtime_resources()
    for d in documents:
        apply(d)
    path = b.ROOT / 'infrastructure/secrets/runtime.yaml'
    path.write_text(yaml.safe_dump_all(documents, sort_keys=False))
    b.k(
        '-n',
        'secrets-system',
        'rollout',
        'status',
        'deployment/secret-expiry-guard',
        '--timeout=120s',
        timeout=130,
    )
    b.k(
        '-n',
        'secrets-system',
        'rollout',
        'status',
        'deployment/secret-rotator',
        '--timeout=120s',
        timeout=130,
    )


def render_gitops():
    project = {
        'apiVersion': 'argoproj.io/v1alpha1',
        'kind': 'AppProject',
        'metadata': {'name': 'odoo-secrets', 'namespace': 'argocd'},
        'spec': {
            'sourceRepos': [
                'git://local-git.gitops-system.svc.cluster.local:9418/odoo-platform.git'
            ],
            'destinations': [
                {'server': 'https://kubernetes.default.svc', 'namespace': ns}
                for ns in (
                    'openbao',
                    'external-secrets',
                    'secrets-system',
                    'odoo-dev',
                    'postgres-ha',
                )
            ],
            'clusterResourceWhitelist': [
                {'group': group, 'kind': kind}
                for group, kind in (
                    ('rbac.authorization.k8s.io', 'ClusterRole'),
                    ('rbac.authorization.k8s.io', 'ClusterRoleBinding'),
                    ('apiextensions.k8s.io', 'CustomResourceDefinition'),
                    ('admissionregistration.k8s.io', 'ValidatingWebhookConfiguration'),
                )
            ],
            'namespaceResourceWhitelist': [
                {'group': group, 'kind': kind}
                for group, kind in (
                    ('', 'ConfigMap'),
                    ('', 'Service'),
                    ('', 'ServiceAccount'),
                    ('', 'PersistentVolumeClaim'),
                    ('apps', 'StatefulSet'),
                    ('apps', 'Deployment'),
                    ('rbac.authorization.k8s.io', 'Role'),
                    ('rbac.authorization.k8s.io', 'RoleBinding'),
                    ('networking.k8s.io', 'NetworkPolicy'),
                    ('policy', 'PodDisruptionBudget'),
                    ('monitoring.coreos.com', 'ServiceMonitor'),
                    ('monitoring.coreos.com', 'PodMonitor'),
                    ('cilium.io', 'CiliumNetworkPolicy'),
                    ('', 'Secret'),
                    ('batch', 'CronJob'),
                    ('batch', 'Job'),
                    ('external-secrets.io', 'SecretStore'),
                    ('external-secrets.io', 'ExternalSecret'),
                )
            ],
        },
    }
    apply(project)
    (b.ROOT / 'gitops/bootstrap/secrets-project.yaml').write_text(
        yaml.safe_dump(project, sort_keys=False)
    )
    directory = b.ROOT / 'gitops/platform/secrets-system'
    directory.mkdir(parents=True, exist_ok=True)
    documents = list(
        yaml.safe_load_all((b.ROOT / 'infrastructure/secrets/runtime.yaml').read_text())
    )
    (directory / 'runtime.yaml').write_text(
        yaml.safe_dump_all(
            [document for document in documents if document['kind'] not in ('Role', 'RoleBinding')],
            sort_keys=False,
        )
    )
    (directory / 'postgres-network.yaml').write_text(
        (b.ROOT / 'infrastructure/secrets/postgres-network.yaml').read_text()
    )
    (directory / 'api-policy.yaml').write_text(
        (b.ROOT / 'infrastructure/secrets/api-policy.yaml').read_text()
    )
    resources = ['runtime.yaml', 'postgres-network.yaml', 'api-policy.yaml']
    values = yaml.safe_load((b.ROOT / 'gitops/environments/dev/odoo.yaml').read_text())
    if values['database'].get('credentialsMode') == 'rotating':
        documents = external_secret_resources()
        documents.append(
            {
                'apiVersion': 'v1',
                'kind': 'ServiceAccount',
                'metadata': {'name': 'eso-odoo', 'namespace': 'odoo-dev'},
                'automountServiceAccountToken': False,
            }
        )
        (directory / 'application-credentials.yaml').write_text(
            yaml.safe_dump_all(documents, sort_keys=False)
        )
        resources.append('application-credentials.yaml')
    (directory / 'kustomization.yaml').write_text(
        yaml.safe_dump(
            {
                'apiVersion': 'kustomize.config.k8s.io/v1beta1',
                'kind': 'Kustomization',
                'resources': resources,
            },
            sort_keys=False,
        )
    )
    app = {
        'apiVersion': 'argoproj.io/v1alpha1',
        'kind': 'Application',
        'metadata': {'name': 'secrets-system', 'namespace': 'argocd'},
        'spec': {
            'project': 'odoo-secrets',
            'source': {
                'repoURL': 'git://local-git.gitops-system.svc.cluster.local:9418/odoo-platform.git',
                'targetRevision': 'main',
                'path': 'gitops/platform/secrets-system',
            },
            'destination': {
                'server': 'https://kubernetes.default.svc',
                'namespace': 'secrets-system',
            },
            'ignoreDifferences': [
                {
                    'group': 'external-secrets.io',
                    'kind': 'ExternalSecret',
                    'name': 'odoo-ha-database',
                    'namespace': 'odoo-dev',
                    'jqPathExpressions': ['.spec.data[].remoteRef.version'],
                    'jsonPointers': ['/metadata/annotations/force-sync'],
                }
            ],
            'syncPolicy': {'syncOptions': ['RespectIgnoreDifferences=true']},
        },
    }
    (b.ROOT / 'gitops/applications/secrets-system.yaml').write_text(
        yaml.safe_dump(app, sort_keys=False)
    )


class ForwardedConnection(w.psycopg2.extensions.connection):
    def close(self):
        try:
            super().close()
        finally:
            stack = getattr(self, 'forward_stack', None)
            if stack:
                stack.close()
                self.forward_stack = None


class LocalWorker(w.Worker):
    def login_connection(self, credential, database=None):
        stack = contextlib.ExitStack()
        try:
            address = stack.enter_context(
                b.forward(primary(self.settings.get('cluster', 'odoo-ha')), 'postgres-ha', 5432)
            )
            connection = w.psycopg2.connect(
                host=self.settings['database_host'],
                hostaddr='127.0.0.1',
                port=int(address.rsplit(':', 1)[1]),
                dbname=database or self.settings['application_database'],
                user=credential['username'],
                password=credential['password'],
                sslmode='verify-full',
                sslrootcert=self.settings['database_ca'],
                connect_timeout=5,
                connection_factory=ForwardedConnection,
            )
            connection.forward_stack = stack
            return connection
        except BaseException:
            stack.close()
            raise

    def identity(self, pod):
        if pod.get('status', {}).get('phase') != 'Running':
            return None
        try:
            code = "import json,urllib.request;print(urllib.request.urlopen('http://127.0.0.1:9116/identity',timeout=1).read().decode())"
            return json.loads(
                b.k(
                    '-n',
                    self.settings['application_namespace'],
                    'exec',
                    pod['metadata']['name'],
                    '-c',
                    'ha-exporter',
                    '--',
                    'python3',
                    '-c',
                    code,
                    timeout=8,
                )
            )
        except (RuntimeError, ValueError, subprocess.TimeoutExpired):
            return None

    def kube(self, path, method='GET', payload=None):
        parsed = urllib.parse.urlsplit(path)
        pieces = parsed.path.split('/')
        index = pieces.index('namespaces')
        namespace, kind = pieces[index + 1 : index + 3]
        name = pieces[index + 3] if len(pieces) > index + 3 else None
        if method == 'GET':
            if kind == 'secrets' and name and not b.exists(namespace, kind, name):
                return {}
            args = ['-n', namespace, 'get', kind]
            if name:
                args.append(name)
            args += ['-o', 'json']
            query = urllib.parse.parse_qs(parsed.query)
            if query.get('labelSelector'):
                args += ['-l', query['labelSelector'][0]]
            return json.loads(b.k(*args))
        if method == 'PATCH' and name:
            # stdin avoids exposing generated credential values in argv.
            return json.loads(
                b.k(
                    '-n',
                    namespace,
                    'patch',
                    kind,
                    name,
                    '--type=merge',
                    '--patch-file=/dev/stdin',
                    '-o',
                    'json',
                    data=json.dumps(payload).encode(),
                )
            )
        raise ValueError('Unsupported host Kubernetes request')

    def bao_jwt(self):
        return token('secret-rotator', self.settings.get('auth_namespace', 'secrets-system'))


@contextlib.contextmanager
def local_worker(cluster='odoo-ha', namespace='odoo-dev', bao_address=None, mode='controller'):
    with contextlib.ExitStack() as stack:
        db_address = stack.enter_context(b.forward(primary(cluster), 'postgres-ha', 5432))
        if bao_address is None:
            bao_address = stack.enter_context(b.forward())
        cfg = settings(mode)
        cfg.update(
            {
                'database_host': cluster + '-primary.postgres-ha.svc',
                'database_hostaddr': '127.0.0.1',
                'database_port': int(db_address.rsplit(':', 1)[1]),
                'application_namespace': namespace,
                'database_ca': str(
                    b.PRIVATE / ('postgres-ca.crt' if cluster == 'odoo-ha' else cluster + '-ca.crt')
                ),
                'bao_address': bao_address,
                'bao_ca': str(b.PRIVATE / 'ca.crt'),
            }
        )
        cfg['cluster'] = cluster
        if cluster != 'odoo-ha':
            cfg.update(auth_namespace=namespace, bao_role='odoo-test-rotator')
        control_file = b.PRIVATE / (
            'control.json' if cluster == 'odoo-ha' else 'control-' + cluster + '.json'
        )
        password = json.loads(control_file.read_text())['guard' if mode == 'guard' else 'rotator']
        transient = b.PRIVATE / ('client-password-' + secrets.token_hex(6))
        b.private_write(transient, password)
        cfg['database_password_file'] = str(transient)
        worker = LocalWorker(cfg)
        try:
            yield worker
        finally:
            worker.close()
            transient.unlink(missing_ok=True)


def status():
    with local_worker(bao_address='https://not-used.invalid') as worker:
        state = worker.status()
    return b.record('secrets-status', state=state)


def prepare_restore():
    for role in w.ROLES:
        sql(f"ALTER ROLE {role} IN DATABASE odoo_dev SET role='odoo'", database='postgres')
    with local_worker() as worker:
        credential = worker.issue()
        worker.publish(credential)
        for name in ('odoo', 'odoo-cron'):
            worker.kube(
                '/apis/apps/v1/namespaces/odoo-dev/deployments/' + name,
                'PATCH',
                {
                    'spec': {
                        'template': {
                            'metadata': {
                                'annotations': {
                                    'odoo.io/credential-generation': credential['generation']
                                }
                            }
                        }
                    }
                },
            )
    b.record(
        'restore-credential-preparation',
        generation=credential['generation'],
        application_still_stopped=True,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        'action',
        choices=[
            'migrate',
            'status',
            'rotate',
            'revoke',
            'rollback',
            'test',
            'observe',
            'prepare-restore',
            'finalize-restore',
        ],
    )
    args = parser.parse_args()
    if args.action == 'status':
        status()
    elif args.action == 'prepare-restore':
        prepare_restore()
    elif args.action in ('rotate', 'rollback', 'finalize-restore'):
        with local_worker() as worker:
            if (
                args.action != 'finalize-restore'
                and not worker.status()['configuration']['enabled']
            ):
                raise RuntimeError('Migrate Odoo before issuing manual rotations')
            if args.action == 'rollback':
                state = worker.status()
                operation = state['operation']
                if operation:
                    staged = worker.get_credentials(operation['new_role'])
                    meta = next(
                        g
                        for g in state['generations']
                        if g['generation'] == operation['generation']
                    )
                    if (
                        not staged
                        or staged['generation'] != operation['generation']
                        or w.utc(meta['expires_at']) <= w.utc(state['clock'])
                    ):
                        worker.sql('SELECT secret_control.abort(%s)', (operation['generation'],))
            if not worker.rotate(force=True):
                raise RuntimeError('Maintenance lock is held; rotation was not started')
        b.record(
            'restore-credential-finalization'
            if args.action == 'finalize-restore'
            else 'manual-rotation'
            if args.action == 'rotate'
            else 'fresh-credential-rollback'
        )
    elif args.action == 'revoke':
        with local_worker() as worker:
            state = worker.status()
            generation = state['configuration']['active_generation']
            if not generation:
                raise RuntimeError('No active rotating generation')
            worker.sql('SELECT secret_control.revoke(%s)', (generation,))
            if state['operation'] and state['operation']['generation'] != generation:
                worker.sql('SELECT secret_control.abort(%s)', (state['operation']['generation'],))
        b.record('emergency-revoke', generation=generation)
    else:
        from secrets_tests import migrate, test, observe

        {'migrate': migrate, 'test': test, 'observe': observe}[args.action]()


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Driver errors may contain password verifiers or bind parameters.
        print('Secrets operation stopped: ' + type(error).__name__, file=sys.stderr)
        raise SystemExit(1)
