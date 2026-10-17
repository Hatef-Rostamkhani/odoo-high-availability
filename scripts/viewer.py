#!/usr/bin/env python3
import json
import os
from pathlib import Path
import subprocess

root = Path(__file__).resolve().parents[1]
command = ['kubectl', '--kubeconfig', str(root / '.state/kubeconfig'), '--context', 'kind-odoo-dev']
config = json.loads(
    subprocess.check_output(
        command + ['config', 'view', '--raw', '--minify', '-o', 'json'], text=True
    )
)
token = subprocess.check_output(
    command + ['-n', 'gitops-system', 'create', 'token', 'odoo-viewer', '--duration=1h'], text=True
).strip()
config['users'] = [{'name': 'odoo-viewer', 'user': {'token': token}}]
config['contexts'][0]['context']['user'] = 'odoo-viewer'
config['contexts'][0]['context']['namespace'] = 'odoo-dev'
os.umask(0o077)
path = root / '.state/viewer.kubeconfig'
path.write_text(json.dumps(config))
path.chmod(0o600)
print('Read-only kubeconfig refreshed with a one-hour token: .state/viewer.kubeconfig')
