#!/usr/bin/env python3
"""Snapshot/stop/resume all Odoo writers; discover the live data claim."""

import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
K = [
    str(ROOT / '.tools/bin/kubectl'),
    '--kubeconfig',
    str(ROOT / '.state/kubeconfig'),
    '--context',
    'kind-odoo-dev',
    '-n',
    'odoo-dev',
]


def k(*args):
    return subprocess.check_output(K + list(args), text=True, timeout=180)


def state():
    deployments = json.loads(k('get', 'deployments', '-o', 'json'))['items']
    result = {}
    for d in deployments:
        if d['metadata']['name'] in ('odoo', 'odoo-cron'):
            result[d['metadata']['name']] = d['spec']['replicas']
    return result


def claim():
    d = json.loads(k('get', 'deployment/odoo', '-o', 'json'))
    return next(
        v['persistentVolumeClaim']['claimName']
        for v in d['spec']['template']['spec']['volumes']
        if v['name'] == 'data'
    )


def snapshot(path):
    path = Path(path)
    path.write_text(json.dumps({'replicas': state(), 'claim': claim()}, indent=2) + '\n')
    path.chmod(0o600)


def stopped():
    pods = json.loads(k('get', 'pods', '-l', 'app.kubernetes.io/name=odoo', '-o', 'json'))['items']
    return not any(
        p['metadata'].get('labels', {}).get('odoo.io/component') in ('server', 'cron') for p in pods
    )


def stop(timeout=180):
    for name in state():
        k('scale', 'deployment/' + name, '--replicas=0')
    deadline = time.monotonic() + timeout
    while not stopped():
        if time.monotonic() > deadline:
            raise RuntimeError('Odoo HTTP/cron writers did not terminate')
        time.sleep(0.5)


def resume(path):
    replicas = json.loads(Path(path).read_text())['replicas']
    for name, count in replicas.items():
        k('scale', 'deployment/' + name, '--replicas=' + str(count))
    for name, count in replicas.items():
        if count:
            k('rollout', 'status', 'deployment/' + name, '--timeout=300s')


if __name__ == '__main__':
    action = sys.argv[1]
    if action == 'snapshot':
        snapshot(sys.argv[2])
    elif action == 'stop':
        stop()
    elif action == 'resume':
        resume(sys.argv[2])
    elif action == 'claim':
        print(claim())
    elif action == 'state':
        print(json.dumps(state()))
    else:
        raise SystemExit('Usage: odoo-runtime.py snapshot FILE|stop|resume FILE|claim|state')
