#!/usr/bin/env bash
set -Eeuo pipefail
source "$(dirname -- "$0")/common.sh"
exec python3 "$ROOT/scripts/doctor.py"
