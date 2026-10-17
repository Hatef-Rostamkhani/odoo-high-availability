#!/usr/bin/env bash
source "$(dirname -- "$0")/common.sh"
assert_cluster
k apply -f "$ROOT/gitops/bootstrap/argocd-rbac.yaml"
k apply -f "$ROOT/gitops/bootstrap/projects.yaml"
python3 - "$ROOT" <<'PY' | k -n argocd patch configmap argocd-cm --type=merge --patch-file=/dev/stdin
import pathlib,json,sys,yaml
r=pathlib.Path(sys.argv[1]);v=yaml.safe_load((r/'gitops/bootstrap/argocd-values.yaml').read_text())
print(json.dumps({'data':{'resource.inclusions':v['configs']['cm']['resource.inclusions']}}))
PY
k -n argocd rollout restart statefulset/argocd-application-controller
k -n argocd rollout status statefulset/argocd-application-controller --timeout=180s
k apply -f "$ROOT/gitops/applications/postgres-ha.yaml"
bash "$ROOT/scripts/gitops-publish.sh"
argocd --core --kube-context "$CONTEXT" app sync postgres-ha --timeout 180
argocd --core --kube-context "$CONTEXT" app wait postgres-ha --sync --timeout 180
# Existing monitoring chart gains only namespace selection and one dashboard.
argocd --core --kube-context "$CONTEXT" app sync monitoring --timeout 180
