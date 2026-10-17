#!/usr/bin/env python3
"""Build Helm charts and a source archive without local state or Git history."""

import argparse
import hashlib
from pathlib import Path
import re
import subprocess
import tempfile

from public_source import export_source

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-only', action='store_true')
    parser.add_argument('--version', help='Chart release version, e.g. 1.2.3 or v1.2.3')
    parser.add_argument('--output', type=Path, default=ROOT / 'dist')
    args = parser.parse_args()
    version = args.version.removeprefix('v') if args.version else None
    if version and not re.fullmatch(r'\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?', version):
        parser.error('Version must be a semantic version such as v1.2.3')
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    source = output / 'odoo-ha-source.tar.gz'
    count = export_source(ROOT, source)
    artifacts = [source]
    if not args.source_only:
        helm = ROOT / '.tools/bin/helm'
        with tempfile.TemporaryDirectory(prefix='charts-', dir=output) as directory:
            for name in ('odoo', 'odoo-edge', 'postgresql'):
                command = [
                    str(helm),
                    'package',
                    str(ROOT / 'helm' / name),
                    '--destination',
                    directory,
                ]
                if version:
                    command += ['--version', version]
                subprocess.run(command, check=True)
            for package in sorted(Path(directory).glob('*.tgz')):
                target = output / package.name
                package.replace(target)
                artifacts.append(target)
    checksums = ''.join(
        f'{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n' for path in artifacts
    )
    (output / 'SHA256SUMS').write_text(checksums)
    print(f'PASS: exported {count} source files; packages and checksums are in {output}')


if __name__ == '__main__':
    main()
