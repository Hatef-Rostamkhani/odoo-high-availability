#!/usr/bin/env python3
"""Bootstrap a portal-only identity for the authenticated session health probe."""

import importlib.util
import json
from pathlib import Path
import secrets
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
spec = importlib.util.spec_from_file_location('odoo_ha', ROOT / 'scripts/odoo-ha.py')
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)
spec = importlib.util.spec_from_file_location('app_client', ROOT / 'tests/application/ha.py')
client_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client_module)
path = ROOT / '.state/odoo-ha-session-probe.json'
if not path.exists():
    path.write_text(
        json.dumps({'username': 'odoo-ha-probe', 'password': secrets.token_urlsafe(32)})
    )
path.chmod(0o600)
credentials = json.loads(path.read_text())
with app.private_forward('odoo-dev') as url:
    client = client_module.Client(url).authenticate()
    group = client.call(
        'ir.model.data',
        'search_read',
        [[['module', '=', 'base'], ['name', '=', 'group_portal']]],
        {'fields': ['res_id']},
    )[0]['res_id']
    ids = client.call('res.users', 'search', [[['login', '=', credentials['username']]]])
    if ids:
        assert (
            client.call('res.users', 'read', [ids], {'fields': ['name']})[0]['name']
            == 'Odoo HA session probe'
        ), 'Probe login belongs to another account'
    else:
        ids = [
            client.call(
                'res.users',
                'create',
                [
                    {
                        'name': 'Odoo HA session probe',
                        'login': credentials['username'],
                        'password': credentials['password'],
                        'group_ids': [(6, 0, [group])],
                    }
                ],
                {'context': {'no_reset_password': True}},
            )
        ]
    user = client.call('res.users', 'read', [ids], {'fields': ['all_group_ids']})[0]
    internal = client.call(
        'ir.model.data',
        'search_read',
        [[['module', '=', 'base'], ['name', '=', 'group_user']]],
        {'fields': ['res_id']},
    )[0]['res_id']
    assert group in user['all_group_ids'] and internal not in user['all_group_ids'], (
        'Probe must be a portal-only identity'
    )
    probe = client_module.Client(url)
    assert (
        probe.rpc(
            '/web/session/authenticate',
            {
                'db': 'odoo_dev',
                'login': credentials['username'],
                'password': credentials['password'],
            },
            30,
        )['uid']
        == ids[0]
    )
app.apply(
    {
        'apiVersion': 'v1',
        'kind': 'Secret',
        'metadata': {
            'name': 'odoo-session-probe',
            'namespace': 'monitoring',
            'labels': {'odoo.io/project': 'odoo-ha'},
        },
        'type': 'Opaque',
        'stringData': credentials,
    }
)
print('Portal-only session probe identity and private monitoring Secret ready')
