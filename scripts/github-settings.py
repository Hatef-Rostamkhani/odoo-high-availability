#!/usr/bin/env python3
"""Review or apply the committed GitHub repository policy using an admin token."""

import argparse
import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', default='Hatef-Rostamkhani/odoo-high-availability')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', args.repository):
        parser.error('Expected owner/repository')
    settings = json.loads((ROOT / '.github/settings.json').read_text())
    rules = json.loads((ROOT / '.github/rulesets/main.json').read_text())
    if not args.apply:
        print(
            json.dumps(
                {
                    'repository': args.repository,
                    'settings': settings,
                    'ruleset': rules,
                    'private_vulnerability_reporting': True,
                },
                indent=2,
            )
        )
        return
    token = os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN')
    if not token:
        raise SystemExit(
            'An authenticated administration token is required in GH_TOKEN; do not paste it into chat.'
        )

    def request(path, method='GET', data=None):
        headers = {
            'Accept': 'application/vnd.github+json',
            'Authorization': 'Bearer ' + token,
            'X-GitHub-Api-Version': '2026-03-10',
        }
        if data is not None:
            headers['Content-Type'] = 'application/json'
        call = urllib.request.Request(
            'https://api.github.com/repos/' + args.repository + path,
            headers=headers,
            method=method,
            data=json.dumps(data).encode() if data is not None else None,
        )
        try:
            with urllib.request.urlopen(call, timeout=30) as response:
                payload = response.read()
                return json.loads(payload) if payload else {}
        except urllib.error.HTTPError as error:
            raise RuntimeError(
                f'GitHub administration request failed: {method} {path} HTTP {error.code}'
            ) from None

    repository = request('')
    if not repository.get('permissions', {}).get('admin'):
        raise SystemExit('Repository administration permission is required')
    request('', 'PATCH', settings)
    request('/private-vulnerability-reporting', 'PUT')
    current = next(
        (entry for entry in request('/rulesets') if entry['name'] == rules['name']), None
    )
    request(
        '/rulesets' + ('/' + str(current['id']) if current else ''),
        'PUT' if current else 'POST',
        rules,
    )
    applied = next(entry for entry in request('/rulesets') if entry['name'] == rules['name'])
    if applied['enforcement'] != 'active':
        raise RuntimeError('Ruleset was not activated')
    print(
        'PASS: GitHub repository settings, private reporting and main ruleset applied and verified'
    )


if __name__ == '__main__':
    main()
