#!/usr/bin/env bash
# Run the documented basic entry point in a uniquely owned temporary cluster.
set -Eeuo pipefail
ROOT="$(cd -- "$(dirname -- "$0")/../.." && pwd)"
cd "$ROOT"
export PATH="$ROOT/.tools/bin:$PATH"
export ODOO_BASIC_CLUSTER="odoo-basic-ci-$$"
cleanup() {
  result=$?
  trap - EXIT
  python3 scripts/basic.py down --cluster "$ODOO_BASIC_CLUSTER" || result=1
  exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
make basic-up
make basic-test
