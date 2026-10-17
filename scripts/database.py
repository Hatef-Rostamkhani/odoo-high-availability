#!/usr/bin/env python3
"""Resolve the active database from Odoo's live configuration, without secret output."""

import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
K = [
    str(ROOT / '.tools/bin/kubectl'),
    '--kubeconfig',
    str(ROOT / '.state/kubeconfig'),
    '--context',
    'kind-odoo-dev',
    '--request-timeout=10s',
]


def kubectl(*args, input=None, timeout=30):
    result = subprocess.run(K + list(args), input=input, capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError('Kubernetes operation failed: ' + ' '.join(args[:4]))
    return result.stdout


def active():
    cm = json.loads(
        kubectl('-n', 'odoo-dev', 'get', 'configmap', 'odoo-configuration', '-o', 'json')
    )
    cfg = json.loads(cm['data']['settings.json'])
    if cfg['host'] == 'postgresql':
        return 'standalone', 'odoo-dev', 'postgresql-0', 'postgres'
    if cfg['host'] != 'odoo-ha-primary.postgres-ha.svc':
        raise RuntimeError('Unrecognized database endpoint; refusing to target another database')
    return 'crunchy', 'postgres-ha', primary(), 'database'


def primary(cluster='odoo-ha'):
    objects = json.loads(
        kubectl(
            '-n',
            'postgres-ha',
            'get',
            'pods',
            '-l',
            f'postgres-operator.crunchydata.com/cluster={cluster},postgres-operator.crunchydata.com/role=master',
            '-o',
            'json',
        )
    )['items']
    objects = [
        p
        for p in objects
        if not p['metadata'].get('deletionTimestamp')
        and any(c['name'] == 'database' for c in p['spec']['containers'])
    ]
    if len(objects) != 1:
        raise RuntimeError('Expected exactly one PostgreSQL primary')
    return objects[0]['metadata']['name']


def command(args, backend=None):
    _, ns, pod, container = (
        active() if backend is None else ('crunchy', 'postgres-ha', primary(), 'database')
    )
    return K + ['-n', ns, 'exec', '-i', pod, '-c', container, '--'] + args


if __name__ == '__main__':
    if sys.argv[1] == 'backend':
        print(active()[0])
    elif sys.argv[1] == 'verify':
        pods = json.loads(
            kubectl('-n', 'odoo-dev', 'get', 'pods', '-l', 'odoo.io/component=server', '-o', 'json')
        )['items']
        pod = next(
            p['metadata']['name'] for p in pods if not p['metadata'].get('deletionTimestamp')
        )
        code = """import json,os,configparser
from pathlib import Path
import psycopg2
s=json.loads(Path('/opt/odoo/settings.json').read_text())
runtime=configparser.ConfigParser(interpolation=None);runtime.read('/run/odoo/odoo.conf');r=runtime['options']
c=psycopg2.connect(host=r['db_host'],port=r['db_port'],dbname=r['db_name'],user=r['db_user'],password=r['db_password'],sslmode=r['db_sslmode'],connect_timeout=5)
with c.cursor() as q:
 q.execute('SELECT current_database(),current_user,pg_is_in_recovery()');assert q.fetchone()==('odoo_dev','odoo',False)
 q.execute('SELECT session_user');assert q.fetchone()[0]==r['db_user']
 q.execute('SELECT rolsuper,rolcreatedb,rolcreaterole FROM pg_roles WHERE rolname=session_user');assert q.fetchone()==(False,False,False)
 if s['sslmode']=='verify-full':
  assert c.info.ssl_in_use
c.close()
print('PASS: Odoo database login, primary routing, TLS mode and least privileges')
"""
        print(
            kubectl('-n', 'odoo-dev', 'exec', '-i', pod, '--', 'python3', '-', input=code.encode())
            .decode()
            .strip()
        )
    elif sys.argv[1] == 'exec':
        raise SystemExit(subprocess.run(command(sys.argv[2:])).returncode)
    else:
        raise SystemExit('Usage: database.py backend|exec command...')
