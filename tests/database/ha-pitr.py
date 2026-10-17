#!/usr/bin/env python3
"""Run and verify a disposable, single-instance PITR clone."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
from ha import restore_test, maintenance

with maintenance():
    restore_test()
