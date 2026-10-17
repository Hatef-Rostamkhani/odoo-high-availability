#!/usr/bin/env bash
source "$(dirname -- "$0")/common.sh"
assert_cluster
bash "$ROOT/scripts/secrets-tools.sh"
exec "$ROOT/.tools/secrets-python/bin/python" "$ROOT/scripts/secrets_admin.py" "${1:-status}"
