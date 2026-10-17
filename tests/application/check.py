#!/usr/bin/env python3
"""Authenticate and exercise Odoo without exposing credentials or user content."""

import hashlib
import http.cookiejar
import json
import os
from pathlib import Path
import socket
import subprocess
import time
import urllib.request

root = Path(__file__).resolve().parents[2]
credentials = json.loads(
    Path(os.environ.get('TEST_CREDENTIALS', str(root / '.state/credentials.json'))).read_text()
)
base = [
    'kubectl',
    '--kubeconfig',
    os.environ.get('TEST_KUBECONFIG', str(root / '.state/kubeconfig')),
    '--context',
    os.environ.get('TEST_CONTEXT', 'kind-odoo-dev'),
    '-n',
    os.environ.get('TEST_NAMESPACE', 'odoo-dev'),
]
with socket.socket() as sock:
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
process = (
    None
    if os.environ.get('TEST_URL')
    else subprocess.Popen(
        base + ['port-forward', '--address', '127.0.0.1', 'service/odoo', f'{port}:8069'],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
)
opener = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()), urllib.request.ProxyHandler({})
)
url = os.environ.get('TEST_URL', f'http://127.0.0.1:{port}')


def rpc(path, params):
    request = urllib.request.Request(
        url + path,
        json.dumps({'jsonrpc': '2.0', 'method': 'call', 'params': params, 'id': 1}).encode(),
        {'Content-Type': 'application/json'},
    )
    result = json.load(opener.open(request, timeout=30))
    if 'error' in result:
        raise RuntimeError('Odoo JSON RPC returned an error; inspect sanitized pod logs')
    return result['result']


def call(model, method, args, kwargs=None):
    return rpc(
        '/web/dataset/call_kw',
        {'model': model, 'method': method, 'args': args, 'kwargs': kwargs or {}},
    )


try:
    for attempt in range(60):
        try:
            assert (
                json.load(opener.open(url + '/web/health?db_server_status=1', timeout=3))['status']
                == 'pass'
            )
            break
        except (OSError, urllib.error.URLError):
            time.sleep(1)
    else:
        raise RuntimeError('Odoo HTTP endpoint did not become healthy')
    result = rpc(
        '/web/session/authenticate',
        {'db': 'odoo_dev', 'login': 'admin', 'password': credentials['odoo-admin']},
    )
    assert result.get('uid'), 'Authentication did not return a user'
    company = call('res.company', 'search_read', [[['id', '=', 1]]], {'fields': ['name']})
    assert company and company[0]['name'] == 'Odoo Development', (
        'Development company name is not generic'
    )
    partners = call(
        'res.partner',
        'search_read',
        [[['name', '=', 'Synthetic Odoo Customer']]],
        {'fields': ['id', 'name']},
    )
    leads = call(
        'crm.lead',
        'search_read',
        [[['name', '=', 'Synthetic pipeline opportunity']]],
        {'fields': ['id', 'name']},
    )
    assert len(partners) == len(leads) == 1, 'Synthetic CRM fixtures missing or duplicated'
    attachments = call(
        'ir.attachment',
        'search_read',
        [[['name', '=', 'odoo-fixture.txt']]],
        {'fields': ['id', 'checksum', 'store_fname']},
    )
    assert len(attachments) == 1 and attachments[0]['store_fname'], (
        'Fixture is not stored in the filestore'
    )
    payload = opener.open(
        url + '/web/content/' + str(attachments[0]['id']) + '?download=true', timeout=10
    ).read()
    assert payload == b'Odoo synthetic attachment v1\n'
    # Create another attachment and immediately read it through HTTP.
    import base64

    identifier = call(
        'ir.attachment',
        'create',
        [
            {
                'name': 'odoo-http-test.txt',
                'res_model': 'res.partner',
                'res_id': partners[0]['id'],
                'datas': base64.b64encode(b'HTTP attachment roundtrip').decode(),
                'mimetype': 'text/plain',
            }
        ],
    )
    assert (
        opener.open(url + '/web/content/' + str(identifier) + '?download=true', timeout=10).read()
        == b'HTTP attachment roundtrip'
    )
    call('ir.attachment', 'unlink', [[identifier]])
    snapshot = {
        'partner_id': partners[0]['id'],
        'lead_id': leads[0]['id'],
        'attachment_id': attachments[0]['id'],
        'attachment_sha256': hashlib.sha256(payload).hexdigest(),
    }
    target = (
        root
        / 'evidence'
        / ('application-' + os.environ.get('TEST_NAMESPACE', 'odoo-dev') + '.json.snapshot')
    )
    target = Path(os.environ.get('TEST_SNAPSHOT_PATH', str(target)))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(snapshot, indent=2) + '\n')
    print(
        'PASS: HTTP health, database-aware readiness, administrator authentication, CRM fixtures, filestore download, and attachment roundtrip'
    )
finally:
    if process:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
