#!/usr/bin/env bash
source "$(dirname -- "$0")/common.sh"
assert_cluster; need k9s
python3 "$ROOT/scripts/viewer.py"
exec k9s --kubeconfig "$ROOT/.state/viewer.kubeconfig" --context "$CONTEXT" --readonly
