#!/usr/bin/env bash
source "$(dirname -- "$0")/common.sh"
assert_cluster
exec python3 "$ROOT/scripts/forwards.py" "${1:-start}"
