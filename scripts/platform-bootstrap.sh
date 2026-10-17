#!/usr/bin/env bash
source "$(dirname -- "$0")/common.sh"
assert_cluster
had_monitoring_crds=false
if k get crd prometheuses.monitoring.coreos.com >/dev/null 2>&1; then had_monitoring_crds=true; fi
mkdir -p "$ROOT/.state/platform"
h template monitoring "$ROOT/gitops/platform/monitoring" -n monitoring \
  -f "$ROOT/gitops/environments/dev/monitoring.yaml" --include-crds \
  --set kube-prometheus-stack.crds.enabled=true \
  --set kube-prometheus-stack.global.rbac.create=true \
  --set kube-prometheus-stack.prometheus.serviceAccount.createTokenSecret=true \
  --set kube-prometheus-stack.grafana.rbac.create=true \
  --set kube-prometheus-stack.kube-state-metrics.rbac.create=true > "$ROOT/.state/platform/monitoring-full.yaml"
python3 - "$ROOT" <<'PY'
import pathlib,sys,yaml
yaml.SafeLoader.add_constructor('tag:yaml.org,2002:value', yaml.SafeLoader.construct_scalar)
r=pathlib.Path(sys.argv[1]);docs=list(yaml.safe_load_all((r/'.state/platform/monitoring-full.yaml').read_text()))
cluster=[d for d in docs if d and d['kind'] in ('ClusterRole','ClusterRoleBinding','CustomResourceDefinition')]
(r/'.state/platform/monitoring-cluster.yaml').write_text(yaml.safe_dump_all(cluster,sort_keys=False))
tokens=[d for d in docs if d and d['kind']=='Secret' and d.get('type')=='kubernetes.io/service-account-token']
assert all(not d.get('data') and not d.get('stringData') for d in tokens)
(r/'.state/platform/monitoring-tokens.yaml').write_text(yaml.safe_dump_all(tokens,sort_keys=False))
PY
k apply --server-side --field-manager=odoo-bootstrap -f "$ROOT/.state/platform/monitoring-cluster.yaml"
k wait --for=condition=Established crd/prometheuses.monitoring.coreos.com crd/servicemonitors.monitoring.coreos.com --timeout=120s
k apply -f "$ROOT/gitops/bootstrap/metrics-server-cluster.yaml"
k apply -f "$ROOT/.state/platform/monitoring-tokens.yaml"
if ! $had_monitoring_crds && k -n argocd get statefulset argocd-application-controller >/dev/null 2>&1; then
  # Reload API discovery after introducing new CRD types to an existing controller.
  k -n argocd rollout restart statefulset/argocd-application-controller
  k -n argocd rollout status statefulset/argocd-application-controller --timeout=180s
fi
