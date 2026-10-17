#!/usr/bin/env python3
import json
from pathlib import Path
import socket
import subprocess
import time
import urllib.request

root = Path(__file__).resolve().parents[1]
base = [
    'kubectl',
    '--kubeconfig',
    str(root / '.state/kubeconfig'),
    '--context',
    'kind-odoo-dev',
    '-n',
    'monitoring',
]
with socket.socket() as sock:
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
process = subprocess.Popen(
    base
    + ['port-forward', '--address', '127.0.0.1', 'service/monitoring-prometheus', f'{port}:9090'],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
try:
    for attempt in range(60):
        try:
            assert opener.open(f'http://127.0.0.1:{port}/-/ready', timeout=3).status == 200
            break
        except OSError:
            time.sleep(1)
    else:
        raise RuntimeError('Prometheus did not become reachable')
    # Operator reconciliation and the first scrape lag pod readiness.
    required = ('cilium', 'hubble', 'apiserver', 'kubelet', 'odoo-availability')
    for attempt in range(60):
        data = json.load(opener.open(f'http://127.0.0.1:{port}/api/v1/targets', timeout=10))
        targets = data['data']['activeTargets']
        jobs = [t['labels'].get('job', '') for t in targets]
        if (
            targets
            and all(t['health'] == 'up' for t in targets)
            and all(any(name in job for job in jobs) for name in required)
        ):
            break
        time.sleep(2)
    assert targets, 'No Prometheus targets'
    unhealthy = [t['labels'].get('job') for t in targets if t['health'] != 'up']
    (root / 'evidence/prometheus-targets.json.snapshot').write_text(
        json.dumps(
            [
                {'job': t['labels'].get('job'), 'health': t['health'], 'error': t.get('lastError')}
                for t in targets
            ],
            indent=2,
        )
        + '\n'
    )
    assert not unhealthy, 'Unhealthy enabled Prometheus targets: ' + str(unhealthy)
    assert all(any(name in t['labels'].get('job', '') for t in targets) for name in required), (
        'A required scrape target is missing'
    )
    result = json.load(
        opener.open(f'http://127.0.0.1:{port}/api/v1/query?query=odoo_odoo_http_up', timeout=10)
    )['data']['result']
    assert result and all(float(item['value'][1]) == 1 for item in result), (
        'Odoo availability metric is not healthy'
    )
    print(
        'PASS: metrics API, Prometheus readiness, enabled scrape targets, Cilium/Hubble monitoring, and Odoo availability metric'
    )
finally:
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
