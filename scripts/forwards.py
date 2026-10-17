#!/usr/bin/env python3
"""Manage only this project's loopback forwards; validate PID ownership before stopping."""

import json
import fcntl
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

root = Path(__file__).resolve().parents[1]
state = root / '.state/forwards.json'
lock = open(root / '.state/forwards.lock', 'w')
fcntl.flock(lock, fcntl.LOCK_EX)
commands = [
    ('odoo', 'odoo-dev', 'odoo', 8069, 8069, 'http'),
    ('argocd', 'argocd', 'argocd-server', 8443, 443, 'https'),
    ('hubble', 'kube-system', 'hubble-ui', 12000, 80, 'http'),
    ('grafana', 'monitoring', 'monitoring-grafana', 3000, 80, 'http'),
    ('prometheus', 'monitoring', 'monitoring-prometheus', 9090, 9090, 'http'),
]


def owned(record):
    try:
        cmd = Path('/proc/' + str(record['pid']) + '/cmdline').read_bytes().split(b'\x00')
    except OSError:
        return False
    return (
        b'port-forward' in cmd
        and str(root / '.state/kubeconfig').encode() in cmd
        and f'{record["port"]}:{record["target"]}'.encode() in cmd
    )


def supervised(record):
    try:
        cmd = Path('/proc/' + str(record['pid']) + '/cmdline').read_bytes().split(b'\x00')
    except OSError:
        return False
    return str(root / 'scripts/forward-service.py').encode() in cmd


records = json.loads(state.read_text()) if state.exists() else {}
selected = sys.argv[2] if len(sys.argv) > 2 else None
if selected:
    assert selected in {c[0] for c in commands}, 'Unknown project forward'
    commands = [c for c in commands if c[0] == selected]
if sys.argv[1] == 'stop':
    if not selected or selected == 'odoo':
        subprocess.run([sys.executable, str(root / 'scripts/odoo-ha.py'), 'entry-stop'], check=True)
    for name, record in list(records.items()):
        if selected and name != selected:
            continue
        if owned(record):
            os.kill(record['pid'], signal.SIGTERM)
            for attempt in range(50):
                if not owned(record):
                    break
                time.sleep(0.1)
            else:
                raise RuntimeError('Owned forward did not stop')
        records.pop(name, None)
    state.write_text(json.dumps(records, indent=2) + '\n')
    state.chmod(0o600)
    print('Project port forwards stopped')
    raise SystemExit(0)
failures = []
for name, namespace, service, port, target, protocol in commands:
    if name == 'odoo':
        import yaml

        values = yaml.safe_load((root / 'gitops/environments/dev/odoo.yaml').read_text())
        if values.get('application', {}).get('mode') == 'ha':
            subprocess.run(
                [sys.executable, str(root / 'scripts/odoo-ha.py'), 'entry-start'], check=True
            )
            print('odoo: http://127.0.0.1:8069 (HA proxy)')
            continue
    if name in records and owned(records[name]):
        if supervised(records[name]):
            print(f'{name}: {protocol}://127.0.0.1:{port} (already running)')
            continue
        # Upgrade only a recorded legacy forward; unrelated listeners remain untouched.
        os.kill(records[name]['pid'], signal.SIGTERM)
        for attempt in range(50):
            if not owned(records[name]):
                break
            time.sleep(0.1)
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(('127.0.0.1', port))
        except OSError:
            failures.append(
                f'{name}: localhost port {port} is occupied; stop the conflicting process'
            )
            continue
    cmd = [
        'kubectl',
        '--kubeconfig',
        str(root / '.state/kubeconfig'),
        '--context',
        'kind-odoo-dev',
        '-n',
        namespace,
        'port-forward',
        '--address',
        '127.0.0.1',
        'service/' + service,
        f'{port}:{target}',
    ]
    log = open(root / '.state' / ('forward-' + name + '.log'), 'a')
    process = subprocess.Popen(
        [sys.executable, str(root / 'scripts/forward-service.py')] + cmd,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    record = {'pid': process.pid, 'port': port, 'target': target}
    for attempt in range(50):
        if process.poll() is not None:
            break
        with socket.socket() as sock:
            if sock.connect_ex(('127.0.0.1', port)) == 0:
                records[name] = record
                print(f'{name}: {protocol}://127.0.0.1:{port}')
                break
        time.sleep(0.2)
    else:
        process.terminate()
    if name not in records or records[name] != record:
        failures.append(f'{name}: forwarding failed; inspect .state/forward-{name}.log')
    log.close()
state.write_text(json.dumps(records, indent=2) + '\n')
state.chmod(0o600)
for failure in failures:
    print(failure, file=sys.stderr)
raise SystemExit(bool(failures))
