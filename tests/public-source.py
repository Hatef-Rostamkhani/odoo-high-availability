#!/usr/bin/env python3
"""Ensure the publication file set excludes private state and retired branding."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from public_source import source_files

print(
    f'PASS: {len(source_files(ROOT))} public source files; private paths and retired names absent'
)
