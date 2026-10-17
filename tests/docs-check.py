#!/usr/bin/env python3
"""Check local Markdown links and the documented fresh-install configuration."""

from pathlib import Path
import re
import sys

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from public_source import source_files

broken = []
for relative in source_files(ROOT):
    if relative.suffix != '.md':
        continue
    path = ROOT / relative
    for target in re.findall(r'\[[^\]]*\]\(([^)]+)\)', path.read_text()):
        if target.startswith(('https://', 'http://', 'mailto:', '#', 'app://')):
            continue
        target = target.split('#')[0].strip('<>')
        if target and not (path.parent / target).exists():
            broken.append(f'{relative}: {target}')
if broken:
    raise SystemExit('Broken local documentation links:\n' + '\n'.join(broken))
odoo = yaml.safe_load((ROOT / 'gitops/environments/dev/odoo.yaml').read_text())
postgres = yaml.safe_load((ROOT / 'gitops/environments/dev/postgresql.yaml').read_text())
assert odoo['database']['backend'] == 'standalone'
assert odoo['database']['credentialsMode'] == 'static'
assert odoo['initialization']['enabled'] and odoo['initialization']['syntheticData']
assert odoo['storage']['existingClaim'] == ''
assert postgres['replicaCount'] == 1
print('PASS: local documentation links and fresh-install defaults')
