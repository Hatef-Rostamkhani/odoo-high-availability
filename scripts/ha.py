#!/usr/bin/env python3
"""Crunchy HA operations, measured gates, private credentials, and bounded cutover."""

import base64
import contextlib
import copy
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import tarfile
import time
import yaml

from database import ROOT, K, kubectl, primary, active
from odoo_platform.ha_config import backend_values, clone_manifest
from odoo_platform.runtime import CommandRunner, SecretRedactor

NS = 'postgres-ha'
CLUSTER = 'odoo-ha'
STATE = ROOT / '.state'
EVIDENCE = ROOT / 'evidence/ha'
EVIDENCE.mkdir(parents=True, exist_ok=True)
DEADLINE = None


def remaining(default):
    if DEADLINE is None:
        return default
    seconds = min(default, DEADLINE - time.monotonic())
    if seconds <= 0:
        raise TimeoutError('Migration pre-commit deadline exceeded')
    return seconds


def run(command, input=None, timeout=180):
    redactor = SecretRedactor.from_files(
        [STATE / 'credentials.json', STATE / 'ha-credentials.json']
    )
    return CommandRunner(ROOT, redactor).run(command, data=input, timeout=remaining(timeout))


def k(*args, input=None, timeout=30):
    return run(K + list(args), input=input, timeout=timeout)


def obj(ns, kind, name):
    return json.loads(k('-n', ns, 'get', kind, name, '-o', 'json'))


def apply(document):
    k('apply', '-f', '-', input=json.dumps(document).encode())


def shell(script, *args, timeout=180):
    return run(['bash', str(ROOT / 'scripts' / script), *args], timeout=timeout)


def sql(statement, pod=None, database='ha_tests', cluster=CLUSTER, timeout=30):
    return (
        k(
            '-n',
            NS,
            'exec',
            '-i',
            pod or primary(cluster),
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
            input=statement.encode(),
            timeout=timeout,
        )
        .decode()
        .strip()
    )


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def fingerprint():
    digest = hashlib.sha256()
    for name in sorted(run(['git', 'ls-files'], timeout=10).decode().splitlines()):
        path = ROOT / name
        if path.is_file():
            digest.update(name.encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def evidence(name, status='PASS', **details):
    record = {
        'test': name,
        'status': status,
        'utc': now(),
        'revision': run(['git', 'rev-parse', 'HEAD']).decode().strip(),
        'source_sha256': fingerprint(),
        'versions': json.loads((ROOT / 'versions.lock.json').read_text()),
        **details,
    }
    path = EVIDENCE / (name + '.json')
    path.write_text(json.dumps(record, indent=2) + '\n')
    print(json.dumps(record, ensure_ascii=False), flush=True)
    return record


def wait(predicate, timeout=180, interval=2):
    end = time.monotonic() + remaining(timeout)
    while time.monotonic() < end:
        try:
            value = predicate()
            if value:
                return value
        except (RuntimeError, subprocess.TimeoutExpired):
            pass
        time.sleep(min(interval, max(0, end - time.monotonic())))
    raise TimeoutError('HA readiness/operation timed out')


def database_pods(cluster=CLUSTER):
    objects = json.loads(
        k(
            '-n',
            NS,
            'get',
            'pods',
            '-l',
            f'postgres-operator.crunchydata.com/cluster={cluster}',
            '-o',
            'json',
        )
    )['items']
    return [
        p
        for p in objects
        if not p['metadata'].get('deletionTimestamp')
        and any(c['name'] == 'database' for c in p['spec']['containers'])
    ]


def healthy(cluster=CLUSTER, count=3):
    pods = database_pods(cluster)
    if len(pods) != count or any(
        not any(
            c['type'] == 'Ready' and c['status'] == 'True'
            for c in p['status'].get('conditions', [])
        )
        for p in pods
    ):
        return False
    leaders = [
        p
        for p in pods
        if sql(
            'SELECT pg_is_in_recovery()',
            p['metadata']['name'],
            cluster=cluster,
            database='postgres',
        )
        == 'f'
    ]
    if len(leaders) != 1:
        return False
    if count == 3:
        if len({p['spec']['nodeName'] for p in pods}) != 3:
            return False
        rows = sql(
            "SELECT count(*),count(*) FILTER (WHERE sync_state='sync'),count(*) FILTER (WHERE state='streaming') FROM pg_stat_replication",
            leaders[0]['metadata']['name'],
        )
        if rows != '2|1|2':
            return False
    return pods


def status():
    pods = healthy()
    assert pods, (
        'Three Ready instances, three distinct nodes, one primary and one synchronous standby are required'
    )
    configuration = (
        json.loads(
            k(
                '-n',
                NS,
                'exec',
                primary(),
                '-c',
                'database',
                '--',
                'patronictl',
                'show-config',
                '--format',
                'json',
            )
        )
        if False
        else k(
            '-n', NS, 'exec', primary(), '-c', 'database', '--', 'patronictl', 'show-config'
        ).decode()
    )
    cfg = yaml.safe_load(configuration)
    assert (
        cfg['synchronous_mode'] is True
        and cfg['synchronous_mode_strict'] is True
        and cfg['synchronous_node_count'] == 1
    ), cfg
    assert cfg['ttl'] == 30 and cfg['loop_wait'] == 10, cfg
    assert sql('SHOW synchronous_commit') == 'on'
    assert sql('SHOW max_connections') == '60'
    assert all(
        sql('SHOW server_version', p['metadata']['name'], database='postgres') == '17.10'
        for p in pods
    )
    assert (
        k('-n', NS, 'exec', primary(), '-c', 'database', '--', 'pgbackrest', '--version')
        .decode()
        .strip()
        == 'pgBackRest 2.58.0'
    )
    return evidence(
        'replication',
        instances=[
            {
                'pod': p['metadata']['name'],
                'node': p['spec']['nodeName'],
                'role': p['metadata']['labels'].get('postgres-operator.crunchydata.com/role'),
            }
            for p in pods
        ],
        replication=sql('SELECT application_name,state,sync_state FROM pg_stat_replication'),
        patroni=cfg,
    )


@contextlib.contextmanager
def maintenance():
    with (STATE / 'maintenance.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Another maintenance operation is active') from None
        process = None
        previous = {
            name: os.environ.get(name)
            for name in ('ODOO_ROTATION_MAINTENANCE_TOKEN', 'ODOO_ROTATION_MAINTENANCE_HELD')
        }
        try:
            cfg = json.loads(
                obj('odoo-dev', 'configmap', 'odoo-configuration')['data']['settings.json']
            )
            if cfg.get('credentialsMode', 'static') == 'rotating' and not os.environ.get(
                'ODOO_ROTATION_MAINTENANCE_TOKEN'
            ):
                import select

                process = subprocess.Popen(
                    [
                        str(ROOT / '.tools/secrets-python/bin/python'),
                        str(ROOT / 'scripts/secrets-maintenance.py'),
                        '--hold',
                        '--budget',
                        '1800',
                    ],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                if not select.select([process.stdout], [], [], 30)[0]:
                    raise RuntimeError('Database maintenance lease was not acquired')
                line = process.stdout.readline().strip()
                if not line.startswith('READY '):
                    raise RuntimeError('Database maintenance lease was not acquired')
                os.environ['ODOO_ROTATION_MAINTENANCE_TOKEN'] = line.split()[1]
                os.environ['ODOO_ROTATION_MAINTENANCE_HELD'] = '1'
            yield
        finally:
            if process:
                try:
                    process.communicate('RELEASE\n', timeout=30)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    process.communicate(timeout=10)
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


def preflight(after_images=False):
    memory = dict(line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())
    available = int(memory['MemAvailable'].split()[0]) * 1024
    import shutil

    free = shutil.disk_usage(ROOT).free
    required = (20 if after_images else 35) * 1024**3
    assert free >= required, (
        f'Requires {required // 1024**3} GiB free; available {free / 1024**3:.2f} GiB'
    )
    assert available >= 12 * 1024**3, (
        f'Requires 12 GiB available RAM; available {available / 1024**3:.2f} GiB'
    )
    nodes = json.loads(k('get', 'nodes', '-o', 'json'))['items']
    assert len(nodes) == 3 and all(
        n['status']['nodeInfo']['kubeletVersion'] == 'v1.35.8' for n in nodes
    )
    api = obj('default', 'service', 'kubernetes')['spec']['clusterIP']
    control = next(n for n in nodes if n['metadata']['name'] == 'odoo-dev-control-plane')
    address = next(
        a['address'] for a in control['status']['addresses'] if a['type'] == 'InternalIP'
    )
    import ipaddress

    ipaddress.ip_address(api)
    ipaddress.ip_address(address)
    return evidence(
        'resources-after-images' if after_images else 'resources',
        available_memory_bytes=available,
        free_disk_bytes=free,
        api_service=api,
        control_plane=address,
    )


def pin_images():
    cluster_path = ROOT / 'infrastructure/database-ha/cluster.yaml'
    cluster = yaml.safe_load(cluster_path.read_text())
    operator_path = ROOT / 'infrastructure/database-ha/operator-values.yaml'
    operator = yaml.safe_load(operator_path.read_text())
    operator.setdefault('controllerImages', {})['cluster'] = operator.get(
        'controllerImages', {}
    ).get(
        'cluster', 'registry.developers.crunchydata.com/crunchydata/postgres-operator:ubi9-6.0.2-0'
    )
    holders = [
        (operator['controllerImages'], 'cluster'),
        (cluster['spec'], 'image'),
        (cluster['spec']['backups']['pgbackrest'], 'image'),
        (cluster['spec']['monitoring']['pgmonitor']['exporter'], 'image'),
    ]
    records = []
    for holder, key in holders:
        original = holder[key].split('@')[0]
        cachekey = hashlib.sha256(original.encode()).hexdigest()
        archive = ROOT / '.cache/images' / (cachekey + '.tar')
        if not archive.exists():
            run(
                [sys.executable, str(ROOT / 'scripts/fetch-image.py'), original, str(archive)],
                timeout=1300,
            )
        with tarfile.open(archive) as stream:
            index = json.load(stream.extractfile('index.json'))
            digest = index['manifests'][0]['digest']
            with stream.extractfile('blobs/sha256/' + digest.split(':')[1]) as blob:
                assert hashlib.file_digest(blob, 'sha256').hexdigest() == digest.split(':')[1]
        holder[key] = original + '@' + digest
        records.append(
            {'image': original, 'digest': digest, 'archive': str(archive.relative_to(ROOT))}
        )
        pinnedkey = hashlib.sha256(holder[key].encode()).hexdigest()
        alias = archive.parent / (pinnedkey + '.tar')
        if not alias.exists():
            os.link(archive, alias)
    cluster_path.write_text(yaml.safe_dump(cluster, sort_keys=False))
    operator_path.write_text(yaml.safe_dump(operator, sort_keys=False))
    (ROOT / 'infrastructure/database-ha/images.lock.json').write_text(
        json.dumps(records, indent=2) + '\n'
    )


def private_secret():
    path = STATE / 'ha-credentials.json'
    if path.exists():
        password = json.loads(path.read_text())['odoo']
    else:
        password = secrets.token_urlsafe(36)
        path.touch(mode=0o600)
        path.write_text(json.dumps({'odoo': password}) + '\n')
        path.chmod(0o600)
    secret = {
        'apiVersion': 'v1',
        'kind': 'Secret',
        'metadata': {
            'name': 'odoo-ha-pguser-odoo',
            'namespace': NS,
            'labels': {
                'postgres-operator.crunchydata.com/cluster': CLUSTER,
                'postgres-operator.crunchydata.com/pguser': 'odoo',
            },
        },
        'type': 'Opaque',
        'stringData': {'password': password},
    }
    # Avoid client-side last-applied annotations containing a plaintext password.
    existing = subprocess.run(
        K + ['-n', NS, 'get', 'secret', secret['metadata']['name']], capture_output=True
    )
    if existing.returncode:
        k('create', '-f', '-', input=json.dumps(secret).encode())
    else:
        current = obj(NS, 'secret', secret['metadata']['name'])
        assert base64.b64decode(current['data']['password']).decode() == password, (
            'Existing HA password differs from private state; refusing to overwrite'
        )


def install():
    prepared = ROOT / 'infrastructure/database-ha/images.lock.json'
    archives_ready = prepared.exists() and all(
        (ROOT / e['archive']).is_file() for e in json.loads(prepared.read_text())
    )
    preflight(after_images=archives_ready)
    pin_images()
    apply(yaml.safe_load((ROOT / 'infrastructure/database-ha/namespace.yaml').read_text()))
    run(
        [
            str(ROOT / '.tools/bin/helm'),
            'template',
            'pgo',
            str(ROOT / 'vendor/pgo-6.0.2.tgz'),
            '-n',
            NS,
            '-f',
            str(ROOT / 'infrastructure/database-ha/operator-values.yaml'),
        ]
    )
    (STATE / 'pgo.yaml').write_bytes(
        run(
            [
                str(ROOT / '.tools/bin/helm'),
                'template',
                'pgo',
                str(ROOT / 'vendor/pgo-6.0.2.tgz'),
                '-n',
                NS,
                '-f',
                str(ROOT / 'infrastructure/database-ha/operator-values.yaml'),
            ]
        )
    )
    shell('cache-images.sh', str(ROOT / '.state/pgo.yaml'), timeout=900)
    cluster = yaml.safe_load((ROOT / 'infrastructure/database-ha/cluster.yaml').read_text())
    images = {
        'images': [
            {'image': cluster['spec']['image']},
            {'image': cluster['spec']['backups']['pgbackrest']['image']},
            {'image': cluster['spec']['monitoring']['pgmonitor']['exporter']['image']},
        ]
    }
    image_path = STATE / 'ha-images.yaml'
    image_path.write_text(yaml.safe_dump(images))
    shell('cache-images.sh', str(image_path), timeout=1200)
    preflight(after_images=True)
    # Helm installs the vendored CRDs; the independent bootstrap needs no Argo CD.
    run(
        [
            str(ROOT / '.tools/bin/helm'),
            'upgrade',
            '--install',
            'pgo',
            str(ROOT / 'vendor/pgo-6.0.2.tgz'),
            '-n',
            NS,
            '-f',
            str(ROOT / 'infrastructure/database-ha/operator-values.yaml'),
            '--wait',
            '--timeout',
            '5m',
        ],
        timeout=330,
    )
    private_secret()
    for document in yaml.safe_load_all(
        (ROOT / 'infrastructure/database-ha/network-policy.yaml').read_text()
    ):
        apply(document)
    for document in yaml.safe_load_all(
        (ROOT / 'infrastructure/database-ha/api-policy.yaml').read_text()
    ):
        apply(document)
    apply(cluster)
    apply(yaml.safe_load((ROOT / 'infrastructure/database-ha/monitoring.yaml').read_text()))
    wait(healthy, timeout=600)
    status()
    sync_credentials()


def sync_credentials():
    source = obj(NS, 'secret', 'odoo-ha-pguser-odoo')
    ca = obj(NS, 'secret', 'odoo-ha-cluster-cert')['data']['ca.crt']
    settings = (
        json.loads(obj('odoo-dev', 'configmap', 'odoo-configuration')['data']['settings.json'])
        if subprocess.run(
            K + ['-n', 'odoo-dev', 'get', 'configmap', 'odoo-configuration'], capture_output=True
        ).returncode
        == 0
        else {}
    )
    items = [('odoo-ha-ca', {'ca.crt': ca})]
    if settings.get('credentialsMode', 'static') != 'rotating':
        items.insert(0, ('odoo-ha-database', {'password': source['data']['password']}))
    for name, data in items:
        secret = {
            'apiVersion': 'v1',
            'kind': 'Secret',
            'metadata': {'name': name, 'namespace': 'odoo-dev'},
            'type': 'Opaque',
            'data': data,
        }
        current = subprocess.run(
            K + ['-n', 'odoo-dev', 'get', 'secret', name, '-o', 'json'], capture_output=True
        )
        if current.returncode == 0:
            secret['metadata']['resourceVersion'] = json.loads(current.stdout)['metadata'][
                'resourceVersion'
            ]
            k('replace', '-f', '-', input=json.dumps(secret).encode())
        else:
            k('create', '-f', '-', input=json.dumps(secret).encode())
    ownership = sql(
        "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname='odoo_dev'",
        database='postgres',
    )
    if ownership != 'odoo':
        sql('ALTER DATABASE odoo_dev OWNER TO odoo', database='postgres')
    sql('ALTER DATABASE ha_tests OWNER TO "ha-test"', database='postgres')


def client_pod():
    name = 'ha-client'
    try:
        obj(NS, 'pod', name)
    except RuntimeError:
        apply(
            {
                'apiVersion': 'v1',
                'kind': 'Pod',
                'metadata': {
                    'name': name,
                    'namespace': NS,
                    'labels': {
                        'postgres-operator.crunchydata.com/cluster': CLUSTER,
                        'odoo.io/ha-client': 'true',
                    },
                },
                'spec': {
                    'restartPolicy': 'Never',
                    'automountServiceAccountToken': False,
                    'securityContext': {
                        'runAsUser': 1000,
                        'runAsGroup': 1000,
                        'runAsNonRoot': True,
                        'fsGroup': 1000,
                        'seccompProfile': {'type': 'RuntimeDefault'},
                    },
                    'containers': [
                        {
                            'name': 'client',
                            'image': json.loads((ROOT / 'versions.lock.json').read_text())[
                                'odoo_image'
                            ],
                            'command': ['python3', '-c', 'import time; time.sleep(86400)'],
                            'resources': {
                                'requests': {'cpu': '25m', 'memory': '32Mi'},
                                'limits': {'cpu': '200m', 'memory': '128Mi'},
                            },
                            'securityContext': {
                                'allowPrivilegeEscalation': False,
                                'readOnlyRootFilesystem': True,
                                'capabilities': {'drop': ['ALL']},
                            },
                            'volumeMounts': [
                                {'name': 'ca', 'mountPath': '/run/ca', 'readOnly': True},
                                {
                                    'name': 'credentials',
                                    'mountPath': '/run/test-secret',
                                    'readOnly': True,
                                },
                            ],
                        }
                    ],
                    'volumes': [
                        {
                            'name': 'ca',
                            'secret': {
                                'secretName': 'odoo-ha-cluster-cert',
                                'items': [{'key': 'ca.crt', 'path': 'ca.crt'}],
                            },
                        },
                        {
                            'name': 'credentials',
                            'secret': {
                                'secretName': 'odoo-ha-pguser-ha-test',
                                'items': [{'key': 'password', 'path': 'password'}],
                            },
                        },
                    ],
                },
            }
        )
    k('-n', NS, 'wait', '--for=condition=Ready', 'pod/' + name, '--timeout=120s', timeout=130)
    return name


def client(code, timeout=30):
    prelude = """import psycopg2, json, time
from pathlib import Path
args=dict(host='odoo-ha-primary.postgres-ha.svc',port=5432,dbname='ha_tests',user='ha-test',password=Path('/run/test-secret/password').read_text().strip(),sslmode='verify-full',sslrootcert='/run/ca/ca.crt',connect_timeout=5)
"""
    return (
        k(
            '-n',
            NS,
            'exec',
            '-i',
            'ha-client',
            '--',
            'python3',
            '-',
            input=(prelude + code).encode(),
            timeout=timeout,
        )
        .decode()
        .strip()
    )


def tls_test():
    client_pod()
    result = client("""conn=psycopg2.connect(**args)
with conn.cursor() as cur:
 cur.execute('SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()');assert cur.fetchone()[0]
conn.close()
args['sslrootcert']='/etc/ssl/certs/ca-certificates.crt'
try:psycopg2.connect(**args)
except psycopg2.OperationalError:pass
else:raise AssertionError('Untrusted CA connection unexpectedly succeeded')
print('TLS verify-full accepted valid CA and rejected unrelated CA')
""")
    roles = sql(
        "SELECT rolname,rolsuper,rolcreatedb,rolcreaterole FROM pg_roles WHERE rolname='odoo'",
        database='postgres',
    )
    assert roles == 'odoo|f|f|f'
    assert (
        sql(
            "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname='odoo_dev'",
            database='postgres',
        )
        == 'odoo'
    )
    return evidence('tls-privileges', result=result, role=roles)


def scale(kind, name, count, ns=NS):
    k('-n', ns, 'scale', kind + '/' + name, '--replicas=' + str(count))


def pod_set(pod):
    return next(r['name'] for r in pod['metadata']['ownerReferences'] if r['kind'] == 'StatefulSet')


@contextlib.contextmanager
def paused_operator():
    replicas = obj(NS, 'deployment', 'pgo')['spec']['replicas']
    assert replicas == 1, 'Operator must be active before a fault test'
    saved = {}

    def interrupted(signum, frame):
        raise InterruptedError('HA operation interrupted')

    previous = {s: signal.signal(s, interrupted) for s in (signal.SIGTERM, signal.SIGINT)}
    try:
        scale('deployment', 'pgo', 0)
        wait(
            lambda: (
                len(
                    json.loads(
                        k(
                            '-n',
                            NS,
                            'get',
                            'pods',
                            '-l',
                            'postgres-operator.crunchydata.com/control-plane=pgo',
                            '-o',
                            'json',
                        )
                    )['items']
                )
                == 0
            ),
            timeout=90,
        )
        yield saved
    finally:
        failures = []
        for name, count in saved.items():
            try:
                scale('statefulset', name, count)
            except Exception:
                failures.append(name)
        try:
            scale('deployment', 'pgo', replicas)
        except Exception:
            failures.append('pgo')
        for s, h in previous.items():
            signal.signal(s, h)
        if failures:
            raise RuntimeError('Cleanup failed for ' + ','.join(failures))
        wait(healthy, timeout=300)


def failover_test():
    status()
    tls_test()
    client_pod()
    token = secrets.token_hex(12)
    client(
        f"""conn=psycopg2.connect(**args)
with conn:
 with conn.cursor() as cur:
  cur.execute('CREATE TABLE IF NOT EXISTS ha_acknowledged(token text,id integer,PRIMARY KEY(token,id))')
for i in range(100):
 with conn:
  with conn.cursor() as cur:cur.execute('INSERT INTO ha_acknowledged VALUES (%s,%s)',({token!r},i))
conn.close()
print('100 transactions acknowledged over verified TLS')
""",
        timeout=45,
    )
    old = primary()
    pod = next(p for p in database_pods() if p['metadata']['name'] == old)
    name = pod_set(pod)
    with paused_operator() as saved:
        saved[name] = obj(NS, 'statefulset', name)['spec']['replicas']
        started = time.monotonic()
        scale('statefulset', name, 0)
        new = wait(lambda: p if (p := primary()) != old else None, timeout=90, interval=1)
        assert sql(f"SELECT count(*) FROM ha_acknowledged WHERE token='{token}'", pod=new) == '100'
        # A successful client commit through the fixed Service proves recovery.
        wait(
            lambda: (
                client(
                    "conn=psycopg2.connect(**args)\nwith conn:\n with conn.cursor() as cur:cur.execute('CREATE TABLE IF NOT EXISTS ha_recovery(id integer)')\nconn.close()\nprint('ok')",
                    timeout=8,
                )
                == 'ok'
            ),
            timeout=max(1, 90 - (time.monotonic() - started)),
            interval=1,
        )
        elapsed = time.monotonic() - started
        assert elapsed <= 90
    evidence(
        'failover',
        old_primary=old,
        new_primary=new,
        seconds=round(elapsed, 3),
        acknowledged_preserved=100,
        cleanup='Three Ready instances and active operator',
        kind_nodes='never stopped',
    )
    strict_test()


def strict_test():
    client_pod()
    healthy_pods = wait(healthy)
    leader = primary()
    replicas = [p for p in healthy_pods if p['metadata']['name'] != leader]
    sql(
        'CREATE TABLE IF NOT EXISTS ha_strict(token text PRIMARY KEY); ALTER TABLE ha_strict OWNER TO "ha-test"'
    )
    with paused_operator() as saved:
        for pod in replicas:
            name = pod_set(pod)
            saved[name] = obj(NS, 'statefulset', name)['spec']['replicas']
            scale('statefulset', name, 0)
        wait(lambda: sql('SELECT count(*) FROM pg_stat_replication') == '0', timeout=90)
        time.sleep(12)
        result = client(
            """import threading, os
conn=psycopg2.connect(**args)
conn.autocommit=True
finished=threading.Event();outcome={}
def write():
 try:
  with conn.cursor() as cur:cur.execute('INSERT INTO ha_strict VALUES (%s)',(str(time.time()),))
  outcome['acknowledged']=True
 except Exception:
  outcome['failed']=True
 finally:finished.set()
threading.Thread(target=write,daemon=True).start()
if finished.wait(5):
 if outcome.get('acknowledged'):raise AssertionError('Strict mode acknowledged write without any standby')
 raise AssertionError('Write failed before the strict blocking interval')
# Abandon the timed-out write. Its local commit may be ambiguous, so it is never
# part of the set of acknowledged transactions checked by the failover test.
print('BLOCKED_WRITE',flush=True)
conn.cancel()
os._exit(0)
""",
            timeout=20,
        )
        assert result == 'BLOCKED_WRITE'
    client("""conn=psycopg2.connect(**args)
with conn:
 with conn.cursor() as cur:cur.execute('INSERT INTO ha_strict VALUES (%s)',(str(time.time()),))
conn.close()
print('write recovered')
""")
    evidence(
        'strict',
        unavailable_standbys=2,
        write_acknowledged=False,
        recovered=True,
        note='A cancelled commit is not counted as acknowledged',
    )


def repo_pod():
    objects = json.loads(
        k(
            '-n',
            NS,
            'get',
            'pods',
            '-l',
            f'postgres-operator.crunchydata.com/cluster={CLUSTER},postgres-operator.crunchydata.com/data=pgbackrest',
            '-o',
            'json',
        )
    )['items']
    return next(
        p['metadata']['name']
        for p in objects
        if any(c['name'] == 'pgbackrest' for c in p['spec']['containers'])
    )


def backup_info():
    return json.loads(
        k(
            '-n',
            NS,
            'exec',
            repo_pod(),
            '-c',
            'pgbackrest',
            '--',
            'pgbackrest',
            '--stanza=db',
            '--output=json',
            'info',
        )
    )


def full_backup():
    started = time.monotonic()
    before = {b['label'] for s in backup_info() for b in s.get('backup', [])}
    k(
        '-n',
        NS,
        'patch',
        'postgrescluster',
        CLUSTER,
        '--type=merge',
        '-p',
        json.dumps(
            {
                'spec': {
                    'backups': {
                        'pgbackrest': {'manual': {'repoName': 'repo1', 'options': ['--type=full']}}
                    }
                }
            }
        ),
    )
    k(
        '-n',
        NS,
        'annotate',
        'postgrescluster',
        CLUSTER,
        'postgres-operator.crunchydata.com/pgbackrest-backup='
        + dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'),
        '--overwrite',
    )

    def finished():
        return next(
            (
                b
                for s in backup_info()
                for b in s.get('backup', [])
                if b['label'] not in before and b['type'] == 'full'
            ),
            None,
        )

    completed = wait(finished, timeout=600)
    return evidence(
        'backup',
        seconds=round(time.monotonic() - started, 3),
        backup=completed,
        repository='host-local PVC',
        schedule_utc='03:00 daily',
        schedule_tehran='06:30 daily',
        retained_full=2,
    )


def restore_test():
    wait(healthy)
    client_pod()
    token = secrets.token_hex(12)
    sql(
        'CREATE TABLE IF NOT EXISTS ha_pitr_sentinel(token text,phase text,PRIMARY KEY(token,phase))'
    )
    full_backup()
    sql(f"INSERT INTO ha_pitr_sentinel VALUES ('{token}','before')")
    time.sleep(2)
    target = (
        sql("SELECT to_char(clock_timestamp() AT TIME ZONE 'UTC','YYYY-MM-DD HH24:MI:SS.US')")
        + '+00'
    )
    time.sleep(2)
    sql(f"INSERT INTO ha_pitr_sentinel VALUES ('{token}','after')")
    segment = sql('SELECT pg_walfile_name(pg_current_wal_lsn())')
    sql('SELECT pg_switch_wal()')
    # Require the precise WAL segment that contains the after-target commit.
    wait(
        lambda: sql(f"SELECT last_archived_wal >= '{segment}' FROM pg_stat_archiver") == 't',
        timeout=120,
    )
    name = 'odoo-ha-pitr'
    exists = subprocess.run(
        K + ['-n', NS, 'get', 'postgrescluster', name, '-o', 'json'], capture_output=True
    )
    if exists.returncode == 0:
        owned = json.loads(exists.stdout)
        assert owned['metadata'].get('labels', {}).get('odoo.io/restore-test') == 'true', (
            'Refusing to replace an unowned restore clone'
        )
        k('-n', NS, 'delete', 'postgrescluster', name, '--wait=true', '--timeout=180s', timeout=190)
        # Delete only test-owned PVCs to avoid using a previous clone's PGDATA.
        k(
            '-n',
            NS,
            'delete',
            'pvc',
            '-l',
            'postgres-operator.crunchydata.com/cluster=' + name,
            '--wait=true',
            '--timeout=120s',
            timeout=130,
        )
    started = time.monotonic()
    apply(clone_manifest(target))
    wait(lambda: healthy(name, count=1), timeout=600)
    found = sql(
        f"SELECT phase FROM ha_pitr_sentinel WHERE token='{token}' ORDER BY phase",
        database='ha_tests',
        cluster=name,
    )
    assert found == 'before', 'PITR must contain before marker and exclude after marker'
    evidence(
        'pitr',
        target_utc=target,
        seconds=round(time.monotonic() - started, 3),
        before_present=True,
        after_absent=True,
        clone=name,
        scope='Database PITR only; not complete ERP recovery',
    )
    # The disposable clone is owned by this test. Its PVCs are explicitly scoped.
    k('-n', NS, 'delete', 'postgrescluster', name, '--wait=true', '--timeout=180s', timeout=190)
    k(
        '-n',
        NS,
        'delete',
        'pvc',
        '-l',
        'postgres-operator.crunchydata.com/cluster=' + name,
        '--wait=true',
        '--timeout=120s',
        timeout=130,
    )


def switchover():
    old = primary()
    k(
        '-n',
        NS,
        'exec',
        old,
        '-c',
        'database',
        '--',
        'patronictl',
        'switchover',
        '--force',
        timeout=60,
    )
    new = wait(lambda: p if (p := primary()) != old else None, timeout=90)
    wait(healthy, timeout=90)
    return old, new


def db_command(backend, args, input=None, timeout=60):
    if backend == 'crunchy':
        ns, pod, container = NS, primary(), 'database'
    else:
        ns, pod, container = 'odoo-dev', 'postgresql-0', 'postgres'
    return k(
        '-n', ns, 'exec', '-i', pod, '-c', container, '--', *args, input=input, timeout=timeout
    )


def app_snapshot():
    run(
        ['env', 'ODOO_TEST_PRIVATE=true', 'bash', str(ROOT / 'tests/application/run.sh')],
        timeout=75,
    )
    return json.loads((ROOT / 'evidence/application-odoo-dev.json.snapshot').read_text())


def db_baseline(backend, database='odoo_dev'):
    statement = """CREATE OR REPLACE FUNCTION pg_temp.data_signature() RETURNS TABLE(table_name text,row_count bigint,content_md5 text) LANGUAGE plpgsql AS $$
DECLARE item record;
BEGIN
 FOR item IN SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename LOOP
  RETURN QUERY EXECUTE format('SELECT %L::text,count(*),md5(coalesce(string_agg(md5(row_to_json(t)::text),%L ORDER BY md5(row_to_json(t)::text)),%L)) FROM public.%I t',item.tablename,'','',item.tablename);
 END LOOP;
END $$;
SELECT json_agg(s ORDER BY table_name) FROM pg_temp.data_signature() s;
"""
    result = (
        db_command(
            backend,
            ['psql', '-X', '-v', 'ON_ERROR_STOP=1', '-U', 'postgres', '-d', database, '-qAt'],
            input=statement.encode(),
            timeout=45,
        )
        .decode()
        .strip()
    )
    return json.loads(result)


def restore_dump(backend, data, database='odoo_dev', metadata=None):
    assert database in ('odoo_dev', 'ha_rehearsal')
    db_command(backend, ['dropdb', '-U', 'postgres', '--if-exists', '--force', database])
    db_command(backend, ['createdb', '-U', 'postgres', '--owner=odoo', database])
    import importlib.util

    module_spec = importlib.util.spec_from_file_location(
        'restore_archive', ROOT / 'scripts/restore-archive.py'
    )
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    module.restore_archive(
        lambda args, **kwargs: db_command(backend, args, **kwargs),
        data,
        database,
        omittable_extensions=module.permitted_omissions(metadata or {}),
    )


def gitops_sync(app):
    revision = run(['git', 'rev-parse', 'HEAD']).decode().strip()
    run(
        [
            str(ROOT / '.tools/bin/argocd'),
            '--core',
            '--kube-context',
            'kind-odoo-dev',
            'app',
            'sync',
            app,
            '--revision',
            revision,
            '--timeout',
            '60',
        ],
        timeout=70,
    )
    wait(
        lambda: (
            obj('argocd', 'application', app)['status'].get('sync', {}).get('revision') == revision
        ),
        timeout=20,
        interval=0.5,
    )


def publish_values(
    odoo,
    postgres=None,
    message='Configure Odoo database backend',
    odoo_text=None,
    postgres_text=None,
):
    paths = ['gitops/environments/dev/odoo.yaml']
    (ROOT / paths[0]).write_text(yaml.safe_dump(odoo, sort_keys=False))
    if postgres is not None:
        paths.append('gitops/environments/dev/postgresql.yaml')
        (ROOT / paths[-1]).write_text(yaml.safe_dump(postgres, sort_keys=False))
    if odoo_text is not None:
        (ROOT / paths[0]).write_text(odoo_text)
    if postgres_text is not None:
        (ROOT / 'gitops/environments/dev/postgresql.yaml').write_text(postgres_text)
    run(['git', 'add', '--', *paths])
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
        timeout=15,
    )
    shell('gitops-publish.sh', timeout=15)
    # Reconcile only the intended apps, without prune.
    k(
        '-n',
        'argocd',
        'annotate',
        'application',
        'odoo',
        'argocd.argoproj.io/refresh=hard',
        '--overwrite',
    )
    gitops_sync('odoo')
    if postgres is not None:
        k(
            '-n',
            'argocd',
            'annotate',
            'application',
            'postgresql',
            'argocd.argoproj.io/refresh=hard',
            '--overwrite',
        )
        gitops_sync('postgresql')


def stopped_app():
    pods = json.loads(
        k('-n', 'odoo-dev', 'get', 'pods', '-l', 'app.kubernetes.io/name=odoo', '-o', 'json')
    )['items']
    return not any(
        p['metadata'].get('labels', {}).get('odoo.io/component') in ('server', 'cron') for p in pods
    )


def stop_app():
    run(
        [
            sys.executable,
            str(ROOT / 'scripts/odoo-runtime.py'),
            'snapshot',
            str(STATE / 'ha-app-runtime.json'),
        ]
    )
    run([sys.executable, str(ROOT / 'scripts/odoo-runtime.py'), 'stop'], timeout=40)


def start_app():
    values = yaml.safe_load((ROOT / 'gitops/environments/dev/odoo.yaml').read_text()) or {}
    scale('deployment', 'odoo', values.get('replicaCount', 1), ns='odoo-dev')
    cron = values.get('cron', {})
    exists = (
        subprocess.run(
            K + ['-n', 'odoo-dev', 'get', 'deployment', 'odoo-cron'], capture_output=True
        ).returncode
        == 0
    )
    if exists:
        scale(
            'deployment',
            'odoo-cron',
            cron.get('replicaCount', 2) if cron.get('enabled') else 0,
            ns='odoo-dev',
        )


def app_forward_active(records):
    if 'odoo' in records:
        return True
    result = subprocess.run(['docker', 'inspect', 'odoo-entry'], capture_output=True)
    if result.returncode:
        return False
    container = json.loads(result.stdout)[0]
    return (
        container['Config'].get('Labels', {}).get('odoo.io.project') == 'odoo-ha'
        and container['State']['Running']
    )


def helper_start():
    claim = run([sys.executable, str(ROOT / 'scripts/odoo-runtime.py'), 'claim']).decode().strip()
    document = json.loads(
        run(
            [
                sys.executable,
                str(ROOT / 'scripts/volume-helper.py'),
                'odoo-dev',
                'odoo-ha-backup',
                claim,
            ]
        )
    )
    apply(document)
    k(
        '-n',
        'odoo-dev',
        'wait',
        '--for=condition=Ready',
        'pod/odoo-ha-backup',
        '--timeout=30s',
        timeout=35,
    )


def helper_stop():
    k(
        '-n',
        'odoo-dev',
        'delete',
        'pod',
        'odoo-ha-backup',
        '--ignore-not-found',
        '--wait=true',
        '--timeout=20s',
        timeout=25,
    )


def capture(backend, baseline, started):
    directory = (
        ROOT / 'backups' / ('ha-' + dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    )
    directory.mkdir(parents=True, mode=0o700)
    directory.chmod(0o700)
    directory.parent.chmod(0o700)
    module_spec = __import__('importlib.util', fromlist=['']).spec_from_file_location(
        'restore_archive', ROOT / 'scripts/restore-archive.py'
    )
    module = __import__('importlib.util', fromlist=['']).module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    instrumentation = module.instrumentation_policy(
        lambda args, **kwargs: db_command(backend, args, **kwargs)
    )
    data = db_command(
        backend,
        [
            'pg_dump',
            '-U',
            'postgres',
            '-d',
            'odoo_dev',
            '--format=custom',
            '--no-owner',
            '--no-acl',
        ],
        timeout=60,
    )
    for name, content in [
        ('database.dump', data),
        (
            'filestore.tar.gz',
            k(
                '-n',
                'odoo-dev',
                'exec',
                'odoo-ha-backup',
                '--',
                'tar',
                '-C',
                '/data',
                '-czf',
                '-',
                'filestore',
                timeout=40,
            ),
        ),
    ]:
        path = directory / name
        path.touch(mode=0o600)
        path.write_bytes(content)
    manifest = {
        'database': 'odoo_dev',
        'postgresql_major': 17,
        'versions': json.loads((ROOT / 'versions.lock.json').read_text()),
        'revision': run(['git', 'rev-parse', 'HEAD']).decode().strip(),
        'source_sha256': fingerprint(),
        'backend': backend,
        'utc': now(),
        'baseline': baseline,
        'sha256': {
            n: hashlib.sha256((directory / n).read_bytes()).hexdigest()
            for n in ('database.dump', 'filestore.tar.gz')
        },
        'operator_instrumentation': instrumentation,
        'consistency': 'Odoo HTTP and cron fully stopped; original filestore PVC retained',
        'window_seconds': round(time.monotonic() - started, 3),
    }
    (directory / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    for name, digest in manifest['sha256'].items():
        assert hashlib.sha256((directory / name).read_bytes()).hexdigest() == digest
    return directory, data


def require_gates():
    for gate in (
        'replication',
        'tls-privileges',
        'failover',
        'strict',
        'backup',
        'pitr',
        'rehearsal',
    ):
        path = EVIDENCE / (gate + '.json')
        assert path.exists(), f'Missing pre-migration gate: {gate}'
        record = json.loads(path.read_text())
        assert record['status'] == 'PASS', f'Pre-migration gate did not pass: {gate}'
        age = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(record['utc'])
        assert age.total_seconds() < 86400, f'Stale gate: {gate}'
        assert record['source_sha256'] == fingerprint(), (
            f'Source changed since gate: {gate}; rerun HA checks'
        )
    if os.environ.get('HA_ROLLBACK_REHEARSAL') != 'true':
        path = EVIDENCE / 'rollback-rehearsal.json'
        assert path.exists() and json.loads(path.read_text())['status'] == 'PASS', (
            'Run the pre-commit rollback rehearsal before real migration'
        )
    wait(healthy, timeout=60)


def rehearse():
    assert active()[0] == 'standalone', 'Migration rehearsal expects Odoo on standalone'
    started = time.monotonic()
    dump = db_command(
        'standalone',
        [
            'pg_dump',
            '-U',
            'postgres',
            '-d',
            'odoo_dev',
            '--format=custom',
            '--no-owner',
            '--no-acl',
        ],
        timeout=60,
    )
    restore_dump('crunchy', dump, 'ha_rehearsal')
    # Verify the archive is readable and all restored objects belong to odoo.
    assert (
        sql(
            "SELECT count(*) FROM pg_tables WHERE schemaname='public' AND tableowner<>'odoo'",
            database='ha_rehearsal',
        )
        == '0'
    )
    assert int(sql('SELECT count(*) FROM res_users', database='ha_rehearsal')) > 0
    seconds = time.monotonic() - started
    assert seconds < 120, 'Dump/restore rehearsal exceeds safe cutover allowance'
    sql('DROP DATABASE ha_rehearsal WITH (FORCE)', database='postgres')
    evidence(
        'rehearsal', dump_restore_seconds=round(seconds, 3), dump_bytes=len(dump), owner='odoo'
    )


def private_state(path, record):
    temporary = path.with_suffix('.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(record, stream, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def migration(action):
    global DEADLINE
    if action == 'rehearse':
        with maintenance():
            rehearse()
        return
    state_path = STATE / 'ha-migration.json'
    prior = json.loads(state_path.read_text()) if state_path.exists() else {}
    if action == 'rollback' and not prior.get('committed'):
        assert prior and not prior.get('aborted'), 'No pending cutover to roll back'
        with maintenance():
            records = json.loads((STATE / 'forwards.json').read_text())
            if app_forward_active(records):
                run([sys.executable, str(ROOT / 'scripts/forwards.py'), 'stop', 'odoo'], timeout=10)
            stop_app()
            wait(stopped_app, timeout=35)
            publish_values(
                prior['original_values'],
                prior['original_postgresql'],
                'Recover interrupted pre-commit Odoo cutover',
                odoo_text=prior.get('original_odoo_text'),
                postgres_text=prior.get('original_postgres_text'),
            )
            start_app()
            k('-n', 'odoo-dev', 'rollout', 'status', 'deployment/odoo', '--timeout=45s', timeout=50)
            if prior['forward_was_active']:
                run(
                    [sys.executable, str(ROOT / 'scripts/forwards.py'), 'start', 'odoo'], timeout=15
                )
            prior['aborted'] = True
            private_state(state_path, prior)
            evidence('rollback', source_restored=True, committed=False)
        return
    reverse = action == 'rollback'
    source = 'crunchy' if reverse else 'standalone'
    target = 'standalone' if reverse else 'crunchy'
    assert active()[0] == source, 'Unexpected active source backend'
    if not reverse:
        status()
        require_gates()
        sync_credentials()
    with maintenance():
        original_odoo_text = (ROOT / 'gitops/environments/dev/odoo.yaml').read_text()
        original_postgres_text = (ROOT / 'gitops/environments/dev/postgresql.yaml').read_text()
        old_values = yaml.safe_load(original_odoo_text) or {}
        old_postgres = (
            yaml.safe_load((ROOT / 'gitops/environments/dev/postgresql.yaml').read_text()) or {}
        )
        before_app = app_snapshot()
        records = (
            json.loads((STATE / 'forwards.json').read_text())
            if (STATE / 'forwards.json').exists()
            else {}
        )
        forwarded = app_forward_active(records)
        if reverse:
            pg = copy.deepcopy(old_postgres)
            pg['replicaCount'] = 1
            # Prepare old PG before outage; the reverse transfer uses a NEW consistent backup.
            publish_values(old_values, pg, 'Prepare standalone PostgreSQL for reverse migration')
            k(
                '-n',
                'odoo-dev',
                'rollout',
                'status',
                'statefulset/postgresql',
                '--timeout=90s',
                timeout=100,
            )
        started = time.monotonic()
        DEADLINE = started + 240
        committed = False
        changed = False
        helper = False
        state = {
            'utc': now(),
            'source': source,
            'target': target,
            'original_values': old_values,
            'original_postgresql': old_postgres,
            'original_odoo_text': original_odoo_text,
            'original_postgres_text': original_postgres_text,
            'committed': False,
            'forward_was_active': forwarded,
        }
        private_state(state_path, state)

        def interrupted(signum, frame):
            raise InterruptedError('Migration interrupted')

        handlers = {s: signal.signal(s, interrupted) for s in (signal.SIGTERM, signal.SIGINT)}
        try:
            if forwarded:
                run([sys.executable, str(ROOT / 'scripts/forwards.py'), 'stop', 'odoo'], timeout=10)
            with socket.socket() as sock:
                assert sock.connect_ex(('127.0.0.1', 8069)) != 0, (
                    'An unowned listener remains on 8069; cutover refused'
                )
            stop_app()
            wait(stopped_app, timeout=35, interval=0.5)
            baseline = db_baseline(source)
            helper_start()
            helper = True
            directory, data = capture(source, baseline, started)
            restore_dump(
                target, data, metadata=json.loads((directory / 'manifest.json').read_text())
            )
            assert db_baseline(target) == baseline, (
                'Restored database signatures differ from the stopped source'
            )
            helper_stop()
            helper = False
            changed = True
            new_values = backend_values(old_values, target, cron=0)
            publish_values(
                new_values, message='Validate Odoo privately on ' + target + ' PostgreSQL'
            )
            start_app()
            k('-n', 'odoo-dev', 'rollout', 'status', 'deployment/odoo', '--timeout=45s', timeout=50)
            assert app_snapshot() == before_app, (
                'Record IDs or attachment checksum changed during migration'
            )
            if target == 'crunchy':
                old, new = switchover()
                assert app_snapshot() == before_app, (
                    'Odoo failed validation after primary switchover'
                )
                evidence(
                    'odoo-switchover',
                    old_primary=old,
                    new_primary=new,
                    endpoint='odoo-ha-primary.postgres-ha.svc',
                    snapshot=before_app,
                )
            # Deliberate failure exercises the actual automatic rollback path.
            if os.environ.get('HA_ROLLBACK_REHEARSAL') == 'true':
                raise RuntimeError('Requested pre-commit rollback rehearsal')
            remaining(1)
            # Reserve time for commit, cron and public access before consuming the 240s budget.
            assert time.monotonic() - started < 220, 'Insufficient cutover budget for commit'
            state.update(
                {
                    'committed': True,
                    'committed_utc': now(),
                    'backup': str(directory),
                    'baseline': baseline,
                    'snapshot': before_app,
                }
            )
            private_state(state_path, state)
            committed = True
            DEADLINE = started + 300
            new_values = backend_values(new_values, target, cron=1)
            publish_values(new_values, message='Commit Odoo migration to ' + target + ' PostgreSQL')
            k('-n', 'odoo-dev', 'rollout', 'status', 'deployment/odoo', '--timeout=45s', timeout=50)
            if forwarded:
                run(
                    [sys.executable, str(ROOT / 'scripts/forwards.py'), 'start', 'odoo'], timeout=15
                )
            elapsed = time.monotonic() - started
            DEADLINE = None
            # Disable old PostgreSQL declaratively after public writes are re-enabled.
            if target == 'crunchy':
                pg = copy.deepcopy(old_postgres)
                pg['replicaCount'] = 0
                publish_values(new_values, pg, 'Retain old PostgreSQL PVC with zero replicas')
            evidence(
                'reverse-migration' if reverse else 'migration',
                seconds=round(elapsed, 3),
                target_seconds=300,
                within_target=elapsed <= 300,
                backup=str(directory),
                snapshot=before_app,
                committed=True,
            )
            if elapsed > 300:
                raise RuntimeError('Migration completed but exceeded five-minute outage target')
        except BaseException as error:
            DEADLINE = None
            if helper:
                with contextlib.suppress(Exception):
                    helper_stop()
            if committed:
                evidence(
                    'migration',
                    'FAIL',
                    reason=str(error),
                    committed=True,
                    recovery='Use ha-rollback for fresh backup and reverse transfer; direct reuse of old database forbidden',
                )
                raise
            # Source database and filestore remain authoritative until commitment.
            stop_app()
            wait(stopped_app, timeout=30, interval=0.5)
            if changed:
                publish_values(
                    old_values,
                    old_postgres,
                    'Restore source Odoo connection after aborted cutover',
                    odoo_text=original_odoo_text,
                    postgres_text=original_postgres_text,
                )
            start_app()
            k('-n', 'odoo-dev', 'rollout', 'status', 'deployment/odoo', '--timeout=45s', timeout=50)
            if forwarded:
                run(
                    [sys.executable, str(ROOT / 'scripts/forwards.py'), 'start', 'odoo'], timeout=15
                )
            elapsed = time.monotonic() - started
            state.update(
                {
                    'committed': False,
                    'aborted': True,
                    'reason': str(error),
                    'outage_seconds': round(elapsed, 3),
                }
            )
            private_state(state_path, state)
            if (
                os.environ.get('HA_ROLLBACK_REHEARSAL') == 'true'
                and str(error) == 'Requested pre-commit rollback rehearsal'
            ):
                assert app_snapshot() == before_app
                evidence(
                    'rollback-rehearsal',
                    seconds=round(elapsed, 3),
                    within_target=elapsed <= 300,
                    source_restored=True,
                    snapshot=before_app,
                )
                return
            evidence(
                'migration',
                'FAIL',
                reason=str(error),
                source_restored=True,
                seconds=round(elapsed, 3),
                within_target=elapsed <= 300,
            )
            raise
        finally:
            DEADLINE = None
            for s, h in handlers.items():
                signal.signal(s, h)


def main():
    action = sys.argv[1]
    if action == 'install':
        with maintenance():
            install()
            evidence('install')
    elif action == 'status':
        status()
    elif action == 'backup':
        with maintenance():
            full_backup()
    elif action == 'test':
        with maintenance():
            try:
                failover_test()
            finally:
                k('-n', NS, 'delete', 'pod', 'ha-client', '--ignore-not-found', '--wait=false')
    elif action in ('restore-test', 'pitr'):
        with maintenance():
            try:
                restore_test()
            finally:
                k('-n', NS, 'delete', 'pod', 'ha-client', '--ignore-not-found', '--wait=false')
    elif action in ('migrate', 'rollback', 'rehearse'):
        migration(action)
    else:
        raise RuntimeError('Unknown HA action')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        evidence(sys.argv[1], 'FAIL', reason=str(error))
        raise
