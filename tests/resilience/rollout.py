#!/usr/bin/env python3
import json
from pathlib import Path
import subprocess
import time
import threading
import urllib.request

root = Path(__file__).resolve().parents[2]
base = [
    'kubectl',
    '--kubeconfig',
    str(root / '.state/kubeconfig'),
    '--context',
    'kind-odoo-dev',
    '-n',
    'odoo-dev',
]
deployment = json.loads(
    subprocess.check_output(base + ['get', 'deployment', 'odoo', '-o', 'json'], text=True)
)
replicas = deployment['spec']['replicas']
is_ha = deployment['spec']['strategy']['type'] == 'RollingUpdate'
assert replicas == (2 if is_ha else 1)
original = deployment['status']['observedGeneration']
http_samples = []
stop = threading.Event()


def probe():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    while not stop.is_set():
        try:
            with opener.open(
                'http://127.0.0.1:8069/web/health?db_server_status=1', timeout=2
            ) as response:
                assert response.status == 200 and json.load(response)['status'] == 'pass'
            http_samples.append(True)
        except Exception:
            http_samples.append(False)
        stop.wait(0.2)


thread = threading.Thread(target=probe, daemon=True)
if is_ha:
    thread.start()
subprocess.run(base + ['rollout', 'restart', 'deployment/odoo'], check=True)
started = time.monotonic()
zero_ready_seconds = 0
max_serving = 0
while time.monotonic() - started < 600:
    pods = json.loads(
        subprocess.check_output(
            base + ['get', 'pods', '-l', 'odoo.io/component=server', '-o', 'json'], text=True
        )
    )['items']
    serving = [
        p
        for p in pods
        if not p['metadata'].get('deletionTimestamp')
        and any(
            c['type'] == 'Ready' and c['status'] == 'True'
            for c in p.get('status', {}).get('conditions', [])
        )
    ]
    max_serving = max(max_serving, len(serving))
    if not serving:
        zero_ready_seconds += 1
    d = json.loads(
        subprocess.check_output(base + ['get', 'deployment', 'odoo', '-o', 'json'], text=True)
    )
    if (
        d['status'].get('observedGeneration', 0) > original
        and d['status'].get('replicas') == replicas
        and d['status'].get('updatedReplicas') == replicas
        and d['status'].get('availableReplicas') == replicas
    ):
        break
    time.sleep(1)
else:
    raise RuntimeError('Rollout exceeded 600 seconds')
if is_ha:
    time.sleep(10)
    stop.set()
    thread.join(timeout=5)
assert max_serving <= (3 if is_ha else 1)
if is_ha:
    assert zero_ready_seconds == 0, 'HA rollout had no Ready web instance'
evidence = {
    'rollout_seconds': round(time.monotonic() - started, 2),
    'samples_with_no_ready_pod': zero_ready_seconds,
    'max_nonterminating_ready_pods': max_serving,
    'public_http_samples': len(http_samples),
    'public_http_failures': http_samples.count(False),
    'zero_downtime': is_ha and zero_ready_seconds == 0 and all(http_samples),
}
(root / 'evidence/rollout.json.snapshot').write_text(json.dumps(evidence, indent=2) + '\n')
print(json.dumps(evidence))
if is_ha:
    assert http_samples and all(http_samples), 'Public HTTP failed during the compatible rollout'
