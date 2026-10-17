#!/usr/bin/env bash
source "$(dirname -- "$0")/common.sh"
assert_cluster; need headlamp
python3 "$ROOT/scripts/viewer.py"
export KUBECONFIG="$ROOT/.state/viewer.kubeconfig"
unset ELECTRON_RUN_AS_NODE
exec headlamp "$@"
