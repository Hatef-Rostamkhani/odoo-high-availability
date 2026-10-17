#!/usr/bin/env python3
"""Independent OpenBao bootstrap. Secret values travel in memory/stdin only."""

import argparse
import base64
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import secrets
import select
import shutil
import socket
import ssl
import subprocess
import sys
import tarfile
import time
import threading
import urllib.error
import urllib.request

import yaml
from odoo_platform.runtime import CommandRunner

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = Path.home() / '.local/share/odoo-dev/secrets'
EVIDENCE = ROOT / 'evidence/secrets'
K = [
    str(ROOT / '.tools/bin/kubectl'),
    '--kubeconfig',
    str(ROOT / '.state/kubeconfig'),
    '--context',
    'kind-odoo-dev',
    '--request-timeout=15s',
]
VERSIONS = {'openbao': '2.7.1', 'openbao_chart': '0.30.3', 'eso': '2.12.0'}
CHARTS = {
    'openbao-0.30.3.tgz': (
        'https://github.com/openbao/openbao-helm/releases/download/openbao-0.30.3/openbao-0.30.3.tgz',
        'd50ed31b8305f430df308c342dd3c1efa877222380e99cb480f74f070d6beaa8',
    ),
    'external-secrets-2.12.0.tgz': (
        'https://github.com/external-secrets/external-secrets/releases/download/helm-chart-2.12.0/external-secrets-2.12.0.tgz',
        '4b584136b7311c0af8c8719d377cc5b1a7ef12c2de18f1fdb30614eab75bdeb3',
    ),
}


def run(command, data=None, timeout=120):
    # OpenBao errors may contain recovery material, so stderr is always omitted.
    return CommandRunner(ROOT, include_stderr=False).run(command, data=data, timeout=timeout)


def k(*args, data=None, timeout=120):
    return run(K + list(args), data, timeout)


def obj(namespace, kind, name):
    return json.loads(k('-n', namespace, 'get', kind, name, '-o', 'json'))


def exists(namespace, kind, name):
    return (
        subprocess.run(K + ['-n', namespace, 'get', kind, name], capture_output=True).returncode
        == 0
    )


def private_write(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    temporary = path.with_name(path.name + '.partial')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'wb') as f:
        f.write(content.encode() if isinstance(content, str) else content)
        f.flush()
        os.fsync(f.fileno())
    temporary.replace(path)
    path.chmod(0o600)


def record(name, status='PASS', **details):
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    target = EVIDENCE / (name + '.json')
    if target.exists():
        attempts = EVIDENCE / 'attempts'
        attempts.mkdir(exist_ok=True)
        previous = json.loads(target.read_text())
        stamp = previous['utc'].replace(':', '').replace('+', '_')
        (attempts / (name + '-' + stamp + '.json')).write_text(target.read_text())
    digest = hashlib.sha256()
    paths = run(['git', 'ls-files', '-co', '--exclude-standard']).decode().splitlines()
    for relative in sorted(set(paths)):
        path = ROOT / relative
        if path.is_file() and not relative.startswith(('evidence/', '.state/', 'backups/')):
            digest.update(relative.encode())
            digest.update(path.read_bytes())
    entry = {
        'test': name,
        'status': status,
        'utc': dt.datetime.now(dt.timezone.utc).isoformat(),
        'revision': run(['git', 'rev-parse', 'HEAD']).decode().strip(),
        'source_sha256': digest.hexdigest(),
        'versions': VERSIONS,
        **details,
    }
    target.write_text(json.dumps(entry, indent=2) + '\n')
    print(json.dumps(entry, ensure_ascii=False), flush=True)
    return entry


def wait(predicate, timeout=180, interval=1):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            result = predicate()
            if result:
                return result
        except (RuntimeError, OSError, urllib.error.HTTPError, subprocess.TimeoutExpired):
            pass
        time.sleep(interval)
    raise TimeoutError('Readiness or operation deadline exceeded')


def preflight(after_images=False):
    memory = (
        int(
            next(
                line.split()[1]
                for line in Path('/proc/meminfo').read_text().splitlines()
                if line.startswith('MemAvailable:')
            )
        )
        * 1024
    )
    disk = shutil.disk_usage(ROOT).free
    required = (15 if after_images else 20) * 2**30
    if disk < required or memory < 12 * 2**30:
        record(
            'resources-after-images' if after_images else 'resources',
            'BLOCKED',
            available_memory_bytes=memory,
            free_disk_bytes=disk,
            required_disk_bytes=required,
        )
        raise RuntimeError('Insufficient resources; no installation or migration is permitted')
    nodes = json.loads(k('get', 'nodes', '-o', 'json'))['items']
    if {n['metadata']['name'] for n in nodes} != {
        'odoo-dev-control-plane',
        'odoo-dev-worker',
        'odoo-dev-worker2',
    }:
        raise RuntimeError('Unexpected cluster identity')
    if not all(
        any(c['type'] == 'Ready' and c['status'] == 'True' for c in n['status']['conditions'])
        for n in nodes
    ):
        raise RuntimeError('All existing nodes must be Ready')
    return record(
        'resources-after-images' if after_images else 'resources',
        available_memory_bytes=memory,
        free_disk_bytes=disk,
    )


def ensure_secret(namespace, name, values):
    encoded = {
        key: base64.b64encode(value if isinstance(value, bytes) else value.encode()).decode()
        for key, value in values.items()
    }
    document = {
        'apiVersion': 'v1',
        'kind': 'Secret',
        'type': 'Opaque',
        'metadata': {
            'name': name,
            'namespace': namespace,
            'annotations': {'argocd.argoproj.io/sync-options': 'Prune=false,Delete=false'},
        },
        'data': encoded,
    }
    if exists(namespace, 'secret', name):
        current = obj(namespace, 'secret', name)
        if current.get('data') != encoded:
            raise RuntimeError(
                'Existing bootstrap Secret differs; refusing to replace key material'
            )
    else:
        k('create', '-f', '-', data=json.dumps(document).encode())


def namespace(name):
    if not exists(name, 'namespace', name):
        k('create', 'namespace', name)


def prepare_keys():
    PRIVATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    PRIVATE.chmod(0o700)
    # Losing the original key must never silently create an incompatible key.
    if exists('openbao', 'secret', 'openbao-seal') and not (PRIVATE / 'seal.key').exists():
        raise RuntimeError('Restore the original seal key from private recovery material')
    for filename in ('seal.key', 'etcd.key', 'snapshot.key'):
        if not (PRIVATE / filename).exists():
            private_write(PRIVATE / filename, secrets.token_bytes(32))
    if not (PRIVATE / 'seal-id').exists():
        private_write(PRIVATE / 'seal-id', 'odoo-dev-static-1\n')
    ca = PRIVATE / 'ca.crt'
    if not ca.exists():
        run(
            [
                'openssl',
                'req',
                '-x509',
                '-newkey',
                'rsa:3072',
                '-nodes',
                '-days',
                '3650',
                '-subj',
                '/CN=Odoo OpenBao Development CA',
                '-keyout',
                PRIVATE / 'ca.key',
                '-out',
                ca,
            ]
        )
    if not (PRIVATE / 'server.crt').exists():
        sans = [
            'DNS:openbao',
            'DNS:openbao.openbao.svc',
            'DNS:openbao.openbao.svc.cluster.local',
            'DNS:openbao-internal.openbao.svc',
            'DNS:localhost',
            'IP:127.0.0.1',
        ]
        sans += [f'DNS:openbao-{i}.openbao-internal.openbao.svc' for i in range(3)]
        sans += [f'DNS:openbao-{i}.openbao-internal.openbao.svc.cluster.local' for i in range(3)]
        private_write(
            PRIVATE / 'server.ext',
            'subjectAltName=' + ','.join(sans) + '\nextendedKeyUsage=serverAuth\n',
        )
        run(
            [
                'openssl',
                'req',
                '-new',
                '-newkey',
                'rsa:3072',
                '-nodes',
                '-subj',
                '/CN=openbao.openbao.svc',
                '-keyout',
                PRIVATE / 'server.key',
                '-out',
                PRIVATE / 'server.csr',
            ]
        )
        run(
            [
                'openssl',
                'x509',
                '-req',
                '-in',
                PRIVATE / 'server.csr',
                '-CA',
                ca,
                '-CAkey',
                PRIVATE / 'ca.key',
                '-CAcreateserial',
                '-days',
                '365',
                '-extfile',
                PRIVATE / 'server.ext',
                '-out',
                PRIVATE / 'server.crt',
            ]
        )
    for file in PRIVATE.iterdir():
        if file.is_file():
            file.chmod(0o600)


def encrypt_etcd():
    node = 'odoo-dev-control-plane'
    manifest_path = '/etc/kubernetes/manifests/kube-apiserver.yaml'
    manifest = run(['docker', 'exec', node, 'cat', manifest_path])
    doc = yaml.safe_load(manifest)
    command = doc['spec']['containers'][0]['command']
    if any(x.startswith('--encryption-provider-config=') for x in command):
        if (
            '--encryption-provider-config=/etc/kubernetes/odoo-secrets/encryption.yaml'
            not in command
        ):
            raise RuntimeError(
                'An unrelated encryption configuration exists; refusing to overwrite it'
            )
    else:
        original_container = bcid = obj('kube-system', 'pod', 'kube-apiserver-' + node)['status'][
            'containerStatuses'
        ][0]['containerID']
        private_write(PRIVATE / 'kube-apiserver.before.yaml', manifest)
        config = {
            'apiVersion': 'apiserver.config.k8s.io/v1',
            'kind': 'EncryptionConfiguration',
            'resources': [
                {
                    'resources': ['secrets'],
                    'providers': [
                        {
                            'aesgcm': {
                                'keys': [
                                    {
                                        'name': 'odoo-dev-1',
                                        'secret': base64.b64encode(
                                            (PRIVATE / 'etcd.key').read_bytes()
                                        ).decode(),
                                    }
                                ]
                            }
                        },
                        {'identity': {}},
                    ],
                }
            ],
        }
        private_write(PRIVATE / 'encryption.yaml', yaml.safe_dump(config))
        run(['docker', 'exec', node, 'mkdir', '-p', '/etc/kubernetes/odoo-secrets'])
        run(
            [
                'docker',
                'cp',
                PRIVATE / 'encryption.yaml',
                node + ':/etc/kubernetes/odoo-secrets/encryption.yaml',
            ]
        )
        run(['docker', 'exec', node, 'chmod', '700', '/etc/kubernetes/odoo-secrets'])
        run(
            ['docker', 'exec', node, 'chmod', '600', '/etc/kubernetes/odoo-secrets/encryption.yaml']
        )
        command += [
            '--encryption-provider-config=/etc/kubernetes/odoo-secrets/encryption.yaml',
            '--encryption-provider-config-automatic-reload=true',
        ]
        doc['spec']['containers'][0].setdefault('volumeMounts', []).append(
            {
                'name': 'odoo-secrets-encryption',
                'mountPath': '/etc/kubernetes/odoo-secrets',
                'readOnly': True,
            }
        )
        doc['spec'].setdefault('volumes', []).append(
            {
                'name': 'odoo-secrets-encryption',
                'hostPath': {'path': '/etc/kubernetes/odoo-secrets', 'type': 'Directory'},
            }
        )
        private_write(PRIVATE / 'kube-apiserver.after.yaml', yaml.safe_dump(doc, sort_keys=False))
        started = time.monotonic()
        # Write outside kubelet's watched directory, then atomically rename.
        run(
            [
                'docker',
                'cp',
                PRIVATE / 'kube-apiserver.after.yaml',
                node + ':/etc/kubernetes/kube-apiserver.encrypted.yaml',
            ]
        )
        run(
            [
                'docker',
                'exec',
                node,
                'mv',
                '/etc/kubernetes/kube-apiserver.encrypted.yaml',
                manifest_path,
            ]
        )
        try:
            wait(lambda: k('get', '--raw=/readyz').strip() == b'ok', timeout=120)
            wait(
                lambda: (
                    '--encryption-provider-config=/etc/kubernetes/odoo-secrets/encryption.yaml'
                    in obj('kube-system', 'pod', 'kube-apiserver-' + node)['spec']['containers'][0][
                        'command'
                    ]
                ),
                timeout=120,
            )

            def restarted():
                pod = obj('kube-system', 'pod', 'kube-apiserver-' + node)
                return pod['status'].get('containerStatuses', [{}])[0].get(
                    'containerID'
                ) != original_container and any(
                    c['type'] == 'Ready' and c['status'] == 'True'
                    for c in pod['status'].get('conditions', [])
                )

            wait(restarted, timeout=120)
        except Exception:
            run(
                [
                    'docker',
                    'cp',
                    PRIVATE / 'kube-apiserver.before.yaml',
                    node + ':/etc/kubernetes/kube-apiserver.rollback.yaml',
                ]
            )
            run(
                [
                    'docker',
                    'exec',
                    node,
                    'mv',
                    '/etc/kubernetes/kube-apiserver.rollback.yaml',
                    manifest_path,
                ]
            )
            wait(lambda: k('get', '--raw=/readyz').strip() == b'ok', timeout=120)
            record('etcd-encryption', 'FAIL', restored_original_manifest=True)
            raise
        record('api-server-encryption-restart', seconds=round(time.monotonic() - started, 3))
    # resourceVersion races with operators are retried; values never leave stdin.
    objects = json.loads(k('get', 'secrets', '-A', '-o', 'json'))['items']
    for item in objects:
        for attempt in range(4):
            current = obj(item['metadata']['namespace'], 'secret', item['metadata']['name'])
            current.pop('status', None)
            current['metadata'].pop('managedFields', None)
            try:
                k('replace', '-f', '-', data=json.dumps(current).encode())
                break
            except RuntimeError:
                if attempt == 3:
                    raise
    record(
        'etcd-encryption',
        rewritten_secrets=len(objects),
        provider='aesgcm',
        scope='Secrets only; encryption key held outside etcd',
    )
    sample = objects[0]['metadata']
    verify_etcd_secret(sample['namespace'], sample['name'])


def verify_etcd_secret(namespace, name):
    raw = k(
        '-n',
        'kube-system',
        'exec',
        'etcd-odoo-dev-control-plane',
        '--',
        'etcdctl',
        '--endpoints=https://127.0.0.1:2379',
        '--cacert=/etc/kubernetes/pki/etcd/ca.crt',
        '--cert=/etc/kubernetes/pki/etcd/healthcheck-client.crt',
        '--key=/etc/kubernetes/pki/etcd/healthcheck-client.key',
        'get',
        '/registry/secrets/' + namespace + '/' + name,
        '--print-value-only',
    )
    if not raw.startswith(b'k8s:enc:aesgcm:v1:odoo-dev-1:'):
        raise RuntimeError('Secret is not encrypted in the raw etcd record')
    return record(
        'etcd-ciphertext-' + namespace + '-' + name,
        provider='aesgcm',
        ciphertext_prefix_verified=True,
    )


def prepare_charts_images():
    for name, (url, expected) in CHARTS.items():
        path = ROOT / 'vendor' / name
        if not path.exists():
            with urllib.request.urlopen(url, timeout=60) as response:
                data = response.read()
            if hashlib.sha256(data).hexdigest() != expected:
                raise RuntimeError('Helm package checksum mismatch')
            path.write_bytes(data)
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise RuntimeError('Vendored Helm package checksum mismatch')
    images = {
        'openbao': 'quay.io/openbao/openbao:2.7.1',
        'eso': 'ghcr.io/external-secrets/external-secrets:v2.12.0',
    }
    lock_path = ROOT / 'infrastructure/secrets/images.lock.json'
    if lock_path.exists():
        return json.loads(lock_path.read_text())
    records = {}
    for name, reference in images.items():
        archive = ROOT / '.cache/images' / (hashlib.sha256(reference.encode()).hexdigest() + '.tar')
        run([sys.executable, ROOT / 'scripts/fetch-image.py', reference, archive], timeout=1300)
        with tarfile.open(archive) as stream:
            digest = json.load(stream.extractfile('index.json'))['manifests'][0]['digest']
        records[name] = {
            'image': reference + '@' + digest,
            'digest': digest,
            'archive': str(archive.relative_to(ROOT)),
        }
        alias = archive.parent / (
            hashlib.sha256(records[name]['image'].encode()).hexdigest() + '.tar'
        )
        if not alias.exists():
            os.link(archive, alias)
    records['runtime'] = {
        'image': json.loads((ROOT / 'versions.lock.json').read_text())['odoo_image']
    }
    records['charts'] = {name: {'sha256': checksum} for name, (_, checksum) in CHARTS.items()}
    lock_path.write_text(json.dumps(records, indent=2) + '\n')
    return records


@contextlib.contextmanager
def forward(pod='openbao-0', namespace='openbao', remote=8200):
    wait(
        lambda: obj(namespace, 'pod', pod).get('status', {}).get('phase') == 'Running', timeout=120
    )
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
    env = os.environ.copy()
    env['KUBECTL_PORT_FORWARD_WEBSOCKETS'] = 'true'
    p = subprocess.Popen(
        K
        + [
            '--request-timeout=0',
            '-n',
            namespace,
            'port-forward',
            '--address=127.0.0.1',
            'pod/' + pod,
            f'{port}:{remote}',
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    try:
        if not select.select([p.stdout], [], [], 20)[0] or not p.stdout.readline().startswith(
            b'Forwarding from '
        ):
            raise RuntimeError('Private forward did not start')

        # Opening and abandoning an unframed PostgreSQL startup connection as
        # a readiness probe can tear down the multiplexed forwarding stream.
        def drain():
            for _ in p.stdout:
                pass

        threading.Thread(target=drain, daemon=True).start()
        yield 'https://127.0.0.1:' + str(port)
    finally:
        p.terminate()
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()


def api(address, path, payload=None, token=None, method=None):
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['X-Vault-Token'] = token
    request = urllib.request.Request(
        address + '/v1/' + path,
        headers=headers,
        data=json.dumps(payload).encode() if payload is not None else None,
        method=method or ('POST' if payload is not None else 'GET'),
    )
    context = ssl.create_default_context(cafile=str(PRIVATE / 'ca.crt'))
    try:
        with urllib.request.urlopen(request, context=context, timeout=15) as response:
            data = response.read()
    except urllib.error.HTTPError as error:
        raise RuntimeError(
            f'OpenBao API {path.split("/")[0]} failed with HTTP {error.code}'
        ) from None
    return json.loads(data) if data else {}


def status():
    instances = []
    for i in range(3):
        with forward('openbao-' + str(i)) as address:
            value = api(address, 'sys/seal-status')
        pod = obj('openbao', 'pod', 'openbao-' + str(i))
        instances.append(
            {
                'pod': 'openbao-' + str(i),
                'node': pod['spec'].get('nodeName'),
                'initialized': value['initialized'],
                'sealed': value['sealed'],
                'cluster_id': value.get('cluster_id'),
                'version': value['version'],
            }
        )
    if any(not p['initialized'] or p['sealed'] for p in instances):
        raise RuntimeError('All three OpenBao instances must be initialized and unsealed')
    if len({p['node'] for p in instances}) != 3 or len({p['cluster_id'] for p in instances}) != 1:
        raise RuntimeError('Expected one Raft cluster across three existing nodes')
    return record('openbao-status', instances=instances)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['install', 'status', 'backup', 'restore-test'])
    args = parser.parse_args()
    if args.action == 'install':
        from secrets_platform import install

        install()
    elif args.action == 'status':
        status()
    else:
        from secrets_platform import backup, restore_test

        (backup if args.action == 'backup' else restore_test)()


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('OpenBao operation stopped: ' + str(error), file=sys.stderr)
        raise SystemExit(1)
