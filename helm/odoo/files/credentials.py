"""Read one atomic Kubernetes credential generation without exposing its values."""

import datetime as dt
import json
from pathlib import Path
import re


def load_bundle(directory, now=None):
    bundle = (Path(directory) / '..data').resolve(strict=True)
    credential = json.loads((bundle / 'credentials.json').read_text())
    required = {'username', 'password', 'generation', 'issued_at', 'expires_at'}
    if set(credential) != required or credential['username'] not in (
        'odoo_login_a',
        'odoo_login_b',
    ):
        raise RuntimeError('Invalid rotating database credential bundle')
    if not all(isinstance(credential[key], str) for key in required) or not credential['password']:
        raise RuntimeError('Invalid credential field type')
    if not re.fullmatch('[a-f0-9]{32}', credential['generation']):
        raise RuntimeError('Invalid credential generation')
    if any((bundle / key).read_text().strip() != credential[key] for key in required):
        raise RuntimeError('Credential bundle contains inconsistent generations')
    try:
        issued = dt.datetime.fromisoformat(credential['issued_at'])
        expiry = dt.datetime.fromisoformat(credential['expires_at'])
        if issued.tzinfo is None or expiry.tzinfo is None:
            raise ValueError('Missing timezone')
    except ValueError:
        raise RuntimeError('Invalid credential timestamp') from None
    now = now or dt.datetime.now(dt.timezone.utc)
    if (
        expiry <= now
        or expiry <= issued
        or expiry > issued + dt.timedelta(hours=24)
        or issued > now + dt.timedelta(seconds=30)
    ):
        raise RuntimeError('Database credential is expired or exceeds its lifetime')
    return credential, expiry
