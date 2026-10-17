#!/usr/bin/env python3
"""Test instance failure and strict replication without stopping any Kind node."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
from ha import failover_test, maintenance

with maintenance():
    failover_test()
