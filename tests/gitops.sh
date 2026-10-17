#!/usr/bin/env bash
source "$(dirname -- "$0")/../scripts/common.sh"
assert_cluster; need argocd
expected=$(git -C "$ROOT" rev-parse HEAD)
applications=(postgresql odoo monitoring metrics-server)
if k -n argocd get application postgres-ha >/dev/null 2>&1; then applications+=(postgres-ha); fi
if k -n argocd get application odoo-edge >/dev/null 2>&1; then applications+=(odoo-edge); fi
for application in "${applications[@]}"; do
  k -n argocd annotate application "$application" argocd.argoproj.io/refresh=hard --overwrite
  # A wait on Healthy/Synced alone can return before the requested refresh runs.
  for ((attempt=0;attempt<60;attempt++)); do
    [[ "$(k -n argocd get application "$application" -o jsonpath='{.status.sync.revision}')" == "$expected" ]] && break
    sleep 2
  done
  argocd --core --kube-context "$CONTEXT" app wait "$application" --health --sync --timeout 300
  [[ "$(k -n argocd get application "$application" -o jsonpath='{.status.sync.revision}')" == "$expected" ]] || die "$application has not reconciled the committed revision"
done
# Harmless managed metadata drift should be identified and corrected by synchronization.
k -n odoo-dev annotate service odoo odoo.io/configuration=drift --overwrite
k -n argocd annotate application odoo argocd.argoproj.io/refresh=hard --overwrite
for ((attempt=0;attempt<60;attempt++)); do
  [[ "$(k -n argocd get application odoo -o jsonpath='{.status.sync.status}')" == OutOfSync ]] && break
  sleep 2
done
[[ "$(k -n argocd get application odoo -o jsonpath='{.status.sync.status}')" == OutOfSync ]] || die 'Argo did not detect managed metadata drift'
argocd --core --kube-context "$CONTEXT" app sync odoo --timeout 300
argocd --core --kube-context "$CONTEXT" app wait odoo --health --sync --timeout 300
[[ "$(k -n odoo-dev get service odoo -o jsonpath='{.metadata.annotations.odoo\.io/configuration}')" == declared ]] || die 'Argo did not restore declared metadata'
echo 'PASS: all applications Healthy/Synced at the published commit; explicit synchronization completed'
