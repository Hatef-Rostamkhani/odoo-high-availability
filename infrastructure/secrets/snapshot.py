"""Daily encrypted Raft snapshot; no API token or plaintext archive is persisted."""

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import ssl
import time
import urllib.request

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

context = ssl.create_default_context(cafile='/run/ca/ca.crt')


def api(address, path, payload=None, token=None):
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['X-Vault-Token'] = token
    request = urllib.request.Request(
        address + '/v1/' + path,
        headers=headers,
        data=json.dumps(payload).encode() if payload is not None else None,
    )
    with urllib.request.urlopen(request, context=context, timeout=90) as response:
        return response.read()


def main():
    address = None
    for i in range(3):
        candidate = f'https://openbao-{i}.openbao-internal.openbao.svc:8200'
        if json.loads(api(candidate, 'sys/leader'))['is_self']:
            address = candidate
            break
    if not address:
        raise RuntimeError('No Raft leader')
    result = json.loads(
        api(
            address,
            'auth/kubernetes/login',
            {'role': 'odoo-bootstrap', 'jwt': Path('/run/auth/token').read_text().strip()},
        )
    )
    raw = api(address, 'sys/storage/raft/snapshot', token=result['auth']['client_token'])
    nonce = os.urandom(12)
    encrypted = AESGCM(Path('/run/key/key').read_bytes()).encrypt(
        nonce, raw, b'odoo-openbao-snapshot-v1'
    )
    path = Path('/backups') / (
        dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '.snapshot.aesgcm'
    )
    os.umask(0o077)
    temporary = path.with_suffix('.partial')
    with temporary.open('wb') as stream:
        stream.write(b'ODOO-BAO-1\n' + nonce + encrypted)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    for old in Path('/backups').glob('*.snapshot.aesgcm'):
        if time.time() - old.stat().st_mtime > 7 * 86400:
            old.unlink()
    print(
        json.dumps(
            {
                'status': 'PASS',
                'archive': path.name,
                'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    )


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'status': 'FAIL', 'error_type': type(error).__name__}))
        raise SystemExit(1)
