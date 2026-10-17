#!/usr/bin/env bash
source "$(dirname -- "$0")/common.sh"
assert_cluster
echo "Dedicated admin kubeconfig: $KUBECONFIG"
echo "Use: export KUBECONFIG='$KUBECONFIG'"
echo "Context: $CONTEXT"
if [[ ${1:-} == viewer ]]; then
  python3 "$ROOT/scripts/viewer.py"
fi
