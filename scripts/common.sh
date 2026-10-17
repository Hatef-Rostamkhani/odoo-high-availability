#!/usr/bin/env bash
# shellcheck shell=bash
set -Eeuo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="$ROOT/.tools/bin:$PATH"
export KUBECONFIG="$ROOT/.state/kubeconfig"
# A pre-existing chat process may not inherit newly granted Docker group access.
# Re-enter only a group the invoking account already belongs to; no sudo/password.
if command -v docker >/dev/null && [[ -S /var/run/docker.sock && ! -w /var/run/docker.sock ]] \
  && id -nG "$(id -un)" | grep -qw docker; then
  printf -v rerun '%q ' bash "$0" "$@"
  exec sg docker -c "$rerun"
fi
CONTEXT=kind-odoo-dev
CLUSTER=odoo-dev
mkdir -p "$ROOT/.state" "$ROOT/evidence"
chmod 700 "$ROOT/.state"
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null || die "$1 is missing; run bash scripts/install-tools.sh or the host installer"; }
k() { kubectl --kubeconfig "$KUBECONFIG" --context "$CONTEXT" "$@"; }
h() { helm --kubeconfig "$KUBECONFIG" --kube-context "$CONTEXT" "$@"; }
assert_cluster() {
  need kubectl; need kind; need docker
  [[ -s "$KUBECONFIG" ]] || die 'Dedicated kubeconfig is missing; run make cluster-up'
  kind get clusters | grep -Fxq "$CLUSTER" || die 'Dedicated Kind cluster not found'
  [[ "$(k config current-context)" == "$CONTEXT" ]] || die 'Unexpected project kubeconfig context'
  [[ "$(docker inspect -f '{{ index .Config.Labels "io.x-k8s.kind.cluster" }}' odoo-dev-control-plane)" == "$CLUSTER" ]] || die 'Node ownership mismatch'
  [[ "$(k get nodes -o name | sort)" == $'node/odoo-dev-control-plane\nnode/odoo-dev-worker\nnode/odoo-dev-worker2' ]] || die 'Unexpected cluster identity or node count'
}
version() { python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])' "$ROOT/versions.lock.json" "$1"; }
wait_deploy() { k -n "$1" rollout status deployment/"$2" --timeout=600s; }
secrets_maintenance() {
  local budget=$1
  shift
  if [[ ${ODOO_ROTATION_MAINTENANCE_HELD:-0} != 1 && -x "$ROOT/.tools/secrets-python/bin/python" && -f "$ROOT/scripts/secrets-maintenance.py" ]]; then
    exec "$ROOT/.tools/secrets-python/bin/python" "$ROOT/scripts/secrets-maintenance.py" --budget "$budget" -- bash "$0" "$@"
  fi
}
