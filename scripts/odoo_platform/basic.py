"""A one-node Kind profile that starts from the same values used by fresh GitOps installs."""

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import subprocess
import sys

import yaml

from .runtime import CommandRunner, SecretRedactor, require_owner
from .images import verify_cached_image

ROOT = Path(__file__).resolve().parents[2]
OWNER_LABEL = 'odoo.io/basic-owner'


class BasicEnvironment:
    def __init__(self, cluster='odoo-basic', root=ROOT):
        if not cluster.startswith('odoo-basic') or not all(
            c.isalnum() or c == '-' for c in cluster
        ):
            raise ValueError(
                'Basic cluster names must start with odoo-basic and use letters, digits or hyphens'
            )
        self.root = Path(root)
        self.cluster = cluster
        self.namespace = 'odoo-dev'
        self.context = 'kind-' + cluster
        self.state = self.root / '.state' / cluster
        self.config = self.state / 'kubeconfig'
        self.credentials_path = self.state / 'credentials.json'
        self.marker = self.state / 'owner.json'
        self.bin = self.root / '.tools/bin'
        self.runner = CommandRunner(self.root)

    def kubectl(self, *args, data=None, timeout=120):
        return self.runner.run(
            [self.bin / 'kubectl', '--kubeconfig', self.config, '--context', self.context, *args],
            data=data,
            timeout=timeout,
        )

    def apply(self, document):
        self.kubectl('apply', '-f', '-', data=json.dumps(document).encode())

    def exists(self):
        return (
            self.cluster
            in self.runner.run([self.bin / 'kind', 'get', 'clusters']).decode().splitlines()
        )

    def check_owner(self):
        if not self.marker.exists() or json.loads(self.marker.read_text()) != {
            'cluster': self.cluster
        }:
            raise RuntimeError('Missing or mismatched basic environment ownership marker')
        node = json.loads(self.runner.run(['docker', 'inspect', self.cluster + '-control-plane']))[
            0
        ]
        if node['Config']['Labels'].get('io.x-k8s.kind.cluster') != self.cluster:
            raise RuntimeError('Kind node ownership mismatch')

    def canonical_image(self, application):
        reference = json.loads((self.root / 'versions.lock.json').read_text())[
            application + '_image'
        ]
        repository, digest = reference.split('@')
        if ':' in repository.rsplit('/', 1)[-1]:
            repository = repository.rsplit(':', 1)[0]
        return repository + '@' + digest

    def load_images(self):
        locks = json.loads((self.root / 'versions.lock.json').read_text())
        for application in ('odoo', 'postgres'):
            reference = locks[application + '_image']
            archive = (
                self.root
                / '.cache/images'
                / (hashlib.sha256(reference.encode()).hexdigest() + '.tar')
            )
            if not archive.exists():
                continue
            verify_cached_image(self.root, application, reference, archive)
            node = self.cluster + '-control-plane'
            with archive.open('rb') as stream:
                subprocess.run(
                    [
                        'docker',
                        'exec',
                        '-i',
                        node,
                        'ctr',
                        '-n',
                        'k8s.io',
                        'images',
                        'import',
                        '--all-platforms',
                        '--digests',
                        '-',
                    ],
                    stdin=stream,
                    stdout=subprocess.DEVNULL,
                    check=True,
                    timeout=240,
                )
            entries = self.runner.run(
                ['docker', 'exec', node, 'ctr', '-n', 'k8s.io', 'images', 'ls']
            ).decode()
            selected = next(
                (
                    line.split()
                    for line in entries.splitlines()
                    if line.split() and line.split()[0] in (reference, reference.split('@')[0])
                ),
                None,
            )
            if not selected:
                raise RuntimeError('Cached image reference is missing')
            payload = (self.root / 'tests/ci/images' / (application + '-index.json')).read_bytes()
            if 'sha256:' + hashlib.sha256(payload).hexdigest() != reference.split('@')[1]:
                raise RuntimeError('Pinned upstream image index mismatch')
            allowed = {
                entry['digest']
                for entry in json.loads(payload)['manifests']
                if entry.get('platform', {}).get('architecture') == 'amd64'
                and entry.get('platform', {}).get('os') == 'linux'
            }
            if selected[2] not in allowed | {reference.split('@')[1]}:
                raise RuntimeError('Cached platform digest mismatch')
            self.runner.run(
                [
                    'docker',
                    'exec',
                    node,
                    'ctr',
                    '-n',
                    'k8s.io',
                    'images',
                    'tag',
                    '--force',
                    selected[0],
                    self.canonical_image(application),
                ]
            )

    def create_secrets(self, namespace):
        credentials = json.loads(self.credentials_path.read_text())
        self.runner.redactor = SecretRedactor(credentials.values())
        items = [
            ('postgres-admin', {'password': credentials['postgres-admin']}),
            ('odoo-database', {'password': credentials['odoo-database']}),
            (
                'odoo-credentials',
                {
                    'admin-password': credentials['odoo-admin'],
                    'master-password': credentials['odoo-master'],
                },
            ),
        ]
        for name, values in items:
            self.apply(
                {
                    'apiVersion': 'v1',
                    'kind': 'Secret',
                    'type': 'Opaque',
                    'metadata': {'name': name, 'namespace': namespace},
                    'data': {
                        key: base64.b64encode(value.encode()).decode()
                        for key, value in values.items()
                    },
                }
            )

    def install_chart(self, chart, namespace, *, restore=False):
        command = [
            self.bin / 'helm',
            'template',
            chart,
            self.root / 'helm' / chart,
            '-n',
            namespace,
            '-f',
            self.root / 'gitops/environments/dev' / (chart + '.yaml'),
            '--set',
            'storage.size=1Gi',
            '--set-string',
            'image=' + self.canonical_image('postgres' if chart == 'postgresql' else 'odoo'),
        ]
        if restore:
            command += [
                '--set',
                'initialization.enabled=false',
                '--set',
                'cronThreads=0',
                '--set',
                'replicaCount=0',
            ]
        self.kubectl('-n', namespace, 'apply', '-f', '-', data=self.runner.run(command))

    def wait_database(self, namespace):
        self.kubectl(
            '-n',
            namespace,
            'rollout',
            'status',
            'statefulset/postgresql',
            '--timeout=300s',
            timeout=310,
        )

    def wait_application(self, namespace):
        self.kubectl(
            '-n', namespace, 'rollout', 'status', 'deployment/odoo', '--timeout=300s', timeout=310
        )

    def up(self):
        for name in ('kind', 'kubectl', 'helm'):
            if not (self.bin / name).exists():
                raise RuntimeError(f'{name} is missing; run make install-tools')
        self.runner.run(['docker', 'info'])
        existing = self.exists()
        if existing:
            self.check_owner()
            require_owner(
                json.loads(self.kubectl('get', 'namespace', self.namespace, '-o', 'json')),
                OWNER_LABEL,
                self.cluster,
            )
        else:
            available = (
                int(
                    next(
                        line.split()[1]
                        for line in Path('/proc/meminfo').read_text().splitlines()
                        if line.startswith('MemAvailable:')
                    )
                )
                * 1024
            )
            if available < 4 * 1024**3 or shutil.disk_usage(self.root).free < 8 * 1024**3:
                raise RuntimeError('Basic profile requires 4 GiB available RAM and 8 GiB free disk')
            self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
            if any(self.state.iterdir()):
                raise RuntimeError('Basic state directory is not empty; refusing to overwrite it')
            self.marker.write_text(json.dumps({'cluster': self.cluster}))
            try:
                image = json.loads((self.root / 'versions.lock.json').read_text())['kind_image']
                environment = os.environ.copy()
                for key in (
                    'HTTP_PROXY',
                    'HTTPS_PROXY',
                    'ALL_PROXY',
                    'http_proxy',
                    'https_proxy',
                    'all_proxy',
                ):
                    environment.pop(key, None)
                self.runner.run(
                    [
                        self.bin / 'kind',
                        'create',
                        'cluster',
                        '--name',
                        self.cluster,
                        '--image',
                        image,
                        '--kubeconfig',
                        self.config,
                        '--wait',
                        '180s',
                    ],
                    timeout=240,
                    env=environment,
                )
                self.config.chmod(0o600)
            except BaseException:
                self.down()
                raise
        try:
            self.check_owner()
            if not self.credentials_path.exists():
                credentials = {
                    key: secrets.token_urlsafe(36)
                    for key in ('postgres-admin', 'odoo-database', 'odoo-admin', 'odoo-master')
                }
                descriptor = os.open(
                    self.credentials_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                )
                with os.fdopen(descriptor, 'w') as stream:
                    json.dump(credentials, stream)
            self.load_images()
            self.apply(
                {
                    'apiVersion': 'v1',
                    'kind': 'Namespace',
                    'metadata': {
                        'name': self.namespace,
                        'labels': {
                            OWNER_LABEL: self.cluster,
                            'pod-security.kubernetes.io/enforce': 'restricted',
                        },
                    },
                }
            )
            self.create_secrets(self.namespace)
            self.install_chart('postgresql', self.namespace)
            self.wait_database(self.namespace)
            self.install_chart('odoo', self.namespace)
            self.kubectl(
                '-n',
                self.namespace,
                'wait',
                '--for=condition=complete',
                'job/odoo-initialize',
                '--timeout=900s',
                timeout=910,
            )
            self.wait_application(self.namespace)
            print(f'PASS: {self.cluster} ready; private state in {self.state}', flush=True)
        except BaseException:
            if not existing:
                self.down()
            raise

    def down(self):
        if self.exists():
            self.check_owner()
            self.runner.run(
                [self.bin / 'kind', 'delete', 'cluster', '--name', self.cluster], timeout=120
            )
        if self.marker.exists():
            if json.loads(self.marker.read_text()) != {'cluster': self.cluster}:
                raise RuntimeError('Refusing to remove unrelated state')
            shutil.rmtree(self.state)

    def application_check(self, namespace):
        environment = os.environ.copy()
        environment.pop('TEST_URL', None)
        environment.update(
            TEST_KUBECONFIG=str(self.config),
            TEST_CONTEXT=self.context,
            TEST_NAMESPACE=namespace,
            TEST_CREDENTIALS=str(self.credentials_path),
            TEST_SNAPSHOT_PATH=str(self.state / 'snapshot.json'),
        )
        output = self.runner.run(
            [sys.executable, self.root / 'tests/application/check.py'], env=environment, timeout=150
        )
        print(output.decode().strip(), flush=True)
        return json.loads((self.state / 'snapshot.json').read_text())

    def volume_helper(self, namespace):
        pods = json.loads(self.kubectl('-n', namespace, 'get', 'pods', '-o', 'json'))['items']
        if any(pod['metadata']['name'] == 'basic-filestore' for pod in pods):
            raise RuntimeError('Filestore helper name is occupied; operation refused')
        document = json.loads(
            self.runner.run(
                [
                    sys.executable,
                    self.root / 'scripts/volume-helper.py',
                    namespace,
                    'basic-filestore',
                    'odoo-data',
                ]
            )
        )
        document['metadata']['labels'][OWNER_LABEL] = self.cluster
        document['spec']['containers'][0]['image'] = self.canonical_image('odoo')
        self.apply(document)
        try:
            self.kubectl(
                '-n',
                namespace,
                'wait',
                '--for=condition=Ready',
                'pod/basic-filestore',
                '--timeout=120s',
                timeout=130,
            )
        except BaseException:
            require_owner(
                json.loads(
                    self.kubectl('-n', namespace, 'get', 'pod', 'basic-filestore', '-o', 'json')
                ),
                OWNER_LABEL,
                self.cluster,
            )
            self.kubectl('-n', namespace, 'delete', 'pod', 'basic-filestore', '--wait=true')
            raise

    def capture(self):
        helper = False
        try:
            self.kubectl('-n', self.namespace, 'scale', 'deployment/odoo', '--replicas=0')
            self.kubectl(
                '-n',
                self.namespace,
                'wait',
                '--for=delete',
                'pod',
                '-l',
                'odoo.io/component=server',
                '--timeout=120s',
                timeout=130,
            )
            self.volume_helper(self.namespace)
            helper = True
            dump = self.kubectl(
                '-n',
                self.namespace,
                'exec',
                'postgresql-0',
                '--',
                'pg_dump',
                '-U',
                'postgres',
                '-d',
                'odoo_dev',
                '--format=custom',
                '--no-owner',
                '--no-acl',
                timeout=120,
            )
            filestore = self.kubectl(
                '-n',
                self.namespace,
                'exec',
                'basic-filestore',
                '--',
                'tar',
                '-czf',
                '-',
                '-C',
                '/data/filestore',
                'odoo_dev',
                timeout=120,
            )
            if not dump or not filestore:
                raise RuntimeError('Backup capture produced an empty archive')
            return dump, filestore
        finally:
            try:
                if helper:
                    self.kubectl(
                        '-n', self.namespace, 'delete', 'pod', 'basic-filestore', '--wait=true'
                    )
            finally:
                self.kubectl('-n', self.namespace, 'scale', 'deployment/odoo', '--replicas=1')
                self.wait_application(self.namespace)

    def restore_test(self, baseline):
        namespace = 'odoo-restore-test'
        namespaces = json.loads(self.kubectl('get', 'namespaces', '-o', 'json'))['items']
        if any(item['metadata']['name'] == namespace for item in namespaces):
            raise RuntimeError('Restore namespace is occupied; refusing to reuse it')
        dump, filestore = self.capture()
        created = False
        try:
            self.apply(
                {
                    'apiVersion': 'v1',
                    'kind': 'Namespace',
                    'metadata': {
                        'name': namespace,
                        'labels': {
                            OWNER_LABEL: self.cluster,
                            'pod-security.kubernetes.io/enforce': 'restricted',
                        },
                    },
                }
            )
            created = True
            self.create_secrets(namespace)
            self.install_chart('postgresql', namespace)
            self.wait_database(namespace)
            self.kubectl(
                '-n',
                namespace,
                'exec',
                '-i',
                'postgresql-0',
                '--',
                'sh',
                '-ceu',
                'umask 077; cat > /tmp/basic-restore.dump',
                data=dump,
                timeout=120,
            )
            self.kubectl(
                '-n',
                namespace,
                'exec',
                'postgresql-0',
                '--',
                'pg_restore',
                '-U',
                'postgres',
                '--role=odoo',
                '-d',
                'odoo_dev',
                '--no-owner',
                '--no-acl',
                '--exit-on-error',
                '/tmp/basic-restore.dump',
                timeout=120,
            )
            self.install_chart('odoo', namespace, restore=True)
            self.volume_helper(namespace)
            self.kubectl(
                '-n',
                namespace,
                'exec',
                '-i',
                'basic-filestore',
                '--',
                'sh',
                '-ceu',
                'mkdir -p /data/filestore; tar -xzf - -C /data/filestore',
                data=filestore,
                timeout=120,
            )
            self.kubectl('-n', namespace, 'delete', 'pod', 'basic-filestore', '--wait=true')
            self.kubectl('-n', namespace, 'scale', 'deployment/odoo', '--replicas=1')
            self.wait_application(namespace)
            if self.application_check(namespace) != baseline:
                raise RuntimeError('Isolated database/filestore restore differs from its source')
        finally:
            if created:
                require_owner(
                    json.loads(self.kubectl('get', 'namespace', namespace, '-o', 'json')),
                    OWNER_LABEL,
                    self.cluster,
                )
                self.kubectl(
                    'delete', 'namespace', namespace, '--wait=true', '--timeout=180s', timeout=190
                )

    def test(self):
        self.check_owner()
        require_owner(
            json.loads(self.kubectl('get', 'namespace', self.namespace, '-o', 'json')),
            OWNER_LABEL,
            self.cluster,
        )
        self.runner.redactor = SecretRedactor.from_files([self.credentials_path])
        role = (
            self.kubectl(
                '-n',
                self.namespace,
                'exec',
                'postgresql-0',
                '--',
                'psql',
                '-U',
                'postgres',
                '-d',
                'postgres',
                '-Atc',
                "SELECT rolname,rolsuper,rolcreatedb,rolcreaterole FROM pg_roles WHERE rolname='odoo'",
            )
            .decode()
            .strip()
        )
        if role != 'odoo|f|f|f':
            raise RuntimeError('Application database role has unexpected privileges')
        before = self.application_check(self.namespace)
        for arguments in [('postgresql-0',), ('-l', 'odoo.io/component=server')]:
            self.kubectl(
                '-n',
                self.namespace,
                'delete',
                'pod',
                *arguments,
                '--wait=true',
                '--timeout=120s',
                timeout=130,
            )
            self.wait_database(self.namespace)
            self.wait_application(self.namespace)
        if self.application_check(self.namespace) != before:
            raise RuntimeError('Persistent records or attachment hash changed during pod recovery')
        self.restore_test(before)
        print(
            'PASS: login/CRM/attachments, least privileges, pod recovery and isolated database/filestore restore',
            flush=True,
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('up', 'test', 'status', 'down', 'forward'))
    parser.add_argument('--cluster', default=os.environ.get('ODOO_BASIC_CLUSTER', 'odoo-basic'))
    args = parser.parse_args()
    environment = BasicEnvironment(args.cluster)

    def interrupted(signum, frame):
        raise InterruptedError('Basic operation interrupted')

    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, interrupted)
    if args.action in ('up', 'test', 'down'):
        getattr(environment, args.action)()
    elif args.action == 'status':
        environment.check_owner()
        print(environment.kubectl('-n', environment.namespace, 'get', 'pods,pvc,jobs').decode())
    else:
        environment.check_owner()
        subprocess.run(
            [
                environment.bin / 'kubectl',
                '--kubeconfig',
                environment.config,
                '--context',
                environment.context,
                '-n',
                environment.namespace,
                'port-forward',
                '--address',
                '127.0.0.1',
                'service/odoo',
                '8069:8069',
            ],
            check=True,
        )
