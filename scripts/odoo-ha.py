#!/usr/bin/env python3
"""Bootstrap, migrate and validate Odoo instance HA without rebuilding Kind."""

import base64
import contextlib
import copy
import datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import yaml

import ha
from ha import ROOT, STATE, run, k, obj, apply, wait, maintenance

spec = importlib.util.spec_from_file_location('odoo_runtime', ROOT / 'scripts/odoo-runtime.py')
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)
EVIDENCE = ROOT / 'evidence/odoo-ha'
EVIDENCE.mkdir(parents=True, exist_ok=True)
VALUES = ROOT / 'gitops/environments/dev/odoo.yaml'
IMAGE_LOCK = json.loads((ROOT / 'infrastructure/odoo-ha/images.lock.json').read_text())['haproxy']
IMAGE = IMAGE_LOCK['image']
IMAGE_ID = IMAGE_LOCK['docker_runtime_digest']
WORKERS = ['odoo-dev-worker', 'odoo-dev-worker2']
ENTRY = 'odoo-entry'
PRIVATE = 'odoo-private-entry'
ENTRY_IP = '172.18.0.250'
LAST_AUTH_FAILURE = None


def evidence(name, status='PASS', **details):
    value = {
        'test': name,
        'status': status,
        'utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'revision': run(['git', 'rev-parse', 'HEAD']).decode().strip(),
        'source_sha256': ha.fingerprint(),
        'versions': json.loads((ROOT / 'versions.lock.json').read_text()),
        **details,
    }
    (EVIDENCE / (name + '.json')).write_text(json.dumps(value, indent=2) + '\n')
    print(json.dumps(value), flush=True)
    return value


def render(chart, values=None, namespace='odoo-dev'):
    command = [
        str(ROOT / '.tools/bin/helm'),
        'template',
        chart,
        str(ROOT / 'helm' / chart),
        '-n',
        namespace,
    ]
    if values is not None:
        path = STATE / (chart + '-ha-values.yaml')
        path.write_text(yaml.safe_dump(values, sort_keys=False))
        command += ['-f', str(path)]
    return run(command)


def deploy(values, namespace='odoo-dev'):
    k('-n', namespace, 'apply', '-f', '-', input=render('odoo', values, namespace))


def ready(namespace, component, count=2):
    selector = (
        'app=odoo-edge'
        if component == 'edge'
        else 'app.kubernetes.io/name=odoo,odoo.io/component=' + component
    )
    pods = json.loads(k('-n', namespace, 'get', 'pods', '-l', selector, '-o', 'json'))['items']
    pods = [p for p in pods if not p['metadata'].get('deletionTimestamp')]
    if len(pods) != count:
        return False
    if any(
        not any(
            c['type'] == 'Ready' and c['status'] == 'True'
            for c in p['status'].get('conditions', [])
        )
        for p in pods
    ):
        return False
    if count == 2 and len({p['spec']['nodeName'] for p in pods}) != 2:
        return False
    return pods


def helper(name, claim, node=None, namespace='odoo-dev', second_claim=None):
    existing = subprocess.run(
        ha.K + ['-n', namespace, 'get', 'pod', name, '-o', 'json'], capture_output=True
    )
    if existing.returncode == 0:
        assert (
            json.loads(existing.stdout)['metadata'].get('labels', {}).get('odoo.io/project')
            == 'odoo-ha'
        ), 'Unrelated helper name is occupied'
    doc = json.loads(
        run([sys.executable, str(ROOT / 'scripts/volume-helper.py'), namespace, name, claim])
    )
    doc['metadata']['labels']['odoo.io/project'] = 'odoo-ha'
    if node:
        doc['spec']['nodeName'] = node
    if second_claim:
        doc['spec']['volumes'].append(
            {'name': 'other', 'persistentVolumeClaim': {'claimName': second_claim}}
        )
        doc['spec']['containers'][0]['volumeMounts'].append(
            {'name': 'other', 'mountPath': '/other'}
        )
    apply(doc)
    k('-n', namespace, 'wait', '--for=condition=Ready', 'pod/' + name, '--timeout=45s', timeout=50)
    return name


def delete_helper(name, namespace='odoo-dev'):
    existing = subprocess.run(
        ha.K + ['-n', namespace, 'get', 'pod', name, '-o', 'json'], capture_output=True
    )
    if existing.returncode == 0:
        assert (
            json.loads(existing.stdout)['metadata'].get('labels', {}).get('odoo.io/project')
            == 'odoo-ha'
        ), 'Refusing to delete an unrelated helper'
    k(
        '-n',
        namespace,
        'delete',
        'pod',
        name,
        '--ignore-not-found',
        '--wait=true',
        '--timeout=20s',
        timeout=25,
    )


def preflight(after_images=False):
    mem = (
        int(
            next(
                line
                for line in Path('/proc/meminfo').read_text().splitlines()
                if line.startswith('MemAvailable:')
            ).split()[1]
        )
        * 1024
    )
    assert mem >= 12 * 1024**3, 'At least 12 GiB available RAM is required'
    assert shutil.disk_usage(ROOT).free >= (15 if after_images else 20) * 1024**3, (
        'Insufficient free disk'
    )
    assert run(['systemctl', 'is-active', 'nfs-server']).decode().strip() == 'active', (
        'Run sudo bash scripts/odoo-ha-host.sh first'
    )
    for service in ('nfs-mountd', 'nfs-idmapd'):
        assert run(['systemctl', 'is-active', service]).decode().strip() == 'active', (
            service + ' is not healthy; repeat sudo bash scripts/odoo-ha-host.sh'
        )
    with socket.create_connection(('172.18.0.1', 2049), timeout=2):
        pass
    if os.environ.get('ODOO_HA_ALLOW_UNINITIALIZED') != '1':
        assert ha.active()[0] == 'crunchy', 'Existing Crunchy backend must be active'
    assert len(json.loads(k('get', 'nodes', '-o', 'json'))['items']) == 3


def install():
    preflight()
    k('apply', '-f', str(ROOT / 'gitops/bootstrap/namespaces.yaml'))
    k('apply', '-f', str(ROOT / 'gitops/bootstrap/projects.yaml'))
    k('apply', '-f', str(ROOT / 'gitops/bootstrap/argocd-rbac.yaml'))
    secret = obj('argocd', 'secret', 'odoo-in-cluster')
    allowed = base64.b64decode(secret['data']['namespaces']).decode().split(',')
    if 'odoo-edge' not in allowed:
        allowed.append('odoo-edge')
        k(
            '-n',
            'argocd',
            'patch',
            'secret',
            'odoo-in-cluster',
            '--type=merge',
            '-p',
            json.dumps({'stringData': {'namespaces': ','.join(allowed)}}),
        )
    k('apply', '-f', str(ROOT / 'infrastructure/odoo-ha/storage.yaml'))
    wait(
        lambda: obj('odoo-dev', 'pvc', 'odoo-shared-data')['status']['phase'] == 'Bound',
        timeout=30,
        interval=0.5,
    )
    token = os.urandom(12).hex()
    names = []
    try:
        for i, node in enumerate(WORKERS):
            names.append('odoo-ha-nfs-' + str(i))
            helper(names[-1], 'odoo-shared-data', node)
        k(
            '-n',
            'odoo-dev',
            'exec',
            names[0],
            '--',
            'python3',
            '-c',
            "from pathlib import Path; p=Path('/data/.ha-probe-"
            + token
            + "'); p.write_text('shared'); p.rename(str(p)+'.ready')",
        )
        k(
            '-n',
            'odoo-dev',
            'exec',
            names[1],
            '--',
            'python3',
            '-c',
            "from pathlib import Path; p=Path('/data/.ha-probe-"
            + token
            + ".ready'); assert p.read_text()=='shared'; p.unlink()",
        )
    finally:
        for name in names:
            delete_helper(name)
    edge = render('odoo-edge', namespace='odoo-edge')
    path = STATE / 'odoo-edge.yaml'
    path.write_bytes(edge)
    run(['bash', str(ROOT / 'scripts/cache-images.sh'), str(path)], timeout=700)
    # The same immutable archive is imported into the host Docker engine.
    if subprocess.run(
        ['docker', 'image', 'inspect', IMAGE_ID],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode:
        archive = ROOT / '.cache/images' / (hashlib.sha256(IMAGE.encode()).hexdigest() + '.tar')
        run(
            [
                'skopeo',
                'copy',
                'oci-archive:' + str(archive),
                'docker-daemon:' + IMAGE.split('@')[0],
            ],
            timeout=180,
        )
    k('-n', 'odoo-edge', 'apply', '-f', '-', input=edge)
    wait(lambda: ready('odoo-edge', 'edge'), timeout=180)
    preflight(True)
    entry_start(private=True)
    if os.environ.get('ODOO_HA_ALLOW_UNINITIALIZED') != '1':
        run([sys.executable, str(ROOT / 'scripts/odoo-ha-session-probe.py')], timeout=90)
    evidence(
        'install',
        shared_storage='NFSv4.1 RWX on both Workers',
        old_claim_retained=True,
        image=IMAGE,
    )


def docker_owned(name):
    r = subprocess.run(['docker', 'inspect', name], capture_output=True)
    if r.returncode:
        return False
    assert json.loads(r.stdout)[0]['Config']['Labels'].get('odoo.io.project') == 'odoo-ha', (
        'Refusing to modify an unrelated container'
    )
    return True


def entry_stop(private=False):
    name = PRIVATE if private else ENTRY
    if docker_owned(name):
        run(['docker', 'rm', '-f', name], timeout=20)


def entry_start(private=False):
    name = PRIVATE if private else ENTRY
    port = 18069 if private else 8069
    if docker_owned(name):
        return
    config = STATE / 'odoo-entry.cfg'
    addresses = [
        run(
            [
                'docker',
                'inspect',
                '-f',
                '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}',
                n,
            ]
        )
        .decode()
        .strip()
        for n in WORKERS
    ]
    config.write_text(
        'global\n  maxconn 1024\ndefaults\n  mode tcp\n  timeout connect 1s\n  timeout client 1h\n  timeout server 1h\n  timeout check 1s\n  retries 0\nfrontend odoo\n  bind :18069\n  default_backend proxies\nbackend proxies\n  balance roundrobin\n  option httpchk GET /_edge/ready\n  http-check expect status 200\n'
        + ''.join(
            f'  server edge{i} {ip}:30669 check inter 1s fall 2 rise 2\n'
            for i, ip in enumerate(addresses)
        )
    )
    with socket.socket() as sock:
        assert sock.connect_ex(('127.0.0.1', port)) != 0, (
            'Required loopback port is occupied by another listener'
        )
    # Exact /32 allow-policy requires an exclusively owned address.
    network = json.loads(run(['docker', 'network', 'inspect', 'kind']))[0]
    assert not any(
        c['IPv4Address'].split('/')[0] == ENTRY_IP for c in network.get('Containers', {}).values()
    ), 'Entry IP is occupied'
    run(
        [
            'docker',
            'run',
            '-d',
            '--name',
            name,
            '--label',
            'odoo.io.project=odoo-ha',
            '--restart',
            'unless-stopped',
            '--network',
            'kind',
            '--ip',
            ENTRY_IP,
            '--user',
            '1000:1000',
            '--read-only',
            '--cap-drop',
            'ALL',
            '--security-opt',
            'no-new-privileges',
            '--memory',
            '128m',
            '--cpus',
            '.25',
            '-p',
            f'127.0.0.1:{port}:18069',
            '-v',
            str(config) + ':/usr/local/etc/haproxy/haproxy.cfg:ro',
            IMAGE_ID,
        ],
        timeout=30,
    )
    wait(lambda: health(port, edge=True), timeout=30, interval=0.5)


def health(port=8069, edge=False):
    path = '/_edge/ready' if edge else '/web/health?db_server_status=1'
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f'http://127.0.0.1:{port}' + path, timeout=2) as response:
            return response.status == 200 and (edge or json.load(response).get('status') == 'pass')
    except (OSError, urllib.error.URLError):
        return False


def authenticated_ready(client, version=None):
    global LAST_AUTH_FAILURE
    phase = 'session'
    try:
        client.session()
        if version:
            phase = 'websocket'
            client.websocket(version)
        LAST_AUTH_FAILURE = None
        return True
    except (OSError, RuntimeError, AssertionError) as error:
        failure = {'phase': phase, 'type': type(error).__name__, 'message': str(error)[:200]}
        if LAST_AUTH_FAILURE != failure:
            print(json.dumps({'private_validation': failure}), flush=True)
        LAST_AUTH_FAILURE = failure
        return False


@contextlib.contextmanager
def private_forward(namespace, target='service/odoo'):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    process = subprocess.Popen(
        ha.K
        + ['-n', namespace, 'port-forward', '--address', '127.0.0.1', target, str(port) + ':8069'],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        wait(lambda: health(port), timeout=20, interval=0.2)
        yield 'http://127.0.0.1:' + str(port)
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def status():
    preflight(True)
    web = ready('odoo-dev', 'server')
    cron = ready('odoo-dev', 'cron')
    edge = ready('odoo-edge', 'edge')
    assert web and cron and edge, (
        'Two Ready instances on distinct Workers are required for every role'
    )
    assert runtime.claim() == 'odoo-shared-data'
    assert obj('odoo-dev', 'pvc', 'odoo-shared-data')['spec']['accessModes'] == ['ReadWriteMany']
    assert docker_owned(ENTRY) and health(), 'Public loopback entry is not healthy'
    for pod in web:
        raw = k(
            '-n',
            'odoo-dev',
            'exec',
            pod['metadata']['name'],
            '--',
            'python3',
            '-c',
            "import configparser,json; c=configparser.ConfigParser(interpolation=None); c.read('/run/odoo/odoo.conf'); print(json.dumps({k:c['options'][k] for k in ['workers','max_cron_threads','db_maxconn','db_maxconn_gevent','proxy_mode','db_sslmode']}))",
        )
        settings = json.loads(raw)
        assert settings == {
            'workers': '2',
            'max_cron_threads': '0',
            'db_maxconn': '3',
            'db_maxconn_gevent': '4',
            'proxy_mode': 'True',
            'db_sslmode': 'verify-full',
        }, settings
    for pod in cron:
        raw = k(
            '-n',
            'odoo-dev',
            'exec',
            pod['metadata']['name'],
            '-c',
            'odoo',
            '--',
            'python3',
            '-c',
            "import configparser,json; from pathlib import Path; c=configparser.ConfigParser(interpolation=None); c.read('/run/odoo/odoo.conf'); assert b'--no-http' in Path('/proc/1/cmdline').read_bytes(); print(json.dumps({k:c['options'][k] for k in ['workers','max_cron_threads','db_maxconn','db_sslmode']}))",
        )
        assert json.loads(raw) == {
            'workers': '0',
            'max_cron_threads': '1',
            'db_maxconn': '4',
            'db_sslmode': 'verify-full',
        }
    return evidence(
        'status',
        web=[p['metadata']['name'] for p in web],
        cron=[p['metadata']['name'] for p in cron],
        edge=[p['metadata']['name'] for p in edge],
        address='http://127.0.0.1:8069',
        failure_scope='Instance HA; host/NFS/host entry remain shared failure points',
    )


COPY_CODE = """import hashlib,json,os,shutil
from pathlib import Path
source=Path(os.environ.get('SOURCE_ROOT','/data'))/os.environ.get('SOURCE_SUBDIR','');target=Path(os.environ.get('TARGET_ROOT','/other'))/os.environ.get('TARGET_SUBDIR','')
for name in ('filestore','sessions'):
 s=source/name;t=target/name
 if not s.exists():continue
 shutil.copytree(s,t,dirs_exist_ok=True)
 # Remove stale destination files only during the exclusive final sync.
 if os.environ.get('FINAL_SYNC')=='1':
  for p in sorted(t.rglob('*'),reverse=True):
   if not (s/p.relative_to(t)).exists():
    shutil.rmtree(p) if p.is_dir() else p.unlink()
 def hashes(root):
  return {str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()}
 if os.environ.get('FINAL_SYNC')=='1':assert hashes(s)==hashes(t),name+' checksum mismatch'
print('Filestore/session copy complete')
"""


def copy_data(
    name, final=False, namespace='odoo-dev', source_subdir='', target_subdir='', reverse=False
):
    assert not any(
        '..' in Path(p).parts or Path(p).is_absolute() for p in (source_subdir, target_subdir)
    )
    k(
        '-n',
        namespace,
        'exec',
        '-i',
        name,
        '--',
        'env',
        'FINAL_SYNC=' + ('1' if final else '0'),
        'SOURCE_SUBDIR=' + source_subdir,
        'TARGET_SUBDIR=' + target_subdir,
        'SOURCE_ROOT=' + ('/other' if reverse else '/data'),
        'TARGET_ROOT=' + ('/data' if reverse else '/other'),
        'python3',
        '-',
        input=COPY_CODE.encode(),
        timeout=60,
    )


def publish(values, message):
    VALUES.write_text(yaml.safe_dump(values, sort_keys=False))
    monitoring = ROOT / 'gitops/environments/dev/monitoring.yaml'
    monitor_values = yaml.safe_load(monitoring.read_text())
    monitor_values['odooHA'] = {
        'enabled': values.get('application', {}).get('mode', 'single') == 'ha'
    }
    monitoring.write_text(yaml.safe_dump(monitor_values, sort_keys=False))
    run(['git', 'add', str(VALUES.relative_to(ROOT)), str(monitoring.relative_to(ROOT))])
    run(
        [
            'git',
            '-c',
            'user.name=Codex Automation',
            '-c',
            'user.email=codex@local.invalid',
            'commit',
            '-m',
            message,
        ],
        timeout=20,
    )
    run(['bash', str(ROOT / 'scripts/gitops-publish.sh')], timeout=20)


def sync():
    for app in ('odoo-edge', 'odoo', 'monitoring'):
        k('apply', '-f', str(ROOT / 'gitops/applications' / (app + '.yaml')))
        run(
            [
                str(ROOT / '.tools/bin/argocd'),
                '--core',
                '--kube-context',
                'kind-odoo-dev',
                'app',
                'sync',
                app,
                '--timeout',
                '180',
            ],
            timeout=190,
        )


def migrate():
    preflight(True)
    original = yaml.safe_load(VALUES.read_text())
    assert original.get('application', {}).get('mode', 'single') == 'single', 'Already in HA mode'
    replicas = runtime.state()
    assert replicas.get('odoo') == 1 and replicas.get('odoo-cron', 0) == 0, (
        'Migration must start with one Odoo writer and no separate cron writer'
    )
    assert runtime.claim() == 'odoo-data', 'Migration expects the retained local data claim'
    assert (EVIDENCE / 'rehearsal.json').exists() and json.loads(
        (EVIDENCE / 'rehearsal.json').read_text()
    )['status'] == 'PASS', 'Run isolated rehearsal before migration'
    new = yaml.safe_load((ROOT / 'gitops/environments/dev/odoo-ha.yaml').read_text())
    entry_start(private=True)
    client_spec = importlib.util.spec_from_file_location(
        'odoo_client', ROOT / 'tests/application/ha.py'
    )
    client_module = importlib.util.module_from_spec(client_spec)
    client_spec.loader.exec_module(client_module)
    existing_session = client_module.Client().authenticate()
    existing_session.call(
        'ir.config_parameter', 'set_param', ['web.base.url', 'http://127.0.0.1:8069']
    )
    existing_session.call('ir.config_parameter', 'set_param', ['web.base.url.freeze', 'True'])
    (STATE / 'odoo-single-values.yaml').write_text(yaml.safe_dump(original, sort_keys=False))
    committed = False
    started = None
    switched = False

    def interrupted(signum, frame):
        raise InterruptedError('Odoo migration interrupted')

    handlers = {s: signal.signal(s, interrupted) for s in (signal.SIGINT, signal.SIGTERM)}
    with maintenance():
        name = 'odoo-ha-transfer'
        try:
            helper(name, runtime.claim(), second_claim='odoo-shared-data')
            copy_data(name)
            started = time.monotonic()
            ha.DEADLINE = started + 240
            run([sys.executable, str(ROOT / 'scripts/forwards.py'), 'stop', 'odoo'])
            runtime.snapshot(STATE / 'odoo-migration-runtime.json')
            runtime.stop(timeout=35)
            baseline = ha.db_baseline('crunchy')
            # Consistent backup from the original data claim before switching.
            ha.helper_start()
            try:
                directory, _ = ha.capture('crunchy', baseline, started)
            finally:
                ha.helper_stop()
            copy_data(name, True)
            private = copy.deepcopy(new)
            private['cron']['replicaCount'] = 0
            switched = True
            deploy(private)
            wait(lambda: ready('odoo-dev', 'server'), timeout=60, interval=1)
            wait(lambda: health(18069), timeout=30, interval=0.5)
            assert ha.db_baseline('crunchy') == baseline, (
                'Database changed before closed validation'
            )
            existing_session.url = 'http://127.0.0.1:18069'
            version = (
                k(
                    '-n',
                    'odoo-dev',
                    'exec',
                    ready('odoo-dev', 'server')[0]['metadata']['name'],
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
            wait(lambda: authenticated_ready(existing_session, version), timeout=30, interval=0.5)
            environment = os.environ.copy()
            environment['TEST_URL'] = 'http://127.0.0.1:18069'
            result = subprocess.run(
                [sys.executable, str(ROOT / 'tests/application/check.py')],
                env=environment,
                timeout=ha.remaining(30),
                capture_output=True,
            )
            assert result.returncode == 0, (
                'Private Odoo validation failed: ' + result.stderr.decode()[-500:]
            )
            marker = {
                'utc': ha.now(),
                'backup': str(directory),
                'source_claim': 'odoo-data',
                'target_claim': 'odoo-shared-data',
            }
            marker_path = STATE / 'odoo-ha-committed.json'
            temporary = marker_path.with_suffix('.partial')
            temporary.write_text(json.dumps(marker))
            temporary.chmod(0o600)
            with temporary.open('rb') as stream:
                os.fsync(stream.fileno())
            temporary.replace(marker_path)
            committed = True
            ha.DEADLINE = None
            deploy(new)
            entry_stop(private=True)
            entry_start()
            wait(health, timeout=30, interval=0.5)
            elapsed = time.monotonic() - started
            publish(new, 'Run Odoo web and cron replicas on shared NFS through HA proxies')
            sync()
            wait(lambda: ready('odoo-dev', 'cron'), timeout=90)
            evidence(
                'migration',
                status='PASS' if elapsed <= 300 else 'FAIL',
                seconds=round(elapsed, 3),
                target_seconds=300,
                within_target=elapsed <= 300,
                backup=str(directory),
                database='Existing Crunchy HA unchanged',
                old_claim_retained=True,
                retained_session=True,
            )
        except BaseException:
            ha.DEADLINE = None
            if LAST_AUTH_FAILURE:
                evidence('private-validation-failure', status='FAIL', **LAST_AUTH_FAILURE)
            if not committed and started is not None:
                rollback_started = time.monotonic()
                runtime.stop(timeout=50)
                if switched:
                    copy_data(name, True, reverse=True)
                deploy(original)
                wait(lambda: ready('odoo-dev', 'server', 1), timeout=45, interval=0.5)
                entry_stop(private=True)
                run(
                    [sys.executable, str(ROOT / 'scripts/forwards.py'), 'start', 'odoo'], timeout=20
                )
                evidence(
                    'precommit-rollback',
                    seconds=round(time.monotonic() - rollback_started, 3),
                    total_outage_seconds=round(time.monotonic() - started, 3),
                )
            raise
        finally:
            ha.DEADLINE = None
            delete_helper(name)
            for s, handler in handlers.items():
                signal.signal(s, handler)
    status()


def rollback():
    with maintenance():
        assert runtime.claim() == 'odoo-shared-data'
        original = yaml.safe_load((STATE / 'odoo-single-values.yaml').read_text())
        assert original['database']['backend'] == 'crunchy', (
            'Rollback must preserve the authoritative Crunchy database'
        )
        name = 'odoo-ha-reverse-transfer'
        try:
            helper(name, 'odoo-shared-data', second_claim='odoo-data')
            runtime.snapshot(STATE / 'odoo-reverse-runtime.json')
            runtime.stop()
            copy_data(name, True)
            deploy(original)
            wait(lambda: ready('odoo-dev', 'server', 1), timeout=90)
            entry_stop()
            publish(original, 'Return Odoo to one instance with freshly synchronized filestore')
            run([sys.executable, str(ROOT / 'scripts/forwards.py'), 'start', 'odoo'], timeout=20)
            k('-n', 'odoo-dev', 'delete', 'deployment', 'odoo-cron', '--ignore-not-found')
            evidence(
                'rollback',
                fresh_reverse_copy=True,
                database_backend='crunchy',
                old_data_never_reused_without_sync=True,
            )
        finally:
            delete_helper(name)


def rehearse():
    archives = list((ROOT / 'backups').glob('*/manifest.json'))
    assert archives, 'A verified ERP backup is required for rehearsal'
    archive = max(archives, key=lambda p: p.stat().st_mtime_ns).parent
    env = os.environ.copy()
    env['BACKUP'] = str(archive)
    env['ERP_RESTORE_ODOO_HA_REHEARSAL'] = '1'
    subprocess.run(
        ['bash', str(ROOT / 'tests/database/restore.sh')], env=env, check=True, timeout=900
    )
    evidence('rehearsal', isolated=True, backup=str(archive), forward_and_reverse_copy=True)


def isolated_rehearsal(original, namespace):
    """Exercise forward and fresh reverse file transfer on a disposable ERP clone."""
    token = os.urandom(6).hex()
    volume = 'odoo-rehearsal-' + token
    directory = Path('/srv/odoo/odoo-data') / ('.rehearsal-' + token)
    directory.mkdir(mode=0o2770)
    directory.chmod(0o2770)
    pv = yaml.safe_load_all((ROOT / 'infrastructure/odoo-ha/storage.yaml').read_text())
    pv, pvc = list(pv)
    pv['metadata']['name'] = volume
    pv['spec']['claimRef'] = {'namespace': namespace, 'name': volume}
    pvc['metadata'].update(name=volume, namespace=namespace)
    pvc['spec']['volumeName'] = volume
    apply(pv)
    apply(pvc)
    wait(lambda: obj(namespace, 'pvc', volume)['status']['phase'] == 'Bound', timeout=30)
    name = 'odoo-ha-rehearsal-copy'
    new = copy.deepcopy(original)
    new.update(
        application={'mode': 'ha'},
        replicaCount=2,
        workers=2,
        cronThreads=0,
        dbMaxConnections=3,
        proxyMode=True,
        workerMemory={'soft': 402653184, 'hard': 536870912},
        cron={'enabled': True, 'replicaCount': 0, 'threads': 1},
        storage={
            'existingClaim': volume,
            'accessModes': ['ReadWriteMany'],
            'dataDir': '/var/lib/odoo/' + directory.name,
        },
    )
    original_snapshot = (ROOT / 'evidence/application-odoo-restore-test.json.snapshot').read_bytes()
    client_spec = importlib.util.spec_from_file_location(
        'odoo_client', ROOT / 'tests/application/ha.py'
    )
    client_module = importlib.util.module_from_spec(client_spec)
    client_spec.loader.exec_module(client_module)

    def check():
        env = os.environ.copy()
        env['TEST_NAMESPACE'] = namespace
        env.pop('TEST_URL', None)
        subprocess.run(
            [sys.executable, str(ROOT / 'tests/application/check.py')],
            env=env,
            check=True,
            timeout=90,
        )
        assert (
            ROOT / 'evidence/application-odoo-restore-test.json.snapshot'
        ).read_bytes() == original_snapshot

    try:
        helper(name, 'odoo-data', namespace=namespace, second_claim=volume)
        k('-n', namespace, 'scale', 'deployment/odoo', '--replicas=0')
        k(
            '-n',
            namespace,
            'wait',
            '--for=delete',
            'pod',
            '-l',
            'odoo.io/component=server',
            '--timeout=90s',
            timeout=100,
        )
        copy_data(name, True, namespace, target_subdir=directory.name)
        delete_helper(name, namespace)
        deploy(new, namespace)
        wait(lambda: ready(namespace, 'server'), timeout=90)
        check()
        pods = ready(namespace, 'server')
        payload = b'Fresh HA attachment must survive reverse transfer'
        with private_forward(namespace, 'pod/' + pods[0]['metadata']['name']) as url:
            client = client_module.Client(url).authenticate()
            identifier = client.call(
                'ir.attachment',
                'create',
                [
                    {
                        'name': 'odoo-reverse-' + token,
                        'res_model': 'res.partner',
                        'res_id': json.loads(original_snapshot)['partner_id'],
                        'datas': base64.b64encode(payload).decode(),
                    }
                ],
            )
        with private_forward(namespace, 'pod/' + pods[1]['metadata']['name']) as url:
            client.url = url
            client.session()
            assert (
                client.opener.open(
                    url + '/web/content/' + str(identifier) + '?download=true', timeout=5
                ).read()
                == payload
            )
        k('-n', namespace, 'scale', 'deployment/odoo', '--replicas=0')
        k(
            '-n',
            namespace,
            'wait',
            '--for=delete',
            'pod',
            '-l',
            'odoo.io/component=server',
            '--timeout=90s',
            timeout=100,
        )
        helper(name, volume, namespace=namespace, second_claim='odoo-data')
        copy_data(name, True, namespace, source_subdir=directory.name)
        delete_helper(name, namespace)
        deploy(original, namespace)
        wait(lambda: ready(namespace, 'server', 1), timeout=90)
        check()
        with private_forward(namespace) as url:
            client.url = url
            client.session()
            assert (
                client.opener.open(
                    url + '/web/content/' + str(identifier) + '?download=true', timeout=5
                ).read()
                == payload
            )
            client.call('ir.attachment', 'unlink', [[identifier]])
        evidence(
            'rollback-rehearsal',
            namespace=namespace,
            isolated=True,
            fresh_reverse_copy=True,
            shared_web_replicas=2,
            database_preserved=True,
        )
    finally:
        delete_helper(name, namespace)
        k('-n', namespace, 'scale', 'deployment/odoo', '--replicas=0')
        k(
            '-n',
            namespace,
            'wait',
            '--for=delete',
            'pod',
            '-l',
            'odoo.io/component=server',
            '--timeout=90s',
            timeout=100,
        )
        k(
            '-n',
            namespace,
            'delete',
            'pvc',
            volume,
            '--ignore-not-found',
            '--wait=true',
            '--timeout=30s',
            timeout=35,
        )
        k('delete', 'pv', volume, '--ignore-not-found', '--wait=true', '--timeout=30s', timeout=35)
        shutil.rmtree(directory)


if __name__ == '__main__':
    action = sys.argv[1]
    if action == 'install':
        install()
    elif action == 'status':
        status()
    elif action == 'migrate':
        migrate()
    elif action == 'rollback':
        rollback()
    elif action == 'rehearse':
        rehearse()
    elif action == 'entry-start':
        entry_start()
    elif action == 'entry-stop':
        entry_stop()
    elif action == 'entry-stop-private':
        entry_stop(private=True)
    elif action == 'test':
        status()
        subprocess.run(
            [sys.executable, str(ROOT / 'tests/application/ha.py')], check=True, timeout=1800
        )
    else:
        raise SystemExit('Unknown Odoo HA operation')
