#!/usr/bin/env bash
source "$(dirname -- "$0")/common.sh"
assert_cluster
k apply -f "$ROOT/gitops/bootstrap/namespaces.yaml"
k apply -f "$ROOT/infrastructure/database-ha/namespace.yaml"
python3 "$ROOT/scripts/create-secrets.py"
bash "$ROOT/scripts/gitops-publish.sh"
bash "$ROOT/scripts/cache-images.sh" "$ROOT/gitops/bootstrap/local-git.yaml"
k apply -f "$ROOT/gitops/bootstrap/local-git.yaml"
wait_deploy gitops-system local-git
chart="$ROOT/vendor/argo-cd-$(version argocd_chart).tgz"
h template argocd "$chart" -n argocd -f "$ROOT/gitops/bootstrap/argocd-values.yaml" > "$ROOT/.state/argocd.yaml"
bash "$ROOT/scripts/cache-images.sh" "$ROOT/.state/argocd.yaml"
h upgrade --install argocd "$chart" -n argocd -f "$ROOT/gitops/bootstrap/argocd-values.yaml" --wait --timeout 10m
k apply -f "$ROOT/gitops/bootstrap/argocd-rbac.yaml"
k apply -f "$ROOT/gitops/bootstrap/viewer-rbac.yaml"
k apply -f "$ROOT/gitops/bootstrap/projects.yaml"
k config set-context "$CONTEXT" --namespace=argocd >/dev/null
python3 - "$ROOT" <<'PY'
import base64,json,pathlib,subprocess,sys
r=pathlib.Path(sys.argv[1]);p=r/'.state/credentials.json';c=json.loads(p.read_text())
result=subprocess.run(['kubectl','--kubeconfig',str(r/'.state/kubeconfig'),'--context','kind-odoo-dev','-n','argocd','get','secret','argocd-initial-admin-secret','-o','json'],capture_output=True,text=True)
if result.returncode==0:c['argocd-admin']=base64.b64decode(json.loads(result.stdout)['data']['password']).decode();p.write_text(json.dumps(c,indent=2)+'\n');p.chmod(0o600)
PY
wait_deploy argocd argocd-server
echo 'Argo CD ready. Initial credential saved privately; see docs/installation.md for access and rotation.'
