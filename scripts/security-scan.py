#!/usr/bin/env python3
"""Create vulnerability inventories and SBOMs; optionally enforce release severity gates."""

import argparse
import datetime
import hashlib
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
from contextlib import ExitStack
from odoo_platform.images import verify_cached_image

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--gate', action='store_true', help='Fail on HIGH/CRITICAL findings')
    parser.add_argument('--dependencies-only', action='store_true')
    args = parser.parse_args()
    output = ROOT / 'evidence/security'
    output.mkdir(parents=True, exist_ok=True)
    trivy = ROOT / '.tools/bin/trivy'
    common = [str(trivy), '--cache-dir', str(ROOT / '.cache/trivy')]
    # Trivy's database is intentionally refreshed; the scanner binary is locked.
    subprocess.run(
        common
        + [
            'fs',
            '--quiet',
            '--list-all-pkgs',
            '--scanners',
            'vuln',
            '--format',
            'json',
            '--output',
            str(output / 'dependencies.json'),
            '--skip-dirs',
            '.state,.cache,.tools,backups,evidence,dist',
            str(ROOT / 'requirements.txt'),
        ],
        check=True,
    )
    reports = [output / 'dependencies.json']
    versions = json.loads((ROOT / 'versions.lock.json').read_text())
    dependency_report = json.loads(reports[0].read_text())
    if not any(result.get('Packages') for result in dependency_report.get('Results', [])):
        raise RuntimeError(
            'Dependency inventory found no packages; refusing an empty successful scan'
        )
    if not args.dependencies_only:
        with ExitStack() as stack:
            scan_images(stack, common, output, reports, versions)
    severe = []
    for path in reports:
        document = json.loads(path.read_text())
        for result in document.get('Results', []):
            severe.extend(
                {
                    'report': path.name,
                    'id': item['VulnerabilityID'],
                    'package': item['PkgName'],
                    'severity': item['Severity'],
                }
                for item in result.get('Vulnerabilities', [])
                if item['Severity'] in ('HIGH', 'CRITICAL')
            )
    summary = {
        'utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'scope': 'Python dependencies and basic Odoo/PostgreSQL images',
        'images': {}
        if args.dependencies_only
        else {
            application: versions[application + '_image'] for application in ('odoo', 'postgres')
        },
        'gate': args.gate,
        'high_critical_findings': len(severe),
        'distinct_vulnerability_ids': len({item['id'] for item in severe}),
        'findings': severe,
    }
    (output / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    print(f'Security inventory: {len(severe)} HIGH/CRITICAL findings; reports in {output}')
    if args.gate and severe:
        raise SystemExit('Release security gate failed; review and remediate findings')


def scan_images(stack, common, output, reports, versions):
    for application in ('odoo', 'postgres'):
        reference = versions[application + '_image']
        repository, digest = reference.split('@')
        if ':' in repository.rsplit('/', 1)[-1]:
            repository = repository.rsplit(':', 1)[0]
        canonical = repository + '@' + digest
        report = output / (application + '.json')
        command = common + ['image', '--scanners', 'vuln', '--timeout', '10m']
        archive = ROOT / '.cache/images' / (hashlib.sha256(reference.encode()).hexdigest() + '.tar')
        image_argument = [canonical]
        if archive.exists():
            verify_cached_image(ROOT, application, reference, archive)
            layout = stack.enter_context(
                tempfile.TemporaryDirectory(prefix='trivy-image-', dir=ROOT / '.cache')
            )
            with tarfile.open(archive) as source:
                source.extractall(layout, filter='data')
            image_argument = ['--input', layout]
        subprocess.run(
            command + ['--format', 'json', '--output', str(report)] + image_argument, check=True
        )
        subprocess.run(
            command
            + [
                '--format',
                'cyclonedx',
                '--output',
                str(output / (application + '.cdx.json')),
            ]
            + image_argument,
            check=True,
        )
        reports.append(report)


if __name__ == '__main__':
    main()
