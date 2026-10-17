#!/usr/bin/env python3
"""List the basic and advanced documented commands."""

from pathlib import Path

root = Path(__file__).resolve().parents[1]
seen = set()
print('Odoo development commands (README.md and docs/commands.md):\n')
for filename in ('README.md', 'docs/commands.md'):
    for line in (root / filename).read_text().splitlines():
        if line.startswith('| `make ') and line not in seen:
            print(line.strip('| ').replace('`', ''))
            seen.add(line)
