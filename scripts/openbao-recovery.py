#!/usr/bin/env python3
"""Authenticated quorum root recovery; token is never printed or persisted."""

import base64
import json
import openbao as b
from secrets_platform import login, leader_forward


def recover(address, auth=None, legacy=False):
    prefix = 'sys/generate-root/' if legacy else 'sys/generate-root-token/'
    response = b.api(address, prefix + 'attempt', {}, auth, method='PUT')
    value = response.get('data', response)
    nonce, otp = value['nonce'], value['otp']
    recovery = json.loads((b.PRIVATE / 'recovery.json').read_text())['recovery_keys_base64']
    for key in recovery[: value['required']]:
        response = b.api(
            address, prefix + 'update', {'nonce': nonce, 'key': key}, auth, method='PUT'
        )
    result = response.get('data', response)
    if not result['complete']:
        raise RuntimeError('Quorum root recovery incomplete')
    encoded = result['encoded_token']
    raw = base64.b64decode(encoded + '=' * (-len(encoded) % 4))
    token = bytes(x ^ y for x, y in zip(raw, otp.encode())).decode()
    if len(raw) != len(otp):
        raise RuntimeError('Invalid recovery OTP length')
    return token


def authenticated_recovery():
    with leader_forward() as address:
        for path in ('sys/generate-root/attempt', 'sys/generate-root-token/attempt'):
            try:
                b.api(address, path, {}, method='PUT')
            except RuntimeError as error:
                if not any('HTTP ' + str(code) in str(error) for code in (403, 405)):
                    raise
            else:
                raise AssertionError('Unauthenticated root generation is reachable')
        auth = login(address, 'odoo-operator', 'bao-operator', 'openbao')
        root = recover(address, auth)
        try:
            b.api(address, 'sys/policies/acl/root', token=root)
        finally:
            b.api(address, 'auth/token/revoke-self', {}, root)
    b.record(
        'authenticated-quorum-recovery',
        recovery_shares_required=3,
        root_revoked=True,
        public_unauthenticated_recovery_disabled=True,
        token_persisted=False,
    )


if __name__ == '__main__':
    authenticated_recovery()
