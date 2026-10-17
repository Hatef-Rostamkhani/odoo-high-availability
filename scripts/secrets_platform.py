#!/usr/bin/env python3
"""Render/install the secrets platform independently of GitOps."""

import base64
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import ssl
import tarfile
import time
import urllib.request

import yaml

import openbao as b

PROTECT = {'argocd.argoproj.io/sync-options': 'Prune=false,Delete=false'}


def apply(document):
    b.k('apply', '-f', '-', data=json.dumps(document).encode())


def resources(cpu, memory, cpu_limit, memory_limit):
    return {
        'requests': {'cpu': cpu, 'memory': memory},
        'limits': {'cpu': cpu_limit, 'memory': memory_limit},
    }


def image_settings(reference):
    registry, rest = reference.split('/', 1)
    repository, tag = rest.rsplit(':', 1) if '@' not in rest else (None, None)
    # Split at the tag separator before @sha256, not the digest separator.
    base, digest = reference.split('@')
    repository, tag = base.split('/', 1)[1].rsplit(':', 1)
    return {
        'registry': registry,
        'repository': repository,
        'tag': tag + '@' + digest,
        'pullPolicy': 'IfNotPresent',
    }


def openbao_values(lock):
    config = """ui = true
disable_mlock = true
log_level = "warn"
listener "tcp" {
  address = "0.0.0.0:8200"
  cluster_address = "0.0.0.0:8201"
  tls_cert_file = "/openbao/tls/tls.crt"
  tls_key_file = "/openbao/tls/tls.key"
  telemetry { unauthenticated_metrics_access = true }
}
storage "raft" {
  path = "/openbao/data"
  performance_multiplier = 1
"""
    for i in range(3):
        config += f"""  retry_join {{
    leader_api_addr = "https://openbao-{i}.openbao-internal.openbao.svc:8200"
    leader_ca_cert_file = "/openbao/tls/ca.crt"
  }}
"""
    config += """}
seal "static" {
  current_key_id = "odoo-dev-static-1"
  current_key = "file:///openbao/seal/key"
}
service_registration "kubernetes" {}
telemetry {
  prometheus_retention_time = "60s"
  disable_hostname = true
}
audit "file" "odoo" {
  options {
    file_path = "/openbao/audit/audit.jsonl"
    mode = "0600"
    log_raw = "false"
  }
}
"""
    rotate = """while true; do
  if [ -f /openbao/audit/audit.jsonl ] && [ "$(stat -c %s /openbao/audit/audit.jsonl)" -ge 52428800 ]; then
    rm -f /openbao/audit/audit.jsonl.4
    for i in 3 2 1; do
      next=$((i+1)); [ ! -f /openbao/audit/audit.jsonl.$i ] || mv /openbao/audit/audit.jsonl.$i /openbao/audit/audit.jsonl.$next
    done
    mv /openbao/audit/audit.jsonl /openbao/audit/audit.jsonl.1
    for pid in $(pidof bao); do
      if tr '\\000' '\\n' </proc/$pid/cmdline | grep -qx server; then kill -HUP "$pid"; fi
    done
  fi
  sleep 10
done"""
    return {
        'global': {'tlsDisable': False, 'serverTelemetry': {'prometheusOperator': True}},
        'fullnameOverride': 'openbao',
        'injector': {'enabled': False},
        'csi': {'enabled': False},
        'server': {
            'image': image_settings(lock['openbao']['image']),
            'ha': {
                'enabled': True,
                'replicas': 3,
                'raft': {'enabled': True, 'setNodeId': True, 'config': config},
                'disruptionBudget': {'enabled': False},
            },
            'standalone': {'enabled': False},
            'dev': {'enabled': False},
            'resources': resources('100m', '256Mi', '500m', '512Mi'),
            'dataStorage': {'size': '1Gi', 'annotations': PROTECT},
            'auditStorage': {'enabled': True, 'size': '1Gi', 'annotations': PROTECT},
            'affinity': {
                'podAntiAffinity': {
                    'requiredDuringSchedulingIgnoredDuringExecution': [
                        {
                            'topologyKey': 'kubernetes.io/hostname',
                            'labelSelector': {
                                'matchLabels': {
                                    'app.kubernetes.io/name': 'openbao',
                                    'component': 'server',
                                }
                            },
                        }
                    ]
                }
            },
            'tolerations': [
                {
                    'key': 'node-role.kubernetes.io/control-plane',
                    'operator': 'Exists',
                    'effect': 'NoSchedule',
                }
            ],
            'volumes': [
                {'name': 'tls', 'secret': {'secretName': 'openbao-tls', 'defaultMode': 0o440}},
                {'name': 'seal', 'secret': {'secretName': 'openbao-seal', 'defaultMode': 0o440}},
            ],
            'volumeMounts': [
                {'name': 'tls', 'mountPath': '/openbao/tls', 'readOnly': True},
                {'name': 'seal', 'mountPath': '/openbao/seal', 'readOnly': True},
            ],
            'extraEnvironmentVars': {
                'BAO_CACERT': '/openbao/tls/ca.crt',
                'BAO_TLS_SERVER_NAME': 'localhost',
            },
            'readinessProbe': {
                'enabled': True,
                'path': '/v1/sys/health?standbyok=true',
                'timeoutSeconds': 5,
            },
            'livenessProbe': {
                'enabled': True,
                'path': '/v1/sys/health?standbyok=true&sealedcode=200&uninitcode=200',
                'timeoutSeconds': 5,
                'failureThreshold': 6,
            },
            'statefulSet': {
                'securityContext': {
                    'pod': {
                        'runAsUser': 100,
                        'runAsGroup': 1000,
                        'runAsNonRoot': True,
                        'fsGroup': 1000,
                        'seccompProfile': {'type': 'RuntimeDefault'},
                    },
                    'container': {
                        'allowPrivilegeEscalation': False,
                        'capabilities': {'drop': ['ALL']},
                    },
                }
            },
            'shareProcessNamespace': True,
            'extraContainers': [
                {
                    'name': 'audit-rotation',
                    'image': lock['openbao']['image'],
                    'command': ['/bin/sh', '-ec', rotate],
                    'resources': resources('10m', '16Mi', '100m', '32Mi'),
                    'securityContext': {
                        'allowPrivilegeEscalation': False,
                        'readOnlyRootFilesystem': True,
                        'capabilities': {'drop': ['ALL']},
                    },
                    'volumeMounts': [{'name': 'audit', 'mountPath': '/openbao/audit'}],
                }
            ],
            'serviceAccount': {'create': True, 'name': 'openbao', 'createSecret': False},
            'authDelegator': {'enabled': False},
            'service': {'type': 'ClusterIP'},
        },
        'ui': {'enabled': True, 'serviceType': 'ClusterIP'},
        'serverTelemetry': {
            'serviceMonitor': {
                'enabled': True,
                'selectors': {'release': 'monitoring'},
                'tlsConfig': {
                    'ca': {'secret': {'name': 'openbao-ca', 'key': 'ca.crt'}},
                    'serverName': 'openbao.openbao.svc',
                    'insecureSkipVerify': False,
                },
            }
        },
    }


def eso_values(lock):
    image = lock['eso']['image']
    repository, tag = image.split('@')[0].rsplit(':', 1)
    tag += '@' + image.split('@')[1]
    image_value = {'repository': repository, 'tag': tag, 'pullPolicy': 'IfNotPresent'}
    return {
        'fullnameOverride': 'external-secrets',
        'replicaCount': 2,
        'leaderElect': True,
        'image': image_value,
        'installCRDs': True,
        'scopedNamespace': 'odoo-dev',
        'scopedRBAC': True,
        'processClusterStore': False,
        'processClusterExternalSecret': False,
        'processPushSecret': False,
        'processClusterPushSecret': False,
        'processClusterGenerator': False,
        'rbac': {
            'serviceAccountTokenCreate': False,
            'aggregateToView': False,
            'aggregateToEdit': False,
            'aggregateToAdmin': False,
            'servicebindings': {'create': False},
        },
        'resources': resources('50m', '64Mi', '250m', '256Mi'),
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
                'preferredDuringSchedulingIgnoredDuringExecution': [
                    {
                        'weight': 100,
                        'podAffinityTerm': {
                            'topologyKey': 'kubernetes.io/hostname',
                            'labelSelector': {
                                'matchLabels': {'app.kubernetes.io/name': 'external-secrets'}
                            },
                        },
                    }
                ]
            },
        },
        'podDisruptionBudget': {'enabled': True, 'minAvailable': 1},
        'serviceMonitor': {'enabled': True, 'additionalLabels': {'release': 'monitoring'}},
        'webhook': {'image': image_value, 'resources': resources('10m', '32Mi', '100m', '128Mi')},
        'certController': {
            'image': image_value,
            'rbac': {'create': False},
            'resources': resources('10m', '32Mi', '100m', '128Mi'),
        },
    }


def write_chart(name, values, package):
    directory = b.ROOT / 'gitops/platform' / name
    (directory / 'charts').mkdir(parents=True, exist_ok=True)
    (directory / 'templates').mkdir(exist_ok=True)
    chart_name = 'openbao' if name == 'openbao' else 'external-secrets'
    version = V = b.VERSIONS['openbao_chart'] if name == 'openbao' else b.VERSIONS['eso']
    chart = {
        'apiVersion': 'v2',
        'name': 'odoo-' + name,
        'version': '0.1.0',
        'dependencies': [{'name': chart_name, 'version': version, 'repository': 'file://charts'}],
    }
    (directory / 'Chart.yaml').write_text(yaml.safe_dump(chart, sort_keys=False))
    gitops_values = __import__('copy').deepcopy(values)
    if name == 'external-secrets':
        # Bootstrap owns CRDs, privileges and generated webhook key material.
        # GitOps owns workloads and namespaced application declarations.
        gitops_values['installCRDs'] = False
        gitops_values['rbac']['create'] = False
        gitops_values['webhook']['secretAnnotations'] = {'argocd.argoproj.io/hook': 'Skip'}
    (directory / 'values.yaml').write_text(
        yaml.safe_dump({chart_name: gitops_values}, sort_keys=False)
    )
    shutil.copyfile(b.ROOT / 'vendor' / package, directory / 'charts' / package)
    plain = b.ROOT / 'infrastructure/secrets' / (name + '-values.yaml')
    plain.write_text(yaml.safe_dump(values, sort_keys=False))
    app = {
        'apiVersion': 'argoproj.io/v1alpha1',
        'kind': 'Application',
        'metadata': {'name': name, 'namespace': 'argocd'},
        'spec': {
            'project': 'odoo-secrets',
            'source': {
                'repoURL': 'git://local-git.gitops-system.svc.cluster.local:9418/odoo-platform.git',
                'targetRevision': 'main',
                'path': 'gitops/platform/' + name,
                'helm': {'releaseName': name},
            },
            'destination': {'server': 'https://kubernetes.default.svc', 'namespace': name},
            'syncPolicy': {'syncOptions': ['RespectIgnoreDifferences=true']},
        },
    }
    (b.ROOT / 'gitops/applications' / (name + '.yaml')).write_text(
        yaml.safe_dump(app, sort_keys=False)
    )
    if name == 'external-secrets':
        app['spec']['ignoreDifferences'] = [
            {
                'group': 'admissionregistration.k8s.io',
                'kind': 'ValidatingWebhookConfiguration',
                'name': webhook,
                'jqPathExpressions': ['.webhooks[].clientConfig.caBundle'],
            }
            for webhook in ('secretstore-validate', 'externalsecret-validate')
        ]
        (b.ROOT / 'gitops/applications' / f'{name}.yaml').write_text(
            yaml.safe_dump(app, sort_keys=False)
        )
    if name == 'openbao':
        public = [
            {
                'apiVersion': 'policy/v1',
                'kind': 'PodDisruptionBudget',
                'metadata': {'name': 'openbao', 'namespace': 'openbao'},
                'spec': {
                    'minAvailable': 2,
                    'selector': {
                        'matchLabels': {
                            'app.kubernetes.io/name': 'openbao',
                            'app.kubernetes.io/instance': 'openbao',
                            'component': 'server',
                        }
                    },
                },
            }
        ]
        for sa in ('bao-bootstrap', 'bao-operator'):
            public.append(
                {
                    'apiVersion': 'v1',
                    'kind': 'ServiceAccount',
                    'metadata': {'name': sa, 'namespace': 'openbao'},
                    'automountServiceAccountToken': False,
                }
            )
        public.extend(
            [
                {
                    'apiVersion': 'rbac.authorization.k8s.io/v1',
                    'kind': 'ClusterRole',
                    'metadata': {'name': 'odoo-openbao-tokenreview'},
                    'rules': [
                        {
                            'apiGroups': ['authentication.k8s.io'],
                            'resources': ['tokenreviews'],
                            'verbs': ['create'],
                        }
                    ],
                },
                {
                    'apiVersion': 'rbac.authorization.k8s.io/v1',
                    'kind': 'ClusterRoleBinding',
                    'metadata': {'name': 'odoo-openbao-tokenreview'},
                    'roleRef': {
                        'apiGroup': 'rbac.authorization.k8s.io',
                        'kind': 'ClusterRole',
                        'name': 'odoo-openbao-tokenreview',
                    },
                    'subjects': [
                        {'kind': 'ServiceAccount', 'name': 'openbao', 'namespace': 'openbao'}
                    ],
                },
            ]
        )
        for document in public:
            if document['kind'] in ('ClusterRole', 'ClusterRoleBinding'):
                document['metadata']['annotations'] = {'argocd.argoproj.io/hook': 'Skip'}
        (directory / 'bootstrap.yaml').write_text(yaml.safe_dump_all(public, sort_keys=False))
        (directory / 'gitops.yaml').write_text(
            yaml.safe_dump_all(
                [
                    document
                    for document in public
                    if document['kind'] not in ('ClusterRole', 'ClusterRoleBinding')
                ],
                sort_keys=False,
            )
        )
        (directory / 'templates/bootstrap.yaml').write_text('{{ .Files.Get "gitops.yaml" }}\n')
    return plain


def token(sa='bao-bootstrap', namespace='openbao'):
    return (
        b.k('-n', namespace, 'create', 'token', sa, '--audience=openbao', '--duration=10m')
        .decode()
        .strip()
    )


def login(address, role='odoo-bootstrap', sa='bao-bootstrap', namespace='openbao'):
    return b.api(address, 'auth/kubernetes/login', {'role': role, 'jwt': token(sa, namespace)})[
        'auth'
    ]['client_token']


def configure_openbao(address, initial_token):
    root = initial_token
    mounts = b.api(address, 'sys/mounts', token=root)['data']
    if 'kv/' not in mounts:
        b.api(address, 'sys/mounts/kv', {'type': 'kv', 'options': {'version': '2'}}, root)
    auth = b.api(address, 'sys/auth', token=root)['data']
    if 'kubernetes/' not in auth:
        b.api(address, 'sys/auth/kubernetes', {'type': 'kubernetes'}, root)
    ca = obj = b.obj('openbao', 'configmap', 'kube-root-ca.crt')['data']['ca.crt']
    b.api(
        address,
        'auth/kubernetes/config',
        {
            'kubernetes_host': 'https://kubernetes.default.svc:443',
            'kubernetes_ca_cert': ca,
            'disable_local_ca_jwt': False,
        },
        root,
    )
    policies = {
        'odoo-eso': 'path "kv/data/odoo-dev/odoo-db/active" { capabilities = ["read"] }',
        'odoo-rotator': """path "kv/data/odoo-dev/odoo-db/active" { capabilities = ["create","read","update"] }
path "kv/data/odoo-dev/odoo-db/slot-a" { capabilities = ["create","read","update"] }
path "kv/data/odoo-dev/odoo-db/slot-b" { capabilities = ["create","read","update"] }""",
        'odoo-bootstrap': """path "sys/storage/raft/configuration" { capabilities = ["read"] }
path "sys/storage/raft/autopilot/state" { capabilities = ["read"] }
path "sys/storage/raft/snapshot" { capabilities = ["read"] }
path "kv/data/odoo-dev/odoo-db/sentinel" { capabilities = ["create","read","update"] }
path "sys/metrics" { capabilities = ["read"] }""",
        'odoo-operator': """path "sys/generate-root-token/attempt" { capabilities = ["read","update","delete","sudo"] }
path "sys/generate-root-token/update" { capabilities = ["update","sudo"] }""",
    }
    for name, policy in policies.items():
        b.api(address, 'sys/policies/acl/' + name, {'policy': policy}, root)
    for suffix, sa, ns in [
        ('eso', 'eso-odoo', 'odoo-dev'),
        ('rotator', 'secret-rotator', 'secrets-system'),
        ('bootstrap', 'bao-bootstrap', 'openbao'),
        ('operator', 'bao-operator', 'openbao'),
    ]:
        b.api(
            address,
            'auth/kubernetes/role/odoo-' + suffix,
            {
                'bound_service_account_names': [sa],
                'bound_service_account_namespaces': [ns],
                'audience': 'openbao',
                'token_policies': ['odoo-' + suffix],
                'token_ttl': '10m',
                'token_max_ttl': '30m',
            },
            root,
        )


def namespace_policy(ns):
    def ns_peer(name):
        return {'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': name}}}

    node = next(
        n
        for n in json.loads(b.k('get', 'nodes', '-o', 'json'))['items']
        if n['metadata']['name'].endswith('control-plane')
    )
    node_ip = next(a['address'] for a in node['status']['addresses'] if a['type'] == 'InternalIP')
    egress = [
        {
            'to': [ns_peer('kube-system')],
            'ports': [{'protocol': p, 'port': 53} for p in ('UDP', 'TCP')],
        },
    ]
    ingress = [
        {
            'from': [ns_peer('monitoring')],
            'ports': [{'protocol': 'TCP', 'port': p} for p in (8200, 8080, 9117)],
        }
    ]
    if ns == 'openbao':
        ingress += [
            {
                'from': [ns_peer(n) for n in ('openbao', 'external-secrets', 'secrets-system')],
                'ports': [{'protocol': 'TCP', 'port': 8200}],
            },
            {'from': [ns_peer('openbao')], 'ports': [{'protocol': 'TCP', 'port': 8201}]},
        ]
        egress += [
            {
                'to': [ns_peer('openbao')],
                'ports': [{'protocol': 'TCP', 'port': p} for p in (8200, 8201)],
            }
        ]
    elif ns == 'external-secrets':
        ingress += [
            {
                'from': [{'ipBlock': {'cidr': node_ip + '/32'}}, ns_peer('kube-system')],
                'ports': [{'protocol': 'TCP', 'port': p} for p in (10250, 8081)],
            }
        ]
        egress += [{'to': [ns_peer('openbao')], 'ports': [{'protocol': 'TCP', 'port': 8200}]}]
    else:
        egress += [{'to': [ns_peer('postgres-ha')], 'ports': [{'protocol': 'TCP', 'port': 5432}]}]
    return {
        'apiVersion': 'networking.k8s.io/v1',
        'kind': 'NetworkPolicy',
        'metadata': {'name': 'secrets-isolation', 'namespace': ns},
        'spec': {
            'podSelector': {},
            'policyTypes': ['Ingress', 'Egress'],
            'ingress': ingress,
            'egress': egress,
        },
    }


def install():
    b.preflight()
    lock = b.prepare_charts_images()
    values = {'openbao': openbao_values(lock), 'external-secrets': eso_values(lock)}
    paths = {
        name: write_chart(
            name,
            value,
            'openbao-0.30.3.tgz' if name == 'openbao' else 'external-secrets-2.12.0.tgz',
        )
        for name, value in values.items()
    }
    image_manifest = b.ROOT / '.state/secrets-images.yaml'
    image_manifest.write_text(
        yaml.safe_dump({'images': [{'image': lock[n]['image']} for n in ('openbao', 'eso')]})
    )
    b.run(['bash', b.ROOT / 'scripts/cache-images.sh', image_manifest], timeout=1300)
    b.preflight(after_images=True)
    b.prepare_keys()
    b.encrypt_etcd()
    for ns in ('openbao', 'external-secrets', 'secrets-system'):
        b.namespace(ns)
    b.ensure_secret('openbao', 'openbao-seal', {'key': (b.PRIVATE / 'seal.key').read_bytes()})
    b.ensure_secret('openbao', 'openbao-ca', {'ca.crt': (b.PRIVATE / 'ca.crt').read_bytes()})
    b.ensure_secret(
        'openbao',
        'openbao-tls',
        {
            'tls.crt': (b.PRIVATE / 'server.crt').read_bytes(),
            'tls.key': (b.PRIVATE / 'server.key').read_bytes(),
            'ca.crt': (b.PRIVATE / 'ca.crt').read_bytes(),
        },
    )
    b.verify_etcd_secret('openbao', 'openbao-seal')
    apply(
        {
            'apiVersion': 'v1',
            'kind': 'ServiceAccount',
            'metadata': {'name': 'bao-bootstrap', 'namespace': 'openbao'},
            'automountServiceAccountToken': False,
        }
    )
    apply(
        {
            'apiVersion': 'v1',
            'kind': 'ServiceAccount',
            'metadata': {'name': 'bao-operator', 'namespace': 'openbao'},
            'automountServiceAccountToken': False,
        }
    )
    apply(
        {
            'apiVersion': 'v1',
            'kind': 'ServiceAccount',
            'metadata': {'name': 'eso-odoo', 'namespace': 'odoo-dev'},
            'automountServiceAccountToken': False,
        }
    )
    apply(
        {
            'apiVersion': 'v1',
            'kind': 'ServiceAccount',
            'metadata': {'name': 'secret-rotator', 'namespace': 'secrets-system'},
            'automountServiceAccountToken': False,
        }
    )
    # TokenReview only: no Secret reads and no SubjectAccessReview privilege.
    apply(
        {
            'apiVersion': 'rbac.authorization.k8s.io/v1',
            'kind': 'ClusterRole',
            'metadata': {'name': 'odoo-openbao-tokenreview'},
            'rules': [
                {
                    'apiGroups': ['authentication.k8s.io'],
                    'resources': ['tokenreviews'],
                    'verbs': ['create'],
                }
            ],
        }
    )
    apply(
        {
            'apiVersion': 'rbac.authorization.k8s.io/v1',
            'kind': 'ClusterRoleBinding',
            'metadata': {'name': 'odoo-openbao-tokenreview'},
            'roleRef': {
                'apiGroup': 'rbac.authorization.k8s.io',
                'kind': 'ClusterRole',
                'name': 'odoo-openbao-tokenreview',
            },
            'subjects': [{'kind': 'ServiceAccount', 'name': 'openbao', 'namespace': 'openbao'}],
        }
    )
    # Namespaced ESO chart bindings are rendered into the monitored namespace.
    b.run(
        [
            b.ROOT / '.tools/bin/helm',
            '--kubeconfig',
            b.ROOT / '.state/kubeconfig',
            'upgrade',
            '--install',
            'openbao',
            b.ROOT / 'vendor/openbao-0.30.3.tgz',
            '-n',
            'openbao',
            '-f',
            paths['openbao'],
        ],
        timeout=180,
    )
    for document in yaml.safe_load_all(
        (b.ROOT / 'gitops/platform/openbao/bootstrap.yaml').read_text()
    ):
        apply(document)
    b.wait(lambda: b.exists('openbao', 'pod', 'openbao-0'), 120)
    init_path = b.PRIVATE / 'recovery.json'
    with b.forward() as address:
        b.wait(lambda: b.api(address, 'sys/seal-status'), 120)
        state = b.api(address, 'sys/seal-status')
        if not state['initialized']:
            if init_path.exists():
                raise RuntimeError(
                    'Existing recovery material belongs to prior Raft data; restore its snapshot instead of initializing again'
                )
            initial = b.api(
                address, 'sys/init', {'recovery_shares': 5, 'recovery_threshold': 3}, method='PUT'
            )
            b.private_write(init_path, json.dumps(initial))
        elif not init_path.exists():
            raise RuntimeError(
                'Initialized OpenBao has no private recovery record; restore that record'
            )
        b.wait(lambda: not b.api(address, 'sys/seal-status')['sealed'], 120)
        initial = json.loads(init_path.read_text())
        if initial.get('root_token'):
            configure_openbao(address, initial['root_token'])
            b.api(address, 'auth/token/revoke-self', {}, initial['root_token'])
            initial.pop('root_token')
            initial['root_token_revoked'] = True
            b.private_write(init_path, json.dumps(initial))
    # The upstream chart deliberately uses OnDelete; upgrades and fault tests
    # replace one Raft member at a time rather than invoking rollout status.
    b.wait(
        lambda: b.obj('openbao', 'statefulset', 'openbao')['status'].get('readyReplicas') == 3, 180
    )
    # Retain local-path PVs as well as claims; no accidental deletion by GitOps.
    for pvc in json.loads(b.k('-n', 'openbao', 'get', 'pvc', '-o', 'json'))['items']:
        name = pvc['metadata']['name']
        b.k(
            '-n',
            'openbao',
            'annotate',
            'pvc',
            name,
            'argocd.argoproj.io/sync-options=Prune=false,Delete=false',
            '--overwrite',
        )
        if pvc.get('spec', {}).get('volumeName'):
            b.k(
                'patch',
                'pv',
                pvc['spec']['volumeName'],
                '--type=merge',
                '-p',
                '{"spec":{"persistentVolumeReclaimPolicy":"Retain"}}',
            )
    from secrets_admin import eso_rbac

    extras = eso_rbac()
    for document in extras:
        if document['kind'] in ('Role', 'RoleBinding', 'ClusterRole', 'ClusterRoleBinding'):
            document['metadata']['annotations'] = {'argocd.argoproj.io/hook': 'Skip'}
    for document in extras:
        apply(document)
    extra_text = yaml.safe_dump_all(extras, sort_keys=False)
    (b.ROOT / 'infrastructure/secrets/eso-rbac.yaml').write_text(extra_text)
    chart_dir = b.ROOT / 'gitops/platform/external-secrets'
    (chart_dir / 'extra.yaml').write_text(
        yaml.safe_dump_all(
            [
                document
                for document in extras
                if document['kind']
                not in ('Role', 'RoleBinding', 'ClusterRole', 'ClusterRoleBinding')
            ],
            sort_keys=False,
        )
    )
    (chart_dir / 'templates/extra.yaml').write_text('{{ .Files.Get "extra.yaml" }}\n')
    b.run(
        [
            b.ROOT / '.tools/bin/helm',
            '--kubeconfig',
            b.ROOT / '.state/kubeconfig',
            'upgrade',
            '--install',
            'external-secrets',
            b.ROOT / 'vendor/external-secrets-2.12.0.tgz',
            '-n',
            'external-secrets',
            '-f',
            paths['external-secrets'],
            '--wait',
            '--timeout=180s',
        ],
        timeout=200,
    )
    b.ensure_secret('odoo-dev', 'openbao-ca', {'ca.crt': (b.PRIVATE / 'ca.crt').read_bytes()})
    for ns in ('openbao', 'external-secrets', 'secrets-system'):
        apply(namespace_policy(ns))
    for document in yaml.safe_load_all(
        (b.ROOT / 'infrastructure/secrets/api-policy.yaml').read_text()
    ):
        apply(document)
    from secrets_admin import bootstrap_control, render_runtime, render_gitops, render_snapshot_job

    bootstrap_control()
    render_runtime()
    render_snapshot_job()
    render_gitops()
    from secrets_gitops import configure

    configure()
    b.status()
    b.record('openbao-install', root_token_revoked=True, application_connection_changed=False)


def backup():
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    directory = b.ROOT / 'backups/openbao'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    with leader_forward() as address:
        auth = login(address)
        request = urllib.request.Request(
            address + '/v1/sys/storage/raft/snapshot', headers={'X-Vault-Token': auth}
        )
        with urllib.request.urlopen(
            request,
            context=ssl.create_default_context(cafile=str(b.PRIVATE / 'ca.crt')),
            timeout=120,
        ) as response:
            snapshot = response.read()
    nonce = secrets.token_bytes(12)
    encrypted = AESGCM((b.PRIVATE / 'snapshot.key').read_bytes()).encrypt(
        nonce, snapshot, b'odoo-openbao-snapshot-v1'
    )
    path = directory / (
        dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '.snapshot.aesgcm'
    )
    b.private_write(path, b'ODOO-BAO-1\n' + nonce + encrypted)
    for old in directory.glob('*.snapshot.aesgcm'):
        if time.time() - old.stat().st_mtime > 7 * 86400:
            old.unlink()
    return b.record(
        'openbao-backup',
        archive=str(path),
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        encryption='AES-256-GCM',
        local_host_only=True,
    )


@contextlib.contextmanager
def leader_forward():
    pods = json.loads(
        b.k(
            '-n',
            'openbao',
            'get',
            'pods',
            '-l',
            'app.kubernetes.io/name=openbao,component=server',
            '-o',
            'json',
        )
    )['items']
    for pod in pods:
        if pod['status'].get('phase') != 'Running' or pod['metadata'].get('deletionTimestamp'):
            continue
        stack = contextlib.ExitStack()
        try:
            address = stack.enter_context(b.forward(pod['metadata']['name']))
            is_self = b.api(address, 'sys/leader')['is_self']
        except (RuntimeError, OSError):
            stack.close()
            continue
        if is_self:
            with stack:
                yield address
            return
        stack.close()
    raise RuntimeError('No OpenBao Raft leader was found')


def restore_test():
    import secrets_tests as test

    test.TEST_NS = 'bao-restore-test'
    test.TEST_CLUSTER = 'bao-restore-test'
    try:
        test.cleanup()
        return test.restore_openbao()
    except BaseException as error:
        b.record('openbao-restore', 'FAIL', error_type=type(error).__name__, isolated=True)
        raise
