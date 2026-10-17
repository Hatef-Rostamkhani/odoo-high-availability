#!/usr/bin/env bash
source "$(dirname -- "$0")/common.sh"
need kind; need docker
if ! kind get clusters | grep -Fxq "$CLUSTER"; then echo 'Dedicated cluster is already absent.'; exit 0; fi
assert_cluster
k get pods,pvc -A -o json > "$ROOT/.state/cluster-down-inventory.json"
k -n odoo-dev get deployment,statefulset,pvc
if k -n odoo-dev get statefulset postgresql >/dev/null 2>&1; then
  if [[ ${ALLOW_DATA_LOSS:-} != "$CLUSTER" ]]; then bash "$ROOT/scripts/backup.sh"; fi
fi
bash "$ROOT/scripts/dashboards.sh" stop || true
kind delete cluster --name "$CLUSTER"
echo 'Only odoo-dev was deleted. Credentials, Git mirror, and host backups were retained.'
