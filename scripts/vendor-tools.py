#!/usr/bin/env python3
"""Fetch missing vendor artifacts from their locked upstream source."""

import hashlib
import json
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    locks = json.loads((ROOT / 'vendor/charts.lock.json').read_text())
    for name, entry in locks.items():
        destination = ROOT / entry['path']
        if destination.exists():
            if hashlib.sha256(destination.read_bytes()).hexdigest() != entry['sha256']:
                raise RuntimeError(f'{name}: vendor checksum mismatch')
            continue
        with tempfile.TemporaryDirectory(prefix='vendor-', dir=ROOT / '.state') as directory:
            if entry['url'].startswith('oci://'):
                source = entry['url'] + '@' + entry['oci_digest']
                subprocess.run(
                    [str(ROOT / '.tools/bin/helm'), 'pull', source, '--destination', directory],
                    check=True,
                )
                artifacts = list(Path(directory).glob('*.tgz'))
                if len(artifacts) != 1:
                    raise RuntimeError('Expected exactly one locked OCI chart')
                candidate = artifacts[0]
            elif entry['url'].startswith('https://'):
                candidate = Path(directory) / 'download'
                subprocess.run(
                    [
                        'curl',
                        '--fail',
                        '--location',
                        '--retry',
                        '3',
                        '--max-time',
                        '240',
                        entry['url'],
                        '-o',
                        str(candidate),
                    ],
                    check=True,
                )
            else:
                raise RuntimeError(f'{name}: schema snapshot must be restored from Git')
            if hashlib.sha256(candidate.read_bytes()).hexdigest() != entry['sha256']:
                raise RuntimeError(f'{name}: downloaded vendor checksum mismatch')
            destination.parent.mkdir(parents=True, exist_ok=True)
            candidate.replace(destination)
            print(f'{name}: downloaded and verified from upstream')


if __name__ == '__main__':
    (ROOT / '.state').mkdir(exist_ok=True, mode=0o700)
    main()
