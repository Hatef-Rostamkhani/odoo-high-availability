#!/usr/bin/env bash
source "$(dirname -- "$0")/common.sh"
assert_cluster; need argocd
sync_app() {
  k apply -f "$ROOT/gitops/applications/$1.yaml"
  if [[ $1 == odoo && ${ODOO_MAINTENANCE:-false} == true ]]; then
    k -n argocd patch application odoo --type=merge -p '{"spec":{"source":{"helm":{"parameters":[{"name":"replicaCount","value":"0"},{"name":"initialization.enabled","value":"false"},{"name":"cronThreads","value":"0"},{"name":"cron.replicaCount","value":"0"}]}}}}'
  fi
  argocd --core --kube-context "$CONTEXT" app sync "$1" --timeout 900
  argocd --core --kube-context "$CONTEXT" app wait "$1" --health --sync --timeout 900
}
case ${1:-} in
  application)
    for app in postgresql odoo; do
      h template "$app" "$ROOT/helm/$app" -n odoo-dev -f "$ROOT/gitops/environments/dev/$app.yaml" > "$ROOT/.state/$app.yaml"
      bash "$ROOT/scripts/cache-images.sh" "$ROOT/.state/$app.yaml"
    done
    sync_app postgresql
    if [[ $(python3 - "$ROOT/gitops/environments/dev/odoo.yaml" <<'PY'
import sys,yaml
print((yaml.safe_load(open(sys.argv[1])) or {}).get("database",{}).get("backend","standalone"))
PY
) == crunchy ]]; then
      bash "$ROOT/scripts/ha.sh" status
      python3 -c 'import sys;sys.path.insert(0,"scripts");from ha import sync_credentials;sync_credentials()'
    else
      k -n odoo-dev rollout status statefulset/postgresql --timeout=600s
    fi
    sync_app odoo
    wait_deploy odoo-dev odoo
    ;;
  monitoring)
    bash "$ROOT/scripts/platform-bootstrap.sh"
    if [[ $(python3 - "$ROOT/gitops/environments/dev/odoo.yaml" <<'PY'
import sys,yaml
print((yaml.safe_load(open(sys.argv[1])) or {}).get('application',{}).get('mode','single'))
PY
) == ha ]]; then python3 "$ROOT/scripts/odoo-ha-session-probe.py"; fi
    k kustomize "$ROOT/gitops/platform/metrics-server" > "$ROOT/.state/metrics-server.yaml"
    h template monitoring "$ROOT/gitops/platform/monitoring" -n monitoring -f "$ROOT/gitops/environments/dev/monitoring.yaml" > "$ROOT/.state/monitoring.yaml"
    # Prometheus itself is an operator-created pod, not a PodSpec in this rendered manifest.
    python3 "$ROOT/scripts/operator-images.py" "$ROOT/.state/monitoring.yaml" > "$ROOT/.state/monitoring-images.yaml"
    for file in metrics-server monitoring monitoring-images; do bash "$ROOT/scripts/cache-images.sh" "$ROOT/.state/$file.yaml"; done
    sync_app metrics-server
    sync_app monitoring
    wait_deploy kube-system metrics-server
    k apply -f "$ROOT/infrastructure/networking/service-monitors.yaml"
    ;;
  *) die 'Usage: deploy.sh application|monitoring';;
esac
