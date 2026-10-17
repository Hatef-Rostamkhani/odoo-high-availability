#!/usr/bin/env python3
"""Create secrets out of band. Persist local recovery material with owner-only permissions."""

import json
import os
from pathlib import Path
import secrets
import subprocess

ROOT = Path(__file__).resolve().parents[1]
path = ROOT / '.state/credentials.json'
os.umask(0o077)
if path.exists():
    credentials = json.loads(path.read_text())
else:
    credentials = {
        key: secrets.token_urlsafe(36)
        for key in ('postgres-admin', 'odoo-database', 'odoo-admin', 'odoo-master', 'grafana-admin')
    }
    path.write_text(json.dumps(credentials, indent=2) + '\n')
path.chmod(0o600)
items = [
    ('odoo-dev', 'postgres-admin', {'password': credentials['postgres-admin']}),
    ('odoo-dev', 'odoo-database', {'password': credentials['odoo-database']}),
    (
        'odoo-dev',
        'odoo-credentials',
        {
            'admin-password': credentials['odoo-admin'],
            'master-password': credentials['odoo-master'],
        },
    ),
    (
        'monitoring',
        'grafana-credentials',
        {'admin-user': 'admin', 'admin-password': credentials['grafana-admin']},
    ),
]
for namespace, name, data in items:
    # Send via stdin, never argv. Reapplication preserves generated credentials.
    document = {
        'apiVersion': 'v1',
        'kind': 'Secret',
        'type': 'Opaque',
        'metadata': {'name': name, 'namespace': namespace},
        'stringData': data,
    }
    subprocess.run(
        [
            'kubectl',
            '--kubeconfig',
            str(ROOT / '.state/kubeconfig'),
            '--context',
            'kind-odoo-dev',
            'apply',
            '-f',
            '-',
        ],
        input=json.dumps(document),
        text=True,
        check=True,
    )
print(
    'Local secrets reconciled; values are not printed. Recovery file: .state/credentials.json (0600).'
)
