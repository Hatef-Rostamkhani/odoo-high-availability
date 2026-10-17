#!/usr/bin/env python3
from pathlib import Path
import shutil
import subprocess
import tempfile
import sys

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / 'scripts'))
from public_source import source_files

with tempfile.TemporaryDirectory(prefix='odoo-source-') as directory:
    destination = Path(directory)
    for relative in source_files(root):
        path = root / relative
        if path.suffix in ('.tgz', '.gz', '.xz', '.pyc'):
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    subprocess.run(
        [str(root / '.tools/bin/gitleaks'), 'dir', '--redact', '--no-banner', str(destination)],
        check=True,
    )
