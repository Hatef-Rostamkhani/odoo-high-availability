#!/usr/bin/env python3
"""Live acceptance gates. Short credential lifetimes exist only on owned clones."""

import base64
import contextlib
import copy
import datetime as dt
import fcntl
import hashlib
import http.cookiejar
import importlib.util
import json
import os
from pathlib import Path
import secrets
import select
import shutil
import signal
import socket
import ssl
import struct
import subprocess
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.request

import psycopg2
import yaml
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import openbao as b
import secrets_admin as a
from secrets_platform import apply, login, leader_forward, resources

TEST_NS = 'secrets-test'
TEST_CLUSTER = 'secrets-test'
OWNED = {'odoo.io/secrets-test': 'true'}


def checked_namespace():
    if b.exists(TEST_NS, 'namespace', TEST_NS):
        if (
            b.obj(TEST_NS, 'namespace', TEST_NS)['metadata']
            .get('labels', {})
            .get('odoo.io/secrets-test')
            != 'true'
        ):
            raise RuntimeError('An unrelated test namespace exists; refusing to touch it')
    else:
        apply(
            {
                'apiVersion': 'v1',
                'kind': 'Namespace',
                'metadata': {'name': TEST_NS, 'labels': OWNED},
            }
        )


def replace_secret(namespace, name, values):
    if namespace != TEST_NS:
        raise ValueError('Test Secret writes are restricted to the owned test namespace')
    document = {
        'apiVersion': 'v1',
        'kind': 'Secret',
        'metadata': {'name': name, 'namespace': namespace},
        'type': 'Opaque',
        'data': {
            key: base64.b64encode(value if isinstance(value, bytes) else value.encode()).decode()
            for key, value in values.items()
        },
    }
    if b.exists(namespace, 'secret', name):
        document['metadata']['resourceVersion'] = b.obj(namespace, 'secret', name)['metadata'][
            'resourceVersion'
        ]
        b.k('replace', '-f', '-', data=json.dumps(document).encode())
    else:
        b.k('create', '-f', '-', data=json.dumps(document).encode())


def plain_api(address, path, payload=None, token=None, method=None, raw=None):
    headers = {
        'Content-Type': 'application/octet-stream' if raw is not None else 'application/json'
    }
    if token:
        headers['X-Vault-Token'] = token
    request = urllib.request.Request(
        address + '/v1/' + path,
        method=method or ('POST' if payload is not None or raw is not None else 'GET'),
        data=raw
        if raw is not None
        else json.dumps(payload).encode()
        if payload is not None
        else None,
        headers=headers,
    )
    try:
        with urllib.request.urlopen(
            request,
            context=ssl.create_default_context(cafile=str(b.PRIVATE / 'ca.crt')),
            timeout=90,
        ) as response:
            body = response.read()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as error:
        # Record only the operation and HTTP status; response bodies can include
        # recovery material and must never enter an acceptance report.
        raise RuntimeError(f'Clone API {path} returned HTTP {error.code}') from None


def decoded_snapshot(path):
    data = Path(path).read_bytes()
    magic = b'ODOO-BAO-1\n'
    if not data.startswith(magic):
        raise ValueError('Unsupported encrypted snapshot')
    data = data[len(magic) :]
    return AESGCM((b.PRIVATE / 'snapshot.key').read_bytes()).decrypt(
        data[:12], data[12:], b'odoo-openbao-snapshot-v1'
    )


def recover_clone_root(address):
    specification = importlib.util.spec_from_file_location(
        'bao_recovery', b.ROOT / 'scripts/openbao-recovery.py'
    )
    recovery = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(recovery)
    auth = login(address, 'odoo-operator', 'bao-operator', 'openbao')
    return recovery.recover(address, auth)


def create_bao_clone():
    checked_namespace()
    directory = b.PRIVATE / 'rehearsal'
    directory.mkdir(mode=0o700, exist_ok=True)
    b.private_write(
        directory / 'server.ext',
        'subjectAltName=DNS:bao-test.'
        + TEST_NS
        + '.svc,DNS:localhost,IP:127.0.0.1\nextendedKeyUsage=serverAuth\n',
    )
    b.run(
        [
            'openssl',
            'req',
            '-new',
            '-newkey',
            'rsa:3072',
            '-nodes',
            '-subj',
            '/CN=bao-test.secrets-test.svc',
            '-keyout',
            directory / 'server.key',
            '-out',
            directory / 'server.csr',
        ]
    )
    b.run(
        [
            'openssl',
            'x509',
            '-req',
            '-in',
            directory / 'server.csr',
            '-CA',
            b.PRIVATE / 'ca.crt',
            '-CAkey',
            b.PRIVATE / 'ca.key',
            '-CAcreateserial',
            '-days',
            '2',
            '-extfile',
            directory / 'server.ext',
            '-out',
            directory / 'server.crt',
        ]
    )
    replace_secret(
        TEST_NS,
        'bao-test-tls',
        {
            'tls.crt': (directory / 'server.crt').read_bytes(),
            'tls.key': (directory / 'server.key').read_bytes(),
            'ca.crt': (b.PRIVATE / 'ca.crt').read_bytes(),
        },
    )
    replace_secret(TEST_NS, 'bao-test-seal', {'key': (b.PRIVATE / 'seal.key').read_bytes()})
    config = """ui = false
disable_mlock = true
api_addr = "https://bao-test.secrets-test.svc:8200"
cluster_addr = "https://bao-test.secrets-test.svc:8201"
listener "tcp" {
 address = "0.0.0.0:8200"
 cluster_address = "0.0.0.0:8201"
 tls_cert_file = "/run/tls/tls.crt"
 tls_key_file = "/run/tls/tls.key"
}
storage "raft" { path = "/openbao/data" node_id = "secrets-test" }
seal "static" { current_key_id = "odoo-dev-static-1" current_key = "file:///run/seal/key" }
"""
    config = config.replace('secrets-test', TEST_NS)
    apply(
        {
            'apiVersion': 'v1',
            'kind': 'ConfigMap',
            'metadata': {'name': 'bao-test', 'namespace': TEST_NS},
            'data': {'config.hcl': config},
        }
    )
    apply(
        {
            'apiVersion': 'v1',
            'kind': 'ServiceAccount',
            'metadata': {'name': 'bao-test', 'namespace': TEST_NS},
        }
    )
    apply(
        {
            'apiVersion': 'rbac.authorization.k8s.io/v1',
            'kind': 'ClusterRoleBinding',
            'metadata': {'name': 'odoo-' + TEST_NS + '-tokenreview', 'labels': OWNED},
            'roleRef': {
                'apiGroup': 'rbac.authorization.k8s.io',
                'kind': 'ClusterRole',
                'name': 'odoo-openbao-tokenreview',
            },
            'subjects': [{'kind': 'ServiceAccount', 'name': 'bao-test', 'namespace': TEST_NS}],
        }
    )
    apply(
        {
            'apiVersion': 'v1',
            'kind': 'PersistentVolumeClaim',
            'metadata': {'name': 'bao-test', 'namespace': TEST_NS},
            'spec': {
                'accessModes': ['ReadWriteOnce'],
                'resources': {'requests': {'storage': '1Gi'}},
            },
        }
    )
    image = json.loads((b.ROOT / 'infrastructure/secrets/images.lock.json').read_text())['openbao'][
        'image'
    ]
    pod = {
        'apiVersion': 'v1',
        'kind': 'Pod',
        'metadata': {
            'name': 'bao-test',
            'namespace': TEST_NS,
            'labels': {'app': 'bao-test', **OWNED},
        },
        'spec': {
            'serviceAccountName': 'bao-test',
            'securityContext': {
                'runAsUser': 100,
                'runAsGroup': 1000,
                'runAsNonRoot': True,
                'fsGroup': 1000,
                'seccompProfile': {'type': 'RuntimeDefault'},
            },
            'containers': [
                {
                    'name': 'openbao',
                    'image': image,
                    'command': ['bao', 'server', '-config=/run/config/config.hcl'],
                    'resources': resources('100m', '256Mi', '500m', '512Mi'),
                    'securityContext': {
                        'allowPrivilegeEscalation': False,
                        'capabilities': {'drop': ['ALL']},
                    },
                    'ports': [{'name': 'https', 'containerPort': 8200}],
                    'readinessProbe': {
                        'httpGet': {
                            'path': '/v1/sys/health?standbyok=true',
                            'port': 'https',
                            'scheme': 'HTTPS',
                        },
                        'periodSeconds': 2,
                        'timeoutSeconds': 2,
                    },
                    'volumeMounts': [
                        {'name': name, 'mountPath': path, 'readOnly': name != 'data'}
                        for name, path in [
                            ('config', '/run/config'),
                            ('tls', '/run/tls'),
                            ('seal', '/run/seal'),
                            ('data', '/openbao/data'),
                        ]
                    ]
                    + [{'name': 'audit', 'mountPath': '/openbao/audit'}],
                }
            ],
            'volumes': [
                {'name': 'config', 'configMap': {'name': 'bao-test'}},
                {'name': 'tls', 'secret': {'secretName': 'bao-test-tls', 'defaultMode': 0o440}},
                {'name': 'seal', 'secret': {'secretName': 'bao-test-seal', 'defaultMode': 0o440}},
                {'name': 'data', 'persistentVolumeClaim': {'claimName': 'bao-test'}},
                {'name': 'audit', 'emptyDir': {}},
            ],
        },
    }
    apply(pod)
    apply(
        {
            'apiVersion': 'v1',
            'kind': 'Service',
            'metadata': {'name': 'bao-test', 'namespace': TEST_NS},
            'spec': {
                'selector': {'app': 'bao-test'},
                'ports': [{'port': 8200, 'targetPort': 'https'}],
            },
        }
    )
    b.wait(lambda: b.obj(TEST_NS, 'pod', 'bao-test')['status'].get('phase') == 'Running', 120)
    return pod


def restart_clone(pod):
    b.k('-n', TEST_NS, 'delete', 'pod', 'bao-test', '--wait=true', '--timeout=45s')
    apply(pod)
    b.wait(lambda: b.obj(TEST_NS, 'pod', 'bao-test')['status'].get('phase') == 'Running', 120)


def restore_openbao(keep=False):
    checked_namespace()
    pod = create_bao_clone()
    try:
        with leader_forward() as live:
            auth = login(live)
            sentinel = secrets.token_hex(16)
            b.api(live, 'kv/data/odoo-dev/odoo-db/sentinel', {'data': {'value': sentinel}}, auth)
        from secrets_platform import backup

        archive = Path(backup()['archive'])
        data = decoded_snapshot(archive)
        damaged = bytearray(archive.read_bytes())
        damaged[-1] ^= 1
        try:
            magic = b'ODOO-BAO-1\n'
            chunk = bytes(damaged[len(magic) :])
            AESGCM((b.PRIVATE / 'snapshot.key').read_bytes()).decrypt(
                chunk[:12], chunk[12:], b'odoo-openbao-snapshot-v1'
            )
        except Exception:
            pass
        else:
            raise AssertionError('Corrupted encrypted snapshot was accepted')
        with b.forward('bao-test', TEST_NS) as address:
            b.wait(lambda: b.api(address, 'sys/seal-status'), 30)
            init = plain_api(
                address, 'sys/init', {'recovery_shares': 5, 'recovery_threshold': 3}, method='PUT'
            )
            b.wait(lambda: not b.api(address, 'sys/seal-status')['sealed'], 30)
            plain_api(
                address, 'sys/storage/raft/snapshot-force', token=init['root_token'], raw=data
            )
        restart_clone(pod)
        with b.forward('bao-test', TEST_NS) as address:
            b.wait(lambda: not b.api(address, 'sys/seal-status')['sealed'], 60)
            b.wait(lambda: b.api(address, 'sys/leader')['is_self'], 60)
            root = recover_clone_root(address)
            # Configure identical KV flows with auth bound to the isolated SA.
            for suffix, sa, policy in [
                ('rotator', 'secret-rotator', 'odoo-rotator'),
                ('eso', 'eso-odoo', 'odoo-eso'),
            ]:
                plain_api(
                    address,
                    'auth/kubernetes/role/odoo-test-' + suffix,
                    {
                        'bound_service_account_names': [sa],
                        'bound_service_account_namespaces': [TEST_NS],
                        'audience': 'openbao',
                        'token_policies': [policy],
                        'token_ttl': '10m',
                        'token_max_ttl': '30m',
                    },
                    root,
                )
            restored = plain_api(address, 'kv/data/odoo-dev/odoo-db/sentinel', token=root)['data'][
                'data'
            ]['value']
            if restored != sentinel:
                raise AssertionError('Restored OpenBao sentinel mismatch')
            plain_api(address, 'auth/token/revoke-self', {}, root)
            reader = login(address)
        # Validate a missing key file and a present but incorrect key independently.
        for label, values in [
            ('missing', {'unrelated': secrets.token_bytes(32)}),
            ('wrong', {'key': secrets.token_bytes(32)}),
        ]:
            replace_secret(TEST_NS, 'bao-test-seal', values)
            restart_clone(pod)
            if label == 'missing':
                b.wait(
                    lambda: any(
                        c.get('state', {}).get('waiting', {}).get('reason') == 'CrashLoopBackOff'
                        or c.get('lastState', {}).get('terminated', {}).get('exitCode', 0) > 0
                        for c in b.obj(TEST_NS, 'pod', 'bao-test')['status'].get(
                            'containerStatuses', []
                        )
                    ),
                    60,
                )
            else:
                with b.forward('bao-test', TEST_NS) as address:
                    b.wait(lambda: b.api(address, 'sys/seal-status')['sealed'], 30)
                    try:
                        b.api(address, 'kv/data/odoo-dev/odoo-db/sentinel', token=reader)
                    except RuntimeError as error:
                        assert str(error).endswith('HTTP 503')
                    else:
                        raise AssertionError('Sealed clone allowed secret reads')
            b.wait(
                lambda: (
                    not any(
                        c.get('ready')
                        for c in b.obj(TEST_NS, 'pod', 'bao-test')['status'].get(
                            'containerStatuses', []
                        )
                    )
                ),
                30,
            )
            b.record(
                'static-seal-' + label + '-key',
                isolated=True,
                access_rejected=True,
                pod_ready=False,
            )
            replace_secret(TEST_NS, 'bao-test-seal', {'key': (b.PRIVATE / 'seal.key').read_bytes()})
            restart_clone(pod)
            with b.forward('bao-test', TEST_NS) as address:
                b.wait(lambda: not b.api(address, 'sys/seal-status')['sealed'], 60)
        return b.record(
            'openbao-restore',
            isolated=True,
            archive=str(archive),
            sentinel_preserved=True,
            tampered_archive_rejected=True,
            production_credentials_republished=False,
        )
    finally:
        if not keep:
            cleanup()


def cleanup():
    if b.exists('postgres-ha', 'postgrescluster', TEST_CLUSTER):
        cluster = b.obj('postgres-ha', 'postgrescluster', TEST_CLUSTER)
        if cluster['metadata'].get('labels', {}).get('odoo.io/secrets-test') != 'true':
            raise RuntimeError('Refusing to delete an unrelated PostgreSQL cluster')
        b.k(
            '-n',
            'postgres-ha',
            'delete',
            'postgrescluster',
            TEST_CLUSTER,
            '--wait=true',
            '--timeout=120s',
        )
        b.k(
            '-n',
            'postgres-ha',
            'delete',
            'pvc',
            '-l',
            'postgres-operator.crunchydata.com/cluster=' + TEST_CLUSTER,
            '--wait=true',
            '--timeout=120s',
        )
    b.k(
        '-n',
        'postgres-ha',
        'delete',
        'networkpolicy',
        TEST_CLUSTER + '-access',
        '--ignore-not-found',
    )
    if b.exists(TEST_NS, 'namespace', TEST_NS):
        checked_namespace()
        for item in json.loads(b.k('-n', TEST_NS, 'get', 'externalsecrets', '-o', 'json'))['items']:
            name = item['metadata']['name']
            b.k('-n', TEST_NS, 'delete', 'externalsecret', name, '--wait=false')
            # A terminating owned namespace may have already lost its scoped
            # ESO controller. Its Orphan/Retain target needs no deletion work.
            namespace = b.obj(TEST_NS, 'namespace', TEST_NS)
            if namespace['metadata'].get('deletionTimestamp'):
                target = item['spec']['target']
                if (
                    target.get('creationPolicy') != 'Orphan'
                    or target.get('deletionPolicy') != 'Retain'
                ):
                    raise RuntimeError('Refusing to bypass target cleanup for a managed Secret')
                b.k(
                    '-n',
                    TEST_NS,
                    'patch',
                    'externalsecret',
                    name,
                    '--type=json',
                    '-p',
                    json.dumps([{'op': 'remove', 'path': '/metadata/finalizers'}]),
                )
            else:
                b.k(
                    '-n', TEST_NS, 'wait', '--for=delete', 'externalsecret/' + name, '--timeout=60s'
                )
        b.k('delete', 'namespace', TEST_NS, '--wait=true', '--timeout=120s', timeout=130)
    b.k('delete', 'clusterrolebinding', 'odoo-' + TEST_NS + '-tokenreview', '--ignore-not-found')
    # Only a directory created by this harness is removed; production NFS is untouched.
    path = Path('/srv/odoo/odoo-data/.secrets-test')
    if TEST_NS == 'secrets-test' and path.exists():
        shutil.rmtree(path)
    if TEST_NS == 'secrets-test' and b.exists('', 'pv', 'secrets-test-data'):
        pv = b.obj('', 'pv', 'secrets-test-data')
        if pv['metadata'].get('labels', {}).get('odoo.io/secrets-test') == 'true':
            b.k('delete', 'pv', 'secrets-test-data', '--wait=true', '--timeout=60s')


def create_database_clone(backup):
    from ha import healthy, wait

    cluster = copy.deepcopy(
        yaml.safe_load((b.ROOT / 'infrastructure/database-ha/cluster.yaml').read_text())
    )
    cluster['metadata'] = {'name': TEST_CLUSTER, 'namespace': 'postgres-ha', 'labels': OWNED}
    item = cluster['spec']['instances'][0]
    item['replicas'] = 1
    item['minAvailable'] = 1
    item.pop('affinity', None)
    item.pop('tolerations', None)
    item['dataVolumeClaimSpec']['resources']['requests']['storage'] = '1Gi'
    cluster['spec'].pop('monitoring', None)
    config = cluster['spec']['patroni']['dynamicConfiguration']
    config.update(synchronous_mode=False, synchronous_mode_strict=False, synchronous_node_count=0)
    for repo in cluster['spec']['backups']['pgbackrest']['repos']:
        repo.pop('schedules', None)
        repo['volume']['volumeClaimSpec']['resources']['requests']['storage'] = '1Gi'
    cluster['spec']['users'][0]['options'] = (
        'NOSUPERUSER NOCREATEDB NOCREATEROLE CONNECTION LIMIT 45'
    )
    apply(cluster)
    # PGO and restore jobs retain their existing API policy; access to the clone
    # is allowed solely for its owned test namespace.
    apply(
        {
            'apiVersion': 'networking.k8s.io/v1',
            'kind': 'NetworkPolicy',
            'metadata': {'name': 'secrets-test-access', 'namespace': 'postgres-ha'},
            'spec': {
                'podSelector': {
                    'matchLabels': {'postgres-operator.crunchydata.com/cluster': TEST_CLUSTER}
                },
                'policyTypes': ['Ingress'],
                'ingress': [
                    {
                        'from': [
                            {
                                'namespaceSelector': {
                                    'matchLabels': {
                                        'kubernetes.io/metadata.name': TEST_NS,
                                        'odoo.io/secrets-test': 'true',
                                    }
                                }
                            }
                        ],
                        'ports': [{'protocol': 'TCP', 'port': 5432}],
                    }
                ],
            },
        }
    )
    wait(lambda: healthy(TEST_CLUSTER, count=1), timeout=300)
    wait(
        lambda: (
            a.sql("SELECT count(*) FROM pg_database WHERE datname='odoo_dev'", cluster=TEST_CLUSTER)
            == '1'
        ),
        timeout=60,
    )
    a.sql('DROP DATABASE odoo_dev WITH(FORCE)', cluster=TEST_CLUSTER)
    a.sql('CREATE DATABASE odoo_dev OWNER odoo', cluster=TEST_CLUSTER)
    from database import primary

    module_spec = importlib.util.spec_from_file_location(
        'restore_archive', b.ROOT / 'scripts/restore-archive.py'
    )
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    module.restore_archive(
        lambda argv, **kwargs: b.k(
            '-n',
            'postgres-ha',
            'exec',
            '-i',
            primary(TEST_CLUSTER),
            '-c',
            'database',
            '--',
            *argv,
            data=kwargs.get('input'),
            timeout=kwargs.get('timeout', 120),
        ),
        (Path(backup) / 'database.dump').read_bytes(),
    )
    a.sql('ALTER DATABASE odoo_dev OWNER TO odoo', cluster=TEST_CLUSTER)
    ca = base64.b64decode(
        b.obj('postgres-ha', 'secret', TEST_CLUSTER + '-cluster-cert')['data']['ca.crt']
    )
    b.private_write(b.PRIVATE / (TEST_CLUSTER + '-ca.crt'), ca)
    replace_secret(TEST_NS, 'odoo-ha-ca', {'ca.crt': ca})
    a.bootstrap_control(TEST_CLUSTER, namespace=TEST_NS, ttl=600, lead=300)
    a.sql('ALTER ROLE odoo NOLOGIN', cluster=TEST_CLUSTER)
    passwords = json.loads((b.PRIVATE / ('control-' + TEST_CLUSTER + '.json')).read_text())
    for name in ('guard', 'rotator'):
        replace_secret(TEST_NS, name + '-db', {'password': passwords[name]})
    replace_secret(TEST_NS, 'postgres-ca', {'ca.crt': ca})


def test_eso():
    clone_network()
    from secrets_platform import eso_values

    lock = json.loads((b.ROOT / 'infrastructure/secrets/images.lock.json').read_text())
    values = eso_values(lock)
    values.update(
        fullnameOverride='secrets-test-eso',
        replicaCount=1,
        scopedNamespace=TEST_NS,
        installCRDs=False,
        serviceMonitor={'enabled': False},
        webhook={'create': False},
        certController={'create': False},
    )
    path = b.ROOT / '.state/secrets-test-eso-values.yaml'
    path.write_text(yaml.safe_dump(values, sort_keys=False))
    b.run(
        [
            b.ROOT / '.tools/bin/helm',
            '--kubeconfig',
            b.ROOT / '.state/kubeconfig',
            'upgrade',
            '--install',
            'secrets-test-eso',
            b.ROOT / 'vendor/external-secrets-2.12.0.tgz',
            '-n',
            TEST_NS,
            '-f',
            path,
            '--wait',
            '--timeout=120s',
        ],
        timeout=140,
    )
    for name in ('eso-odoo', 'secret-rotator'):
        apply(
            {
                'apiVersion': 'v1',
                'kind': 'ServiceAccount',
                'metadata': {'name': name, 'namespace': TEST_NS},
                'automountServiceAccountToken': False,
            }
        )
    apply(
        {
            'apiVersion': 'rbac.authorization.k8s.io/v1',
            'kind': 'Role',
            'metadata': {'name': 'test-eso-token', 'namespace': TEST_NS},
            'rules': [
                {
                    'apiGroups': [''],
                    'resources': ['serviceaccounts/token'],
                    'resourceNames': ['eso-odoo'],
                    'verbs': ['create'],
                }
            ],
        }
    )
    apply(
        {
            'apiVersion': 'rbac.authorization.k8s.io/v1',
            'kind': 'RoleBinding',
            'metadata': {'name': 'test-eso-token', 'namespace': TEST_NS},
            'roleRef': {
                'apiGroup': 'rbac.authorization.k8s.io',
                'kind': 'Role',
                'name': 'test-eso-token',
            },
            'subjects': [
                {'kind': 'ServiceAccount', 'name': 'secrets-test-eso', 'namespace': TEST_NS}
            ],
        }
    )
    replace_secret(TEST_NS, 'openbao-ca', {'ca.crt': (b.PRIVATE / 'ca.crt').read_bytes()})
    for document in a.external_secret_resources(
        TEST_NS, 'https://bao-test.secrets-test.svc:8200', 'odoo-test-eso'
    ):
        apply(document)


def clone_network():
    apply(
        {
            'apiVersion': 'networking.k8s.io/v1',
            'kind': 'NetworkPolicy',
            'metadata': {'name': 'test-eso-bao', 'namespace': TEST_NS},
            'spec': {
                'podSelector': {'matchLabels': {'app.kubernetes.io/name': 'external-secrets'}},
                'policyTypes': ['Egress'],
                'egress': [
                    {
                        'to': [{'podSelector': {'matchLabels': {'app': 'bao-test'}}}],
                        'ports': [{'port': 8200, 'protocol': 'TCP'}],
                    }
                ],
            },
        }
    )
    apply(
        {
            'apiVersion': 'networking.k8s.io/v1',
            'kind': 'NetworkPolicy',
            'metadata': {'name': 'test-database-dns', 'namespace': TEST_NS},
            'spec': {
                'podSelector': {},
                'policyTypes': ['Egress'],
                'egress': [
                    {
                        'to': [
                            {
                                'namespaceSelector': {
                                    'matchLabels': {'kubernetes.io/metadata.name': 'postgres-ha'}
                                },
                                'podSelector': {
                                    'matchLabels': {
                                        'postgres-operator.crunchydata.com/cluster': TEST_CLUSTER
                                    }
                                },
                            }
                        ],
                        'ports': [{'port': 5432, 'protocol': 'TCP'}],
                    },
                    {
                        'to': [
                            {
                                'namespaceSelector': {
                                    'matchLabels': {'kubernetes.io/metadata.name': 'kube-system'}
                                }
                            }
                        ],
                        'ports': [{'port': 53, 'protocol': p} for p in ('UDP', 'TCP')],
                    },
                ],
            },
        }
    )
    for name, selector in [
        ('eso', {'app.kubernetes.io/name': 'external-secrets'}),
        ('bao', {'app': 'bao-test'}),
    ]:
        apply(
            {
                'apiVersion': 'cilium.io/v2',
                'kind': 'CiliumNetworkPolicy',
                'metadata': {'name': 'test-' + name + '-api', 'namespace': TEST_NS},
                'spec': {
                    'endpointSelector': {'matchLabels': selector},
                    'egress': [
                        {
                            'toEntities': ['kube-apiserver'],
                            'toPorts': [
                                {
                                    'ports': [
                                        {'port': str(port), 'protocol': 'TCP'}
                                        for port in (443, 6443)
                                    ]
                                }
                            ],
                        }
                    ],
                },
            }
        )


def prepare_app_clone(backup, generation):
    root = Path('/srv/odoo/odoo-data/.secrets-test')
    root.mkdir(mode=0o700, exist_ok=True)
    with tarfile.open(Path(backup) / 'filestore.tar.gz') as archive:
        archive.extractall(root, filter='data')
    pv = {
        'apiVersion': 'v1',
        'kind': 'PersistentVolume',
        'metadata': {'name': 'secrets-test-data', 'labels': OWNED},
        'spec': {
            'capacity': {'storage': '5Gi'},
            'accessModes': ['ReadWriteMany'],
            'persistentVolumeReclaimPolicy': 'Retain',
            'storageClassName': '',
            'mountOptions': ['vers=4.1', 'hard'],
            'nfs': copy.deepcopy(b.obj('', 'pv', 'odoo-shared-data')['spec']['nfs']),
            'claimRef': {'name': 'odoo-shared-data', 'namespace': TEST_NS},
        },
    }
    apply(pv)
    apply(
        {
            'apiVersion': 'v1',
            'kind': 'PersistentVolumeClaim',
            'metadata': {'name': 'odoo-shared-data', 'namespace': TEST_NS},
            'spec': {
                'accessModes': ['ReadWriteMany'],
                'storageClassName': '',
                'volumeName': 'secrets-test-data',
                'resources': {'requests': {'storage': '5Gi'}},
            },
        }
    )
    source = b.obj('odoo-dev', 'secret', 'odoo-credentials')
    replace_secret(
        TEST_NS,
        'odoo-credentials',
        {key: base64.b64decode(value) for key, value in source['data'].items()},
    )
    values = yaml.safe_load((b.ROOT / 'gitops/environments/dev/odoo.yaml').read_text())
    values['storage']['dataDir'] = '/var/lib/odoo/.secrets-test'
    values['database'].update(
        host=TEST_CLUSTER + '-primary.postgres-ha.svc',
        credentialsMode='rotating',
        existingSecret='odoo-ha-database',
    )
    values['database']['networkPeer']['podSelector']['matchLabels'] = {
        'postgres-operator.crunchydata.com/cluster': TEST_CLUSTER
    }
    path = b.ROOT / '.state/secrets-test-odoo-values.yaml'
    path.write_text(yaml.safe_dump(values, sort_keys=False))
    rendered = b.run(
        [
            b.ROOT / '.tools/bin/helm',
            'template',
            'odoo',
            b.ROOT / 'helm/odoo',
            '-n',
            TEST_NS,
            '-f',
            path,
        ]
    )
    documents = list(yaml.safe_load_all(rendered))
    for document in documents:
        if not document:
            continue
        document['metadata']['namespace'] = TEST_NS
        if document['kind'] == 'Deployment':
            document['spec']['template']['metadata'].setdefault('annotations', {})[
                'odoo.io/credential-generation'
            ] = generation
        apply(document)
    edge = yaml.safe_load((b.ROOT / 'helm/odoo-edge/values.yaml').read_text())
    edge.update(backendNamespace=TEST_NS, serviceType='ClusterIP')
    edge_path = b.ROOT / '.state/secrets-test-edge-values.yaml'
    edge_path.write_text(yaml.safe_dump(edge, sort_keys=False))
    for document in yaml.safe_load_all(
        b.run(
            [
                b.ROOT / '.tools/bin/helm',
                'template',
                'odoo-edge',
                b.ROOT / 'helm/odoo-edge',
                '-n',
                TEST_NS,
                '-f',
                edge_path,
            ]
        )
    ):
        if document and document['kind'] != 'NetworkPolicy':
            document['metadata']['namespace'] = TEST_NS
            apply(document)
    # The trial proxy and web/cron are co-located in an isolated namespace.
    apply(
        {
            'apiVersion': 'networking.k8s.io/v1',
            'kind': 'NetworkPolicy',
            'metadata': {'name': 'test-internal', 'namespace': TEST_NS},
            'spec': {
                'podSelector': {},
                'policyTypes': ['Ingress', 'Egress'],
                'ingress': [
                    {
                        'from': [
                            {
                                'namespaceSelector': {
                                    'matchLabels': {'kubernetes.io/metadata.name': TEST_NS}
                                }
                            }
                        ]
                    }
                ],
                'egress': [
                    {
                        'to': [
                            {
                                'namespaceSelector': {
                                    'matchLabels': {'kubernetes.io/metadata.name': TEST_NS}
                                }
                            }
                        ]
                    },
                    {
                        'to': [
                            {
                                'namespaceSelector': {
                                    'matchLabels': {'kubernetes.io/metadata.name': 'postgres-ha'}
                                },
                                'podSelector': {
                                    'matchLabels': {
                                        'postgres-operator.crunchydata.com/cluster': TEST_CLUSTER
                                    }
                                },
                            }
                        ],
                        'ports': [{'protocol': 'TCP', 'port': 5432}],
                    },
                    {
                        'to': [
                            {
                                'namespaceSelector': {
                                    'matchLabels': {'kubernetes.io/metadata.name': 'kube-system'}
                                }
                            }
                        ],
                        'ports': [
                            {'protocol': protocol, 'port': 53} for protocol in ('UDP', 'TCP')
                        ],
                    },
                ],
            },
        }
    )
    apply(
        {
            'apiVersion': 'cilium.io/v2',
            'kind': 'CiliumNetworkPolicy',
            'metadata': {'name': 'test-api', 'namespace': TEST_NS},
            'spec': {
                'endpointSelector': {
                    'matchExpressions': [
                        {
                            'key': 'app.kubernetes.io/name',
                            'operator': 'In',
                            'values': ['external-secrets'],
                        }
                    ]
                },
                'egress': [
                    {
                        'toEntities': ['kube-apiserver'],
                        'toPorts': [
                            {
                                'ports': [
                                    {'port': '443', 'protocol': 'TCP'},
                                    {'port': '6443', 'protocol': 'TCP'},
                                ]
                            }
                        ],
                    }
                ],
            },
        }
    )
    apply(
        {
            'apiVersion': 'cilium.io/v2',
            'kind': 'CiliumNetworkPolicy',
            'metadata': {'name': 'test-bao-api', 'namespace': TEST_NS},
            'spec': {
                'endpointSelector': {'matchLabels': {'app': 'bao-test'}},
                'egress': [
                    {
                        'toEntities': ['kube-apiserver'],
                        'toPorts': [
                            {
                                'ports': [
                                    {'port': '443', 'protocol': 'TCP'},
                                    {'port': '6443', 'protocol': 'TCP'},
                                ]
                            }
                        ],
                    }
                ],
            },
        }
    )
    for deployment in ('odoo', 'odoo-cron', 'odoo-edge'):
        b.k(
            '-n',
            TEST_NS,
            'rollout',
            'status',
            'deployment/' + deployment,
            '--timeout=180s',
            timeout=190,
        )
    return values


def install_test_guards():
    cfg = a.settings('guard')
    cfg['database_host'] = TEST_CLUSTER + '-primary.postgres-ha.svc'
    cfg['application_namespace'] = TEST_NS
    apply(
        {
            'apiVersion': 'v1',
            'kind': 'ConfigMap',
            'metadata': {'name': 'secret-worker-test', 'namespace': TEST_NS},
            'data': {
                'worker.py': (b.ROOT / 'infrastructure/secrets/worker.py').read_text(),
                'guard.json': json.dumps(cfg),
            },
        }
    )
    for document in a.runtime_resources():
        if document['kind'] == 'ConfigMap' or document['metadata']['name'] != 'secret-expiry-guard':
            continue
        document = copy.deepcopy(document)
        document['metadata']['namespace'] = TEST_NS
        if document['kind'] == 'Deployment':
            for volume in document['spec']['template']['spec']['volumes']:
                if volume['name'] == 'code':
                    volume['configMap']['name'] = 'secret-worker-test'
        apply(document)
    b.k(
        '-n',
        TEST_NS,
        'rollout',
        'status',
        'deployment/secret-expiry-guard',
        '--timeout=120s',
        timeout=130,
    )


def create_heartbeat(client):
    name = 'Odoo HA heartbeat'
    ids = client.call('ir.cron', 'search', [[['name', '=', name]]])
    code = 'env.cr.execute("INSERT INTO ir_config_parameter (key,value) VALUES (\'odoo.ha.cron_heartbeat\',extract(epoch from clock_timestamp())::text) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value")'
    if not ids:
        model = client.call('ir.model', 'search', [[['model', '=', 'ir.config_parameter']]])[0]
        client.call(
            'ir.cron',
            'create',
            [
                {
                    'name': name,
                    'model_id': model,
                    'state': 'code',
                    'code': code,
                    'interval_number': 1,
                    'interval_type': 'minutes',
                    'nextcall': time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(time.time() - 60)),
                }
            ],
        )
    a.sql("SELECT pg_notify('cron_trigger','odoo_dev')", cluster=TEST_CLUSTER)


def app_client(url):
    spec = importlib.util.spec_from_file_location(
        'application_ha_tests', b.ROOT / 'tests/application/ha.py'
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Client(url, request_timeout=30).authenticate()


def base_snapshot(client):
    partners = client.call(
        'res.partner',
        'search_read',
        [[['name', '=', 'Synthetic Odoo Customer']]],
        {'fields': ['id']},
    )
    leads = client.call(
        'crm.lead',
        'search_read',
        [[['name', '=', 'Synthetic pipeline opportunity']]],
        {'fields': ['id']},
    )
    attachments = client.call(
        'ir.attachment',
        'search_read',
        [[['name', '=', 'odoo-fixture.txt']]],
        {'fields': ['id', 'checksum']},
    )
    if len(partners) != 1 or len(leads) != 1 or len(attachments) != 1:
        raise AssertionError('Expected baseline fixtures')
    with client.opener.open(
        client.url + '/web/content/' + str(attachments[0]['id']) + '?download=true', timeout=10
    ) as response:
        digest = hashlib.sha256(response.read()).hexdigest()
    return {
        'partner_id': partners[0]['id'],
        'lead_id': leads[0]['id'],
        'attachment_id': attachments[0]['id'],
        'attachment_sha256': digest,
    }


def clone_worker():
    stack = contextlib.ExitStack()
    try:
        address = stack.enter_context(b.forward('bao-test', TEST_NS))
        worker = stack.enter_context(a.local_worker(TEST_CLUSTER, TEST_NS, address))
        return stack, worker
    except BaseException:
        stack.close()
        raise


def db_login(worker, credential, database='odoo_dev'):
    return worker.login_connection(credential, database)


def rejected_login(worker, credential):
    try:
        connection = db_login(worker, credential)
    except psycopg2.OperationalError:
        return True
    connection.close()
    raise AssertionError('A retired credential still authenticates')


def preauthentication_expiry_test():
    """A server backend opened before issuance must still expire on time."""
    if TEST_CLUSTER == 'odoo-ha':
        raise ValueError('Short lifetimes require an isolated cluster')
    a.sql(
        'UPDATE secret_control.configuration SET ttl_seconds=60,lead_seconds=10',
        a.CONTROL,
        TEST_CLUSTER,
    )
    from database import primary

    with b.forward(primary(TEST_CLUSTER), 'postgres-ha', 5432) as address:
        remote = socket.create_connection(('127.0.0.1', int(address.rsplit(':', 1)[1])), timeout=5)
        remote.sendall(struct.pack('!II', 8, 80877103))
        assert remote.recv(1) == b'S'
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        stop = threading.Event()
        errors = []

        def bridge():
            client = None
            try:
                client, _ = listener.accept()
                request = b''
                while len(request) < 8:
                    request += client.recv(8 - len(request))
                assert request == struct.pack('!II', 8, 80877103)
                client.sendall(b'S')
                while not stop.is_set():
                    for source in select.select([client, remote], [], [], 0.1)[0]:
                        data = source.recv(65536)
                        if not data:
                            return
                        (remote if source is client else client).sendall(data)
            except OSError:
                if not stop.is_set():
                    errors.append('Proxy transport failed')
            finally:
                if client:
                    client.close()

        thread = threading.Thread(target=bridge, daemon=True)
        thread.start()
        connection = None
        try:
            with a.local_worker(
                TEST_CLUSTER, TEST_NS, bao_address='https://not-used.invalid'
            ) as worker:
                password = secrets.token_urlsafe(36)
                generation = secrets.token_hex(16)
                meta = worker.sql(
                    'SELECT secret_control.issue(%s,%s,%s)',
                    ('odoo_login_a', generation, a.w.scram_verifier(password)),
                )
                credential = {
                    'username': 'odoo_login_a',
                    'password': password,
                    'generation': generation,
                    'issued_at': meta['issued_at'],
                    'expires_at': meta['expires_at'],
                }
                connection = psycopg2.connect(
                    host=worker.settings['database_host'],
                    hostaddr='127.0.0.1',
                    port=listener.getsockname()[1],
                    dbname='odoo_dev',
                    user=credential['username'],
                    password=password,
                    sslmode='verify-full',
                    sslrootcert=worker.settings['database_ca'],
                    connect_timeout=5,
                )
                started = float(
                    a.sql(
                        'SELECT extract(epoch FROM backend_start) FROM pg_stat_activity WHERE pid='
                        + str(connection.get_backend_pid()),
                        cluster=TEST_CLUSTER,
                    )
                )
                assert started < a.w.utc(meta['issued_at']).timestamp()
                connection.autocommit = True
                with connection.cursor() as cursor:
                    cursor.execute('CREATE TABLE odoo_pre_auth_expiry(id integer PRIMARY KEY)')
                    cursor.execute('INSERT INTO odoo_pre_auth_expiry VALUES(1)')
                connection.autocommit = False
                with connection.cursor() as cursor:
                    cursor.execute('INSERT INTO odoo_pre_auth_expiry VALUES(2)')
                deadline = a.w.utc(meta['expires_at']).timestamp()
                while time.time() < deadline:
                    time.sleep(min(0.2, deadline - time.time()))
                while time.time() < deadline + 5:
                    try:
                        with connection.cursor() as cursor:
                            cursor.execute('SELECT 1')
                    except (psycopg2.OperationalError, psycopg2.InterfaceError):
                        break
                    time.sleep(0.05)
                else:
                    raise AssertionError('Pre-issuance backend survived its expiration deadline')
                seconds = time.time() - deadline
                rejected_login(worker, credential)
                assert (
                    a.sql(
                        'SELECT array_agg(id ORDER BY id) FROM odoo_pre_auth_expiry',
                        'odoo_dev',
                        TEST_CLUSTER,
                    )
                    == '{1}'
                )
                worker.sql('SELECT secret_control.abort(%s)', (generation,))
                b.record(
                    'pre-authentication-expiry',
                    backend_started_before_issuance=True,
                    termination_after_deadline_seconds=round(seconds, 3),
                    committed_row_preserved=True,
                    open_transaction_rolled_back=True,
                    fresh_login_rejected=True,
                )
        finally:
            if connection:
                connection.close()
            stop.set()
            listener.close()
            remote.close()
            thread.join(2)


def app_forward(namespace=TEST_NS):
    pod = json.loads(b.k('-n', namespace, 'get', 'pods', '-l', 'app=odoo-edge', '-o', 'json'))[
        'items'
    ]
    ready = next(
        p
        for p in pod
        if not p['metadata'].get('deletionTimestamp')
        and all(c.get('ready') for c in p['status'].get('containerStatuses', []))
    )
    return b.forward(ready['metadata']['name'], namespace, 8080)


def initialize_clone(backup):
    stack, worker = clone_worker()
    with stack:
        credential = worker.issue()
        worker.publish(credential)
        prepare_app_clone(backup, credential['generation'])
        with app_forward() as address:
            client = app_client(address.replace('https:', 'http:'))
            baseline = base_snapshot(client)
            create_heartbeat(client)
        worker.rotate(force=True)
        worker.sql('SELECT secret_control.enable(%s)', (credential['generation'],))
        b.record(
            'isolated-initial-migration',
            generation=credential['generation'],
            baseline=baseline,
            owner_login=False,
        )


def rapid_rotations():
    with app_forward() as address:
        client = app_client(address.replace('https:', 'http:'))
        baseline = base_snapshot(client)
        token = 'secrets-rotation-' + secrets.token_hex(8)
        partner = client.call('res.partner', 'create', [{'name': token}])
        body = b'acknowledged shared filestore across credential generations'
        attachment = client.call(
            'ir.attachment',
            'create',
            [
                {
                    'name': token,
                    'res_model': 'res.partner',
                    'res_id': partner,
                    'datas': base64.b64encode(body).decode(),
                }
            ],
        )
        generations = []
        roles = []
        try:
            for index in range(2):
                stack, worker = clone_worker()
                with stack:
                    state = worker.status()
                    old = worker.get_credentials(state['configuration']['active_role'])
                    started = time.monotonic()
                    worker.rotate(force=True)
                    state = worker.status()
                    credential = worker.get_credentials(state['configuration']['active_role'])
                    generations.append(credential['generation'])
                    roles.append(credential['username'])
                    worker.validate_login(credential)
                    rejected_login(worker, old)
                    assert not state['operation']
                    assert (
                        a.sql(
                            "SELECT sum(rolconnlimit) FROM pg_roles WHERE rolname IN ('odoo_login_a','odoo_login_b')",
                            cluster=TEST_CLUSTER,
                        )
                        == '45'
                    )
                    connection = db_login(worker, credential)
                    try:
                        with connection.cursor() as cursor:
                            cursor.execute('CREATE TABLE odoo_rotation_owner_check(id integer)')
                            cursor.execute(
                                "SELECT pg_get_userbyid(relowner) FROM pg_class WHERE relname='odoo_rotation_owner_check'"
                            )
                            assert cursor.fetchone()[0] == 'odoo'
                        connection.rollback()
                    finally:
                        connection.close()
                    try:
                        db_login(worker, credential, a.CONTROL)
                    except psycopg2.OperationalError:
                        pass
                    else:
                        raise AssertionError('Application accessed the rotation control database')
                    b.record(
                        'isolated-rotation-' + str(index + 1),
                        generation=credential['generation'],
                        role=credential['username'],
                        seconds=round(time.monotonic() - started, 3),
                        previous_login_rejected=True,
                        ownership='odoo',
                        aggregate_connection_budget=45,
                    )
                client.session()
                assert base_snapshot(client) == baseline
                assert (
                    client.call('res.partner', 'read', [[partner]], {'fields': ['name']})[0]['name']
                    == token
                )
                with client.opener.open(
                    client.url + '/web/content/' + str(attachment) + '?download=true', timeout=10
                ) as response:
                    assert response.read() == body
            assert roles == ['odoo_login_b', 'odoo_login_a'] and len(set(generations)) == 2
            b.record(
                'isolated-two-rotations',
                roles=['odoo_login_a', *roles],
                cookie_preserved=True,
                acknowledged_records_preserved=True,
                attachment_sha256=hashlib.sha256(body).hexdigest(),
                baseline=baseline,
            )
        finally:
            client.call('ir.attachment', 'unlink', [[attachment]])
            client.call('res.partner', 'unlink', [[partner]])


def expiry_tests():
    # Only new, un-published credentials in the owned clone get a short TTL.
    # The running clone retains its existing valid generation throughout.
    stack, worker = clone_worker()
    with stack:
        active = worker.get_credentials(worker.status()['configuration']['active_role'])
        a.sql(
            'UPDATE secret_control.configuration SET ttl_seconds=60,lead_seconds=10',
            a.CONTROL,
            TEST_CLUSTER,
        )
        connection = db_login(worker, active)
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    'CREATE TABLE IF NOT EXISTS odoo_expiry_probe(id integer PRIMARY KEY, value text)'
                )
            connection.commit()
        finally:
            connection.close()
        try:
            for guards in (1, 0):
                b.k(
                    '-n',
                    TEST_NS,
                    'scale',
                    'deployment/secret-expiry-guard',
                    '--replicas=' + str(guards),
                )
                b.wait(
                    lambda: (
                        len(
                            json.loads(
                                b.k(
                                    '-n',
                                    TEST_NS,
                                    'get',
                                    'pods',
                                    '-l',
                                    'app.kubernetes.io/name=secret-expiry-guard',
                                    '-o',
                                    'json',
                                )
                            )['items']
                        )
                        == guards
                    ),
                    60,
                )
                credential = worker.issue()
                committed = db_login(worker, credential)
                pending = db_login(worker, credential)
                with committed.cursor() as cursor:
                    cursor.execute(
                        'INSERT INTO odoo_expiry_probe VALUES(%s,%s)', (guards + 10, 'acknowledged')
                    )
                committed.commit()
                committed.close()
                with pending.cursor() as cursor:
                    cursor.execute('SELECT pg_backend_pid()')
                    pid = cursor.fetchone()[0]
                    cursor.execute(
                        'INSERT INTO odoo_expiry_probe VALUES(%s,%s)',
                        (guards + 20, 'must rollback'),
                    )
                expires = a.w.utc(credential['expires_at']).timestamp()
                while time.time() < expires:
                    time.sleep(min(0.25, expires - time.time()))
                if guards:
                    b.wait(
                        lambda: (
                            a.sql(
                                'SELECT count(*) FROM pg_stat_activity WHERE pid=' + str(pid),
                                cluster=TEST_CLUSTER,
                            )
                            == '0'
                        ),
                        5,
                        0.1,
                    )
                    delay = time.time() - expires
                    rejected_login(worker, credential)
                    b.record(
                        'isolated-hard-expiry',
                        seconds_after_deadline=round(delay, 3),
                        surviving_guards=1,
                        new_login_rejected=True,
                        open_transaction_terminated=True,
                    )
                else:
                    time.sleep(5.2)
                    assert (
                        a.sql(
                            'SELECT count(*) FROM pg_stat_activity WHERE pid=' + str(pid),
                            cluster=TEST_CLUSTER,
                        )
                        == '1'
                    )
                    rejected_login(worker, credential)
                    b.record(
                        'isolated-both-guards-down',
                        expected_guarantee_loss_observed=True,
                        expired_open_connection_survived=True,
                        new_login_rejected=True,
                        production_guards_unchanged=True,
                    )
                    b.k('-n', TEST_NS, 'scale', 'deployment/secret-expiry-guard', '--replicas=2')
                    b.k(
                        '-n',
                        TEST_NS,
                        'rollout',
                        'status',
                        'deployment/secret-expiry-guard',
                        '--timeout=120s',
                        timeout=130,
                    )
                    b.wait(
                        lambda: (
                            a.sql(
                                'SELECT count(*) FROM pg_stat_activity WHERE pid=' + str(pid),
                                cluster=TEST_CLUSTER,
                            )
                            == '0'
                        ),
                        5,
                        0.1,
                    )
                pending.close()
                worker.sql('SELECT secret_control.abort(%s)', (credential['generation'],))
                assert (
                    a.sql(
                        'SELECT count(*) FROM odoo_expiry_probe WHERE id=' + str(guards + 10),
                        database='odoo_dev',
                        cluster=TEST_CLUSTER,
                    )
                    == '1'
                )
                assert (
                    a.sql(
                        'SELECT count(*) FROM odoo_expiry_probe WHERE id=' + str(guards + 20),
                        database='odoo_dev',
                        cluster=TEST_CLUSTER,
                    )
                    == '0'
                )
            b.record(
                'isolated-expiry-transactions',
                all_acknowledged_commits_preserved=True,
                unfinished_transactions_rolled_back=True,
                expired_credentials_never_extended=True,
            )
        finally:
            b.k('-n', TEST_NS, 'scale', 'deployment/secret-expiry-guard', '--replicas=2')
            a.sql(
                'UPDATE secret_control.configuration SET ttl_seconds=600,lead_seconds=300',
                a.CONTROL,
                TEST_CLUSTER,
            )
            operation = worker.status()['operation']
            if operation:
                worker.sql('SELECT secret_control.abort(%s)', (operation['generation'],))
            a.sql(
                'DROP TABLE IF EXISTS odoo_expiry_probe', database='odoo_dev', cluster=TEST_CLUSTER
            )


def isolated_crash_resume():
    for checkpoint in ('ISSUED', 'PUBLISHED', 'ROLLOUT_STARTED'):
        stack, worker = clone_worker()
        with stack:
            credential = worker.issue()
            if checkpoint != 'ISSUED':
                worker.publish(credential)
            if checkpoint == 'ROLLOUT_STARTED':
                worker.kube(
                    '/apis/apps/v1/namespaces/' + TEST_NS + '/deployments/odoo',
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
            generation = credential['generation']
        stack, resumed = clone_worker()
        with stack:
            assert resumed.status()['operation']['generation'] == generation
            assert resumed.rotate(force=True)
            assert resumed.status()['configuration']['active_generation'] == generation
            assert not resumed.status()['operation']
        b.record(
            'isolated-controller-resume-' + checkpoint.lower(),
            generation=generation,
            same_operation_resumed=True,
            inconsistent_generation_published=False,
        )


def load_during_rotation():
    duration = 600
    # Give future isolated generations enough time for the measured ~200-second
    # graceful drain. Existing deadlines are never rewritten.
    a.sql(
        'UPDATE secret_control.configuration SET ttl_seconds=900,lead_seconds=450',
        a.CONTROL,
        TEST_CLUSTER,
    )
    with app_forward() as address:
        url = address.replace('https:', 'http:')
        clients = [app_client(url) for _ in range(10)]
        token = 'secrets-load-' + secrets.token_hex(8)
        stop = threading.Event()
        lock = threading.Lock()
        acknowledged = []
        errors = []
        latencies = []
        started = time.monotonic()
        rotations = []

        def user(index, client):
            while not stop.is_set():
                tick = time.monotonic()
                try:
                    client.session()
                    identifier = client.call(
                        'crm.lead',
                        'create',
                        [{'name': token + '-' + str(index), 'type': 'opportunity'}],
                    )
                    with lock:
                        acknowledged.append(identifier)
                    attachment = client.call(
                        'ir.attachment',
                        'create',
                        [
                            {
                                'name': token,
                                'res_model': 'crm.lead',
                                'res_id': identifier,
                                'datas': base64.b64encode(b'rotation load attachment').decode(),
                            }
                        ],
                    )
                    with client.opener.open(
                        url + '/web/content/' + str(attachment) + '?download=true', timeout=30
                    ) as response:
                        assert response.read() == b'rotation load attachment'
                    client.call('ir.attachment', 'unlink', [[attachment]])
                    with lock:
                        latencies.append(time.monotonic() - tick)
                except Exception as error:
                    with lock:
                        errors.append(
                            {
                                'seconds': round(time.monotonic() - started, 3),
                                'type': type(error).__name__,
                            }
                        )
                stop.wait(2)

        threads = [
            threading.Thread(target=user, args=(i, c), daemon=True) for i, c in enumerate(clients)
        ]
        for thread in threads:
            thread.start()
        fault_time = None
        recovery_time = None
        try:
            while time.monotonic() - started < duration:
                stack, worker = clone_worker()
                with stack:
                    if worker.rotate(force=not rotations):
                        rotations.append(worker.status()['configuration']['active_generation'])
                if fault_time is None and time.monotonic() - started > 120:
                    pod = json.loads(
                        b.k(
                            '-n',
                            TEST_NS,
                            'get',
                            'pods',
                            '-l',
                            'odoo.io/component=server',
                            '-o',
                            'json',
                        )
                    )['items'][0]
                    fault_time = time.monotonic()
                    b.k('-n', TEST_NS, 'delete', 'pod', pod['metadata']['name'], '--wait=false')
                    b.wait(clients[0].session, 10, 0.2)
                    recovery_time = time.monotonic() - fault_time
                stop.wait(5)
            stop.set()
            for thread in threads:
                thread.join(35)
            assert all(not thread.is_alive() for thread in threads)
            rows = clients[0].call(
                'crm.lead', 'search_read', [[['name', '=like', token + '%']]], {'fields': ['id']}
            )
            assert set(acknowledged) <= {r['id'] for r in rows}
            assert recovery_time is not None and recovery_time <= 10
            # A request already running during the deliberate fault may fail.
            allowed_start = fault_time - started if fault_time else -100
            unexpected = [
                e for e in errors if not allowed_start - 30 <= e['seconds'] <= allowed_start + 40
            ]
            assert not unexpected, 'Unexpected application errors during rotation'
            pods = json.loads(
                b.k('-n', TEST_NS, 'get', 'pods', '-l', 'app.kubernetes.io/name=odoo', '-o', 'json')
            )['items']
            assert not any(
                c.get('lastState', {}).get('terminated', {}).get('reason') == 'OOMKilled'
                for p in pods
                for c in p['status'].get('containerStatuses', [])
            )
            assert len(rotations) >= 2
            b.record(
                'isolated-load-rotation',
                users=10,
                seconds=round(time.monotonic() - started, 3),
                generations=rotations,
                acknowledged_preserved=len(acknowledged),
                errors=errors,
                web_recovery_seconds=round(recovery_time, 3),
                oom=False,
                max_operation_seconds=round(max(latencies), 3),
            )
        finally:
            stop.set()
            for thread in threads:
                thread.join(35)
            for model in ('ir.attachment', 'crm.lead'):
                ids = clients[0].call(model, 'search', [[['name', '=like', token + '%']]])
                if ids:
                    clients[0].call(model, 'unlink', [ids])


def module_upgrade_test():
    # Copy the live-shaped template so the job reads the same atomic credential
    # bundle and TLS CA as the clone's actual web/cron processes.
    saved = {
        name: b.obj(TEST_NS, 'deployment', name)['spec']['replicas']
        for name in ('odoo', 'odoo-cron')
    }
    try:
        for name in saved:
            b.k('-n', TEST_NS, 'scale', 'deployment/' + name, '--replicas=0')
        b.wait(
            lambda: (
                not json.loads(
                    b.k(
                        '-n',
                        TEST_NS,
                        'get',
                        'pods',
                        '-l',
                        'app.kubernetes.io/name=odoo',
                        '-o',
                        'json',
                    )
                )['items']
            ),
            120,
        )
        env = os.environ.copy()
        env.update(MODULE_NAMESPACE=TEST_NS, MODULES='crm', MODULE_ACTION='upgrade')
        result = subprocess.run(
            [sys.executable, str(b.ROOT / 'scripts/module-job.py')],
            env=env,
            capture_output=True,
            timeout=30,
        )
        if result.returncode:
            raise RuntimeError('Trial module job render failed')
        document = json.loads(result.stdout)
        apply(document)
        began = time.monotonic()
        b.k(
            '-n',
            TEST_NS,
            'wait',
            '--for=condition=complete',
            'job/odoo-module-upgrade',
            '--timeout=240s',
            timeout=245,
        )
        owners = a.sql(
            "SELECT DISTINCT pg_get_userbyid(relowner) FROM pg_class c JOIN pg_namespace n ON c.relnamespace=n.oid WHERE n.nspname='public' AND relname LIKE 'crm_%'",
            database='odoo_dev',
            cluster=TEST_CLUSTER,
        )
        assert owners == 'odoo'
        b.record(
            'isolated-module-upgrade',
            module='crm',
            owner='odoo',
            rotating_credentials=True,
            seconds=round(time.monotonic() - began, 3),
        )
    finally:
        b.k(
            '-n',
            TEST_NS,
            'delete',
            'job',
            'odoo-module-upgrade',
            '--ignore-not-found',
            '--wait=false',
        )
        for name, count in saved.items():
            b.k('-n', TEST_NS, 'scale', 'deployment/' + name, '--replicas=' + str(count))
        for name in saved:
            b.k(
                '-n',
                TEST_NS,
                'rollout',
                'status',
                'deployment/' + name,
                '--timeout=120s',
                timeout=125,
            )


def transfer_failure_tests():
    stack, worker = clone_worker()
    with stack:
        state = worker.status()
        original = worker.get_credentials(state['configuration']['active_role'])
        if state['operation']:
            raise RuntimeError('Failure injection requires a completed preceding rotation')
        original_deadline = original['expires_at']
        secret_uid = b.obj(TEST_NS, 'secret', 'odoo-ha-database')['metadata']['uid']
        a.sql(
            'UPDATE secret_control.configuration SET ttl_seconds=60,lead_seconds=10',
            a.CONTROL,
            TEST_CLUSTER,
        )
        b.k('-n', TEST_NS, 'scale', 'deployment/secrets-test-eso', '--replicas=0')
        b.wait(
            lambda: (
                not json.loads(
                    b.k(
                        '-n',
                        TEST_NS,
                        'get',
                        'pods',
                        '-l',
                        'app.kubernetes.io/name=external-secrets',
                        '-o',
                        'json',
                    )
                )['items']
            ),
            60,
        )
        candidate = None
        try:
            candidate = worker.issue()
            try:
                worker.publish(candidate)
            except TimeoutError:
                pass
            else:
                raise AssertionError('Publication succeeded with ESO stopped')
            assert worker.status()['configuration']['active_generation'] == original['generation']
            meta = next(
                g
                for g in worker.status()['generations']
                if g['generation'] == original['generation']
            )
            assert meta['expires_at'] == original_deadline
            rejected_login(worker, candidate)
            assert b.obj(TEST_NS, 'secret', 'odoo-ha-database')['metadata']['uid'] == secret_uid
            worker.sql('SELECT secret_control.abort(%s)', (candidate['generation'],))
        finally:
            if candidate and worker.status()['operation']:
                worker.sql('SELECT secret_control.abort(%s)', (candidate['generation'],))
            a.sql(
                'UPDATE secret_control.configuration SET ttl_seconds=900,lead_seconds=450',
                a.CONTROL,
                TEST_CLUSTER,
            )
            b.k('-n', TEST_NS, 'scale', 'deployment/secrets-test-eso', '--replicas=1')
            b.k(
                '-n',
                TEST_NS,
                'rollout',
                'status',
                'deployment/secrets-test-eso',
                '--timeout=120s',
                timeout=125,
            )
        # The expired staged version may now arrive in the Secret: no process is
        # restarted to consume it. Recover by a new generation, never extension.
        assert worker.rotate(force=True)
        pod = b.obj(TEST_NS, 'pod', 'bao-test')
        restored = {
            'apiVersion': 'v1',
            'kind': 'Pod',
            'metadata': {
                'name': 'bao-test',
                'namespace': TEST_NS,
                'labels': pod['metadata']['labels'],
            },
            'spec': pod['spec'],
        }
        active = worker.get_credentials(worker.status()['configuration']['active_role'])
        b.k('-n', TEST_NS, 'delete', 'pod', 'bao-test', '--wait=true', '--timeout=45s')
        before = worker.status()
        try:
            worker.bao_token = None
            try:
                worker.issue()
            except (OSError, urllib.error.URLError, RuntimeError):
                pass
            else:
                raise AssertionError('Issuance succeeded without OpenBao')
            after = worker.status()
            assert (
                after['configuration']['active_generation']
                == before['configuration']['active_generation']
            )
            assert (
                next(g for g in after['generations'] if g['generation'] == active['generation'])[
                    'expires_at'
                ]
                == active['expires_at']
            )
            assert b.obj(TEST_NS, 'secret', 'odoo-ha-database')['metadata']['uid'] == secret_uid
        finally:
            apply(restored)
            b.wait(
                lambda: b.obj(TEST_NS, 'pod', 'bao-test')['status'].get('phase') == 'Running', 120
            )
            with b.forward('bao-test', TEST_NS) as address:
                b.wait(lambda: not b.api(address, 'sys/seal-status')['sealed'], 60)
        b.record(
            'isolated-transfer-failures',
            eso_unavailable=True,
            bao_unavailable=True,
            deadline_extension=False,
            expired_candidate_rejected=True,
            existing_secret_retained=True,
            recovery_used_fresh_generation=True,
        )


def rollback_rehearsal():
    with app_forward() as address:
        client = app_client(address.replace('https:', 'http:'))
        baseline = base_snapshot(client)
        stack, worker = clone_worker()
        with stack:
            original = worker.get_credentials(worker.status()['configuration']['active_role'])
            value = b.obj(TEST_NS, 'deployment', 'odoo')
            selector = value['spec']['template']['spec'].get('nodeSelector')
            candidate = worker.issue()
            worker.publish(candidate)
            worker.kube(
                '/apis/apps/v1/namespaces/' + TEST_NS + '/deployments/odoo',
                'PATCH',
                {
                    'spec': {
                        'template': {
                            'spec': {'nodeSelector': {'odoo.io/nonexistent-test-node': 'true'}}
                        }
                    }
                },
            )
            try:
                try:
                    worker.rollout('odoo', candidate, timeout=12)
                except TimeoutError:
                    pass
                else:
                    raise AssertionError('Deliberately blocked rollout became healthy')
                client.session()
                worker.sql('SELECT secret_control.abort(%s)', (candidate['generation'],))
                worker.kube(
                    '/apis/apps/v1/namespaces/' + TEST_NS + '/deployments/odoo',
                    'PATCH',
                    {
                        'spec': {
                            'template': {
                                'spec': {'nodeSelector': selector},
                                'metadata': {
                                    'annotations': {
                                        'odoo.io/credential-generation': original['generation']
                                    }
                                },
                            }
                        }
                    },
                )
                # A fresh generation performs recovery; old deadlines stay fixed.
                assert worker.rotate(force=True)
                active = worker.get_credentials(worker.status()['configuration']['active_role'])
                assert active['generation'] not in (candidate['generation'], original['generation'])
                rejected_login(worker, candidate)
                client.session()
                assert base_snapshot(client) == baseline
                b.record(
                    'isolated-rollback',
                    blocked_rollout_observed=True,
                    recovery_generation=active['generation'],
                    fresh_password=True,
                    expired_or_aborted_password_reused=False,
                    cookie_preserved=True,
                    baseline=baseline,
                )
            finally:
                worker.kube(
                    '/apis/apps/v1/namespaces/' + TEST_NS + '/deployments/odoo',
                    'PATCH',
                    {'spec': {'template': {'spec': {'nodeSelector': selector}}}},
                )


def stale_snapshot_test():
    # Snapshot only the owned Bao clone. PostgreSQL's ledger is deliberately not
    # restored; it must remain the authority over generation and expiration.
    specification = importlib.util.spec_from_file_location(
        'bao_recovery', b.ROOT / 'scripts/openbao-recovery.py'
    )
    recovery = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(recovery)
    with b.forward('bao-test', TEST_NS) as address:
        try:
            auth = login(address, 'odoo-operator', 'bao-operator', 'openbao')
        except RuntimeError:
            # Older rehearsal instances predate the authenticated operator role.
            # This branch is restricted to the owned clone, never the live Bao.
            checked_namespace()
            if TEST_NS != 'secrets-test':
                raise
            root = recovery.recover(address, legacy=True)
            try:
                plain_api(
                    address,
                    'sys/policies/acl/odoo-operator',
                    {
                        'policy': 'path "sys/generate-root-token/attempt" { capabilities=["read","update","delete","sudo"] }\n'
                        'path "sys/generate-root-token/update" { capabilities=["update","sudo"] }'
                    },
                    root,
                )
                plain_api(
                    address,
                    'auth/kubernetes/role/odoo-operator',
                    {
                        'bound_service_account_names': ['bao-operator'],
                        'bound_service_account_namespaces': ['openbao'],
                        'audience': 'openbao',
                        'token_policies': ['odoo-operator'],
                        'token_ttl': '10m',
                        'token_max_ttl': '30m',
                    },
                    root,
                )
            finally:
                plain_api(address, 'auth/token/revoke-self', {}, root)
        bootstrap = login(address)
        request = urllib.request.Request(
            address + '/v1/sys/storage/raft/snapshot', headers={'X-Vault-Token': bootstrap}
        )
        with urllib.request.urlopen(
            request,
            context=ssl.create_default_context(cafile=str(b.PRIVATE / 'ca.crt')),
            timeout=30,
        ) as response:
            snapshot = response.read()
    with app_forward() as address:
        client = app_client(address.replace('https:', 'http:'))
        baseline = base_snapshot(client)
        stack, worker = clone_worker()
        with stack:
            if worker.status()['operation']:
                raise RuntimeError('Snapshot testing requires a completed rotation')
            assert worker.rotate(force=True)
            active = worker.get_credentials(worker.status()['configuration']['active_role'])
            secret = b.obj(TEST_NS, 'secret', 'odoo-ha-database')
            uid = secret['metadata']['uid']
            versions = [
                item['remoteRef']['version']
                for item in b.obj(TEST_NS, 'externalsecret', 'odoo-ha-database')['spec']['data']
            ]
            assert len(set(versions)) == 1
        with b.forward('bao-test', TEST_NS) as address:
            root = recover_clone_root(address)
            plain_api(address, 'sys/storage/raft/snapshot-force', token=root, raw=snapshot)
            # Restore invalidates the newly generated root token, which was not
            # in the earlier snapshot. It is never saved or used again.
        pod = b.obj(TEST_NS, 'pod', 'bao-test')
        restart_clone(
            {
                'apiVersion': 'v1',
                'kind': 'Pod',
                'metadata': {
                    'name': 'bao-test',
                    'namespace': TEST_NS,
                    'labels': pod['metadata']['labels'],
                },
                'spec': pod['spec'],
            }
        )
        with b.forward('bao-test', TEST_NS) as address:
            b.wait(
                lambda: (
                    not b.api(address, 'sys/seal-status')['sealed']
                    and b.api(address, 'sys/leader')['is_self']
                ),
                60,
            )
        b.k(
            '-n',
            TEST_NS,
            'annotate',
            'externalsecret',
            'odoo-ha-database',
            'force-sync=snapshot-rejection',
            '--overwrite',
        )
        b.wait(
            lambda: any(
                c['type'] == 'Ready' and c['status'] == 'False'
                for c in b.obj(TEST_NS, 'externalsecret', 'odoo-ha-database')
                .get('status', {})
                .get('conditions', [])
            ),
            60,
        )
        retained = b.obj(TEST_NS, 'secret', 'odoo-ha-database')
        assert retained['metadata']['uid'] == uid and retained['data'] == secret['data']
        client.session()
        assert base_snapshot(client) == baseline
        stack, worker = clone_worker()
        with stack:
            assert worker.status()['configuration']['active_generation'] == active['generation']
            assert worker.rotate(force=True)
            restored = worker.get_credentials(worker.status()['configuration']['active_role'])
            assert restored['generation'] != active['generation']
            worker.validate_login(restored)
            rejected_login(worker, active)
        client.session()
        assert base_snapshot(client) == baseline
        b.record(
            'isolated-stale-snapshot',
            old_version_publication_rejected=True,
            existing_secret_retained=True,
            postgres_generation_authoritative=True,
            recovery_generation=restored['generation'],
            old_password_republished=False,
            cookie_preserved=True,
            baseline=baseline,
        )


def live_bao_tests():
    with leader_forward() as address:
        auth = login(address)
        config = b.api(address, 'sys/storage/raft/configuration', token=auth)['data']['config'][
            'servers'
        ]
        if (
            len(config) != 3
            or sum(bool(s['leader']) for s in config) != 1
            or not all(s['voter'] for s in config)
        ):
            raise AssertionError('Expected three Raft voters and one leader')
        test_value = secrets.token_hex(16)
        b.api(address, 'kv/data/odoo-dev/odoo-db/sentinel', {'data': {'value': test_value}}, auth)
    for target in range(3):
        started = time.monotonic()
        before = b.obj('openbao', 'pod', 'openbao-' + str(target))['metadata']['uid']
        b.k('-n', 'openbao', 'delete', 'pod', 'openbao-' + str(target), '--wait=false')

        def recovered():
            with leader_forward() as address:
                auth = login(address)
                return (
                    b.api(address, 'kv/data/odoo-dev/odoo-db/sentinel', token=auth)['data']['data'][
                        'value'
                    ]
                    == test_value
                )

        b.wait(recovered, 30, 0.5)
        availability = time.monotonic() - started
        b.wait(
            lambda: (
                b.obj('openbao', 'pod', 'openbao-' + str(target))['metadata']['uid'] != before
                and b.obj('openbao', 'statefulset', 'openbao')['status'].get('readyReplicas') == 3
            ),
            120,
        )
        b.record(
            'openbao-member-restart-' + str(target),
            seconds=round(availability, 3),
            auto_unseal=True,
            manual_key_entry=False,
        )
    started = time.monotonic()
    before = {
        str(i): b.obj('openbao', 'pod', 'openbao-' + str(i))['metadata']['uid'] for i in range(3)
    }
    for target in range(3):
        b.k('-n', 'openbao', 'delete', 'pod', 'openbao-' + str(target), '--wait=false')
    b.wait(
        lambda: (
            all(
                b.obj('openbao', 'pod', 'openbao-' + str(i))['metadata']['uid'] != before[str(i)]
                for i in range(3)
            )
            and b.obj('openbao', 'statefulset', 'openbao')['status'].get('readyReplicas') == 3
        ),
        120,
    )
    b.status()
    b.record('openbao-all-restart', seconds=round(time.monotonic() - started, 3), auto_unseal=True)
    with leader_forward() as address:
        from secrets_platform import token

        eso = b.api(
            address,
            'auth/kubernetes/login',
            {'role': 'odoo-eso', 'jwt': token('eso-odoo', 'odoo-dev')},
        )['auth']['client_token']
        try:
            b.api(address, 'kv/data/odoo-dev/odoo-db/slot-a', token=eso)
        except RuntimeError as error:
            if 'HTTP 403' not in str(error):
                raise
        else:
            raise AssertionError('ESO read staging credentials')
        try:
            b.api(
                address,
                'auth/kubernetes/login',
                {'role': 'odoo-eso', 'jwt': token('default', 'odoo-dev')},
            )
        except RuntimeError as error:
            if 'HTTP 403' not in str(error):
                raise
        else:
            raise AssertionError('An unauthorized ServiceAccount authenticated')
        request = urllib.request.Request(address + '/v1/sys/seal-status')
        try:
            urllib.request.urlopen(request, context=ssl.create_default_context(), timeout=5)
        except urllib.error.URLError:
            pass
        else:
            raise AssertionError('Untrusted CA accepted')
    for principal in ('secret-rotator', 'secret-expiry-guard'):
        p = subprocess.run(
            b.K
            + [
                'auth',
                'can-i',
                'get',
                'secrets/openbao-seal',
                '-n',
                'openbao',
                '--as=system:serviceaccount:secrets-system:' + principal,
            ],
            capture_output=True,
        )
        if p.stdout.strip() != b'no':
            raise AssertionError('A worker can read the seal key')
    b.record(
        'openbao-auth-tls-rbac',
        unauthorized_identity_rejected=True,
        eso_staging_rejected=True,
        invalid_ca_rejected=True,
        workers_seal_key_access=False,
    )


def migrate():
    for name in (
        'isolated-two-rotations',
        'isolated-hard-expiry',
        'isolated-controller-resume-issued',
        'isolated-controller-resume-published',
        'isolated-controller-resume-rollout_started',
        'isolated-load-rotation',
        'openbao-restore',
        'isolated-transfer-failures',
        'isolated-rollback',
        'isolated-stale-snapshot',
        'live-application-prerequisite',
        'pre-authentication-expiry',
    ):
        path = b.EVIDENCE / (name + '.json')
        if not path.exists() or json.loads(path.read_text())['status'] != 'PASS':
            raise RuntimeError('Migration gate has not passed: ' + name)
    b.preflight(after_images=True)
    import ha

    module = importlib.util.spec_from_file_location(
        'odoo_runtime', b.ROOT / 'scripts/odoo-runtime.py'
    )
    runtime = importlib.util.module_from_spec(module)
    module.loader.exec_module(runtime)
    values_path = b.ROOT / 'gitops/environments/dev/odoo.yaml'
    original_text = values_path.read_text()
    original = yaml.safe_load(original_text)
    if original['database'].get('credentialsMode') == 'rotating':
        with a.local_worker(bao_address='https://not-used.invalid') as worker:
            state = worker.status()
            assert state['configuration']['enabled'] and not state['operation']
            secret = b.obj('odoo-dev', 'secret', 'odoo-ha-database')
            credential = json.loads(base64.b64decode(secret['data']['credentials.json']))
            assert credential['generation'] == state['configuration']['active_generation']
            worker.validate_login(credential)
        assert a.sql("SELECT rolcanlogin FROM pg_roles WHERE rolname='odoo'") == 'f'
        return b.record(
            'secrets-migration-idempotence',
            already_migrated=True,
            generation=credential['generation'],
            baseline=base_snapshot(app_client('http://127.0.0.1:8069')),
        )
    client = app_client('http://127.0.0.1:8069')
    baseline = base_snapshot(client)
    state_path = b.ROOT / '.state/secrets-migration.json'
    state = {'committed': False, 'original_values': original, 'baseline': baseline}
    b.private_write(state_path, json.dumps(state))
    committed = False
    started = time.monotonic()
    credential = None
    with ha.maintenance():
        try:
            b.run(['docker', 'stop', '--time=5', 'odoo-entry'], timeout=15)
            runtime.stop(timeout=60)
            database_baseline = ha.db_baseline('crunchy')
            ha.helper_start()
            try:
                backup, _ = ha.capture('crunchy', database_baseline, started)
            finally:
                ha.helper_stop()
            for document in a.external_secret_resources():
                apply(document)
            stack = contextlib.ExitStack()
            with stack:
                address = stack.enter_context(leader_forward())
                worker = stack.enter_context(a.local_worker(bao_address=address))
                credential = worker.issue()
                worker.publish(credential)
                assert ha.db_baseline('crunchy') == database_baseline, (
                    'Stopped database signatures changed during credential cutover'
                )
                values = copy.deepcopy(original)
                values['database']['credentialsMode'] = 'rotating'
                values['initialization']['enabled'] = False
                render_live_odoo(values, credential['generation'], cron=0)
                remaining = 240 - (time.monotonic() - started)
                if remaining < 30:
                    raise TimeoutError('Initial cutover budget exhausted')
                b.k(
                    '-n',
                    'odoo-dev',
                    'rollout',
                    'status',
                    'deployment/odoo',
                    '--timeout=' + str(int(remaining)) + 's',
                    timeout=remaining + 5,
                )
                worker.rollout(
                    'odoo', credential, timeout=max(10, 230 - (time.monotonic() - started))
                )
                worker.sql(
                    'SELECT secret_control.advance(%s,%s)', (credential['generation'], 'WEB_READY')
                )
                with app_forward('odoo-edge') as address:
                    private = app_client(address.replace('https:', 'http:'))
                    assert base_snapshot(private) == baseline
                    # The cookie obtained before cutover is reused privately.
                    client.url = address.replace('https:', 'http:')
                    client.session()
                if time.monotonic() - started >= 240:
                    raise TimeoutError('No rollback time remains')
                state.update(
                    committed=True,
                    generation=credential['generation'],
                    issued_at=credential['issued_at'],
                    expires_at=credential['expires_at'],
                    backup=str(backup),
                    committed_utc=dt.datetime.now(dt.timezone.utc).isoformat(),
                )
                b.private_write(state_path, json.dumps(state))
                committed = True
                a.sql(
                    "ALTER ROLE odoo NOLOGIN; SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename='odoo'"
                )
                cluster_path = b.ROOT / 'infrastructure/database-ha/cluster.yaml'
                cluster = yaml.safe_load(cluster_path.read_text())
                owner = next(user for user in cluster['spec']['users'] if user['name'] == 'odoo')
                owner['options'] = owner.get('options', '') + ' NOLOGIN'
                cluster_path.write_text(yaml.safe_dump(cluster, sort_keys=False))
                apply(cluster)
                values_path.write_text(yaml.safe_dump(values, sort_keys=False))
                render_live_odoo(
                    values, credential['generation'], cron=original['cron']['replicaCount']
                )
                worker.rotate(force=True)
                worker.sql('SELECT secret_control.enable(%s)', (credential['generation'],))
                b.private_write(
                    b.PRIVATE / 'observation-initial-credential.json', json.dumps(credential)
                )
            b.run(['docker', 'start', 'odoo-entry'], timeout=15)
            client.url = 'http://127.0.0.1:8069'
            b.wait(client.session, 15, 0.2)
            assert base_snapshot(client) == baseline
            elapsed = time.monotonic() - started
            b.record(
                'secrets-migration',
                seconds=round(elapsed, 3),
                target_seconds=300,
                within_target=elapsed <= 300,
                generation=credential['generation'],
                baseline=baseline,
                backup=str(backup),
                cookie_preserved=True,
                owner_login=False,
                live_ttl_seconds=86400,
            )
            if elapsed > 300:
                raise AssertionError('Initial cutover exceeded the five-minute objective')
        except BaseException:
            if committed:
                runtime.stop(timeout=60)
                b.record(
                    'secrets-migration',
                    'FAIL',
                    committed=True,
                    application_closed=True,
                    recovery='Use a fresh valid generation; the static owner credential must not be re-enabled',
                )
            else:
                runtime.stop(timeout=45)
                b.k(
                    '-n',
                    'odoo-dev',
                    'delete',
                    'externalsecret',
                    'odoo-ha-database',
                    '--ignore-not-found',
                )
                source = b.obj('postgres-ha', 'secret', 'odoo-ha-pguser-odoo')['data']['password']
                destination = b.obj('odoo-dev', 'secret', 'odoo-ha-database')
                destination['data'] = {'password': source}
                destination['metadata'].pop('annotations', None)
                b.k('replace', '-f', '-', data=json.dumps(destination).encode())
                values_path.write_text(original_text)
                render_live_odoo(original, None, cron=original['cron']['replicaCount'])
                b.k(
                    '-n',
                    'odoo-dev',
                    'rollout',
                    'status',
                    'deployment/odoo',
                    '--timeout=60s',
                    timeout=65,
                )
                b.run(['docker', 'start', 'odoo-entry'], timeout=15)
                if credential:
                    with a.local_worker() as worker:
                        worker.sql('SELECT secret_control.abort(%s)', (credential['generation'],))
                b.record(
                    'secrets-migration',
                    'FAIL',
                    committed=False,
                    source_restored=True,
                    seconds=round(time.monotonic() - started, 3),
                )
            raise


def render_live_odoo(values, generation, cron):
    temporary = copy.deepcopy(values)
    temporary['cron']['replicaCount'] = cron
    path = b.ROOT / '.state/secrets-odoo-values.yaml'
    path.write_text(yaml.safe_dump(temporary, sort_keys=False))
    rendered = b.run(
        [
            b.ROOT / '.tools/bin/helm',
            'template',
            'odoo',
            b.ROOT / 'helm/odoo',
            '-n',
            'odoo-dev',
            '-f',
            path,
        ]
    )
    for document in yaml.safe_load_all(rendered):
        if not document:
            continue
        document['metadata']['namespace'] = 'odoo-dev'
        if document['kind'] == 'Deployment' and generation:
            document['spec']['template']['metadata'].setdefault('annotations', {})[
                'odoo.io/credential-generation'
            ] = generation
        apply(document)


def test():
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(
            sig, lambda *_: (_ for _ in ()).throw(InterruptedError('Acceptance interrupted'))
        )
    cleanup()
    try:
        archives = [
            p.parent
            for p in (b.ROOT / 'backups').glob('*/manifest.json')
            if (p.parent / 'database.dump').exists() and (p.parent / 'filestore.tar.gz').exists()
        ]
        backup = max(archives, key=lambda p: (p / 'manifest.json').stat().st_mtime_ns)
        manifest = json.loads((backup / 'manifest.json').read_text())
        for name, digest in manifest['sha256'].items():
            assert hashlib.sha256((backup / name).read_bytes()).hexdigest() == digest
        checked_namespace()
        create_database_clone(backup)
        clone_network()
        install_test_guards()
        preauthentication_expiry_test()
        a.sql(
            'UPDATE secret_control.configuration SET ttl_seconds=600,lead_seconds=300',
            a.CONTROL,
            TEST_CLUSTER,
        )
        restore_openbao(keep=True)
        test_eso()
        initialize_clone(backup)
        rapid_rotations()
        expiry_tests()
        isolated_crash_resume()
        module_upgrade_test()
        load_during_rotation()
        transfer_failure_tests()
        rollback_rehearsal()
        stale_snapshot_test()
        b.record('isolated-acceptance', short_ttl_isolated=True, production_ttl_unchanged=True)
    except BaseException as error:
        b.record('isolated-acceptance', 'FAIL', error_type=type(error).__name__)
        raise
    finally:
        cleanup()


def observe():
    with (b.ROOT / '.state/secrets-observation.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            return observe_locked()
        except BaseException as error:
            path = b.EVIDENCE / 'real-24h-observation.json'
            if not path.exists() or json.loads(path.read_text())['status'] != 'FAIL':
                b.record('real-24h-observation', 'FAIL', error_type=type(error).__name__)
            raise
        finally:
            specification = importlib.util.spec_from_file_location(
                'secrets_report', b.ROOT / 'scripts/secrets-report.py'
            )
            report = importlib.util.module_from_spec(specification)
            specification.loader.exec_module(report)
            report.report()


def observe_locked():
    state_path = b.ROOT / '.state/secrets-migration.json'
    state = json.loads(state_path.read_text())
    if not state.get('committed'):
        raise RuntimeError('Real observation requires a committed live migration')
    initial = json.loads((b.PRIVATE / 'observation-initial-credential.json').read_text())
    observation_path = b.ROOT / '.state/secrets-observation.json'
    resuming = observation_path.exists()
    if resuming:
        observation = json.loads(observation_path.read_text())
        if observation['initial_generation'] != initial['generation']:
            raise RuntimeError('Observation belongs to another migration')
    else:
        observation = {
            'started_at': dt.datetime.now(dt.timezone.utc).isoformat(),
            'initial_generation': initial['generation'],
        }
        b.private_write(observation_path, json.dumps(observation))
    start = a.w.utc(observation['started_at']).timestamp()
    deadline = start + 86400
    specification = importlib.util.spec_from_file_location(
        'observation_client', b.ROOT / 'tests/application/ha.py'
    )
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    client = module.Client(request_timeout=30)
    cookie_path = b.PRIVATE / 'observation.cookies'
    if resuming and not cookie_path.exists():
        raise RuntimeError('The retained observation cookie is missing')
    jar = http.cookiejar.LWPCookieJar(str(cookie_path))
    client.jar = jar
    client.opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(jar)
    )
    if cookie_path.exists():
        jar.load(ignore_discard=True)
        client.uid = observation['uid']
        client.session()
    else:
        client.authenticate()
        observation['uid'] = client.uid
        b.private_write(observation_path, json.dumps(observation))
        b.private_write(cookie_path, '')
    jar.save(ignore_discard=True)
    cookie_path.chmod(0o600)
    baseline = state['baseline']
    assert base_snapshot(client) == baseline
    pods = json.loads(
        b.k('-n', 'odoo-dev', 'get', 'pods', '-l', 'odoo.io/component=server', '-o', 'json')
    )['items']
    pod = next(p for p in pods if not p['metadata'].get('deletionTimestamp'))['metadata']['name']
    websocket_version = (
        b.k(
            '-n',
            'odoo-dev',
            'exec',
            pod,
            '-c',
            'odoo',
            '--',
            'python3',
            '-c',
            'from odoo.addons.bus.websocket import WebsocketConnectionHandler; print(WebsocketConnectionHandler._VERSION)',
        )
        .decode()
        .strip()
    )
    ledger_path = b.ROOT / '.state/secrets-observation-writes.json'
    ledger = json.loads(ledger_path.read_text()) if ledger_path.exists() else []
    controller_generations = set()
    path = b.EVIDENCE / 'observation-samples.jsonl'
    errors = []
    generations = set()
    samples = 0
    b.record(
        'real-24h-observation',
        'NOT RUN',
        started_at=observation['started_at'],
        expected_completion_at=dt.datetime.fromtimestamp(deadline, dt.timezone.utc).isoformat(),
        collector_running=True,
        live_ttl_seconds=86400,
    )
    while time.time() < deadline:
        sample = {'utc': dt.datetime.now(dt.timezone.utc).isoformat()}
        try:
            client.session()
            assert base_snapshot(client) == baseline
            client.websocket(websocket_version)
            heartbeat_rows = client.call(
                'ir.config_parameter',
                'search_read',
                [[['key', '=', 'odoo.ha.cron_heartbeat']]],
                {'fields': ['value']},
            )
            heartbeat = float(heartbeat_rows[0]['value']) if heartbeat_rows else 0
            assert 0 <= time.time() - heartbeat < 90, 'Cron heartbeat is stale'
            sample['write_attempted'] = True
            identifier = client.call(
                'res.partner',
                'create',
                [{'name': 'odoo-secrets-observation-' + initial['generation']}],
            )
            sample['acknowledged_id'] = identifier
            ledger.append(identifier)
            b.private_write(ledger_path, json.dumps(ledger))
            assert client.call('res.partner', 'search_count', [[['id', 'in', ledger]]]) == len(
                ledger
            )
            logs = b.k(
                '-n',
                'secrets-system',
                'logs',
                'deployment/secret-rotator',
                '--since-time=' + observation['started_at'],
            ).decode()
            for line in logs.splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get('event') == 'ROTATION_COMPLETE':
                    controller_generations.add(event['generation'])
            with a.local_worker(bao_address='https://not-used.invalid') as worker:
                value = worker.status()
                configuration = value['configuration']
                assert (
                    configuration['ttl_seconds'] == 86400 and configuration['lead_seconds'] == 900
                )
                generation = configuration['active_generation']
                generations.add(generation)
                sample.update(
                    generation=generation,
                    phase=(value['operation'] or {}).get('phase'),
                    active_connections=value['connections'],
                    cron_heartbeat=heartbeat,
                    websocket=True,
                    acknowledged_writes=len(ledger),
                    automatic_generations=sorted(controller_generations),
                )
            assert (
                b.obj('secrets-system', 'deployment', 'secret-expiry-guard')['status'].get(
                    'availableReplicas'
                )
                == 2
            )
            sample['status'] = 'PASS'
        except Exception as error:
            sample.update(status='FAIL', error_type=type(error).__name__)
            errors.append(sample)
        jar.save(ignore_discard=True)
        cookie_path.chmod(0o600)
        with path.open('a') as output:
            output.write(json.dumps(sample) + '\n')
        samples += 1
        time.sleep(min(30, max(0, deadline - time.time())))
    with a.local_worker(bao_address='https://not-used.invalid') as worker:
        value = worker.status()
        current = value['configuration']['active_generation']
        assert current != initial['generation'] and not value['operation']
        rejected_login(worker, initial)
        assert value['connections'].get(initial['username'], 0) == 0
    client.session()
    assert base_snapshot(client) == baseline
    historic = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    automatic = {
        generation for sample in historic for generation in sample.get('automatic_generations', [])
    }
    assert current in automatic, 'No completion from the automatic controller was observed'
    assert client.call('res.partner', 'search_count', [[['id', 'in', ledger]]]) == len(ledger)
    errors = [sample for sample in historic if sample['status'] == 'FAIL']
    timestamps = [a.w.utc(sample['utc']).timestamp() for sample in historic]
    gaps = [
        later - earlier for earlier, later in zip([start] + timestamps, timestamps + [time.time()])
    ]
    if not historic or max(gaps) > 75:
        b.record(
            'real-24h-observation',
            'FAIL',
            elapsed_seconds=round(time.time() - start, 3),
            longest_sample_gap_seconds=round(max(gaps), 3),
        )
        raise AssertionError('Real observation has a monitoring gap')
    if errors:
        b.record(
            'real-24h-observation',
            'FAIL',
            elapsed_seconds=round(time.time() - start, 3),
            failed_samples=len(errors),
        )
        raise AssertionError('Real observation recorded health failures')
    b.record(
        'real-24h-observation',
        elapsed_seconds=round(time.time() - start, 3),
        samples=samples,
        generations=sorted(generations),
        automatic_rotation=True,
        initial_login_rejected=True,
        initial_sessions=0,
        cookie_preserved=True,
        baseline=baseline,
        cron_healthy=True,
        websocket_healthy=True,
        acknowledged_writes_preserved=len(ledger),
    )
    client.call('res.partner', 'unlink', [ledger])
