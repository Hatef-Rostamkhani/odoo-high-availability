#!/usr/bin/env python3
"""Install only artifacts whose URLs and checksums are committed in the lock."""

import concurrent.futures
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[1]


def install(item):
    name, spec = item
    destination = ROOT / spec['destination']
    if destination.exists() and hashlib.sha256(destination.read_bytes()).hexdigest() == spec.get(
        'binary_sha256', spec['sha256']
    ):
        return f'{name}: already verified'
    cache = ROOT / '.cache/downloads' / spec.get('cache_name', name)
    cache.parent.mkdir(parents=True, exist_ok=True)
    if not cache.exists() or hashlib.sha256(cache.read_bytes()).hexdigest() != spec['sha256']:
        partial = cache.with_suffix('.partial')
        subprocess.run(
            [
                'curl',
                '--fail',
                '--location',
                '--retry',
                '5',
                '--connect-timeout',
                '15',
                '--max-time',
                '300',
                spec['url'],
                '-o',
                str(partial),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        if hashlib.sha256(partial.read_bytes()).hexdigest() != spec['sha256']:
            partial.unlink()
            raise RuntimeError(f'{name}: download checksum mismatch')
        partial.replace(cache)
    data = cache.read_bytes()
    if spec.get('member'):
        with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as archive:
            data = archive.extractfile(spec['member']).read()
    if hashlib.sha256(data).hexdigest() != spec.get('binary_sha256', spec['sha256']):
        raise RuntimeError(f'{name}: extracted binary checksum mismatch')
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_suffix('.installing')
    temp.write_bytes(data)
    temp.chmod(0o755 if spec.get('executable', True) else 0o644)
    temp.replace(destination)
    return f'{name}: installed and verified'


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--only', nargs='+', help='Install only these locked tools (default: all)')
    args = parser.parse_args()
    specifications = json.loads((ROOT / 'downloads.lock.json').read_text())
    if args.only:
        unknown = set(args.only) - specifications.keys()
        if unknown:
            parser.error('Unknown tools: ' + ', '.join(sorted(unknown)))
        specifications = {name: specifications[name] for name in dict.fromkeys(args.only)}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        for result in pool.map(install, specifications.items()):
            print(result)
