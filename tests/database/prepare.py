#!/usr/bin/env python3
import hashlib
import json
from pathlib import Path
import sys

root = Path(__file__).resolve().parents[2]
backup = Path(sys.argv[1])
manifest = json.loads((backup / 'manifest.json').read_text())
for name, expected in manifest['sha256'].items():
    with (backup / name).open('rb') as stream:
        assert hashlib.file_digest(stream, 'sha256').hexdigest() == expected, (
            'Backup checksum mismatch'
        )
credentials = json.loads((root / '.state/credentials.json').read_text())
documents = [
    {
        'apiVersion': 'v1',
        'kind': 'Namespace',
        'metadata': {
            'name': 'odoo-restore-test',
            'labels': {
                'odoo.io/restore-test': 'true',
                'pod-security.kubernetes.io/enforce': 'restricted',
            },
        },
    }
]
for name, data in [
    ('postgres-admin', {'password': credentials['postgres-admin']}),
    ('odoo-database', {'password': credentials['odoo-database']}),
    (
        'odoo-credentials',
        {
            'admin-password': credentials['odoo-admin'],
            'master-password': credentials['odoo-master'],
        },
    ),
]:
    documents.append(
        {
            'apiVersion': 'v1',
            'kind': 'Secret',
            'metadata': {'name': name, 'namespace': 'odoo-restore-test'},
            'type': 'Opaque',
            'stringData': data,
        }
    )
path = root / '.state/restore-bootstrap.yaml'
path.write_text('\n---\n'.join(json.dumps(d) for d in documents))
path.chmod(0o600)
