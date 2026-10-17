#!/usr/bin/env bash
source "$(dirname -- "$0")/../scripts/common.sh"
need helm; need shellcheck; need kubeconform; need gitleaks; need actionlint
python3 "$ROOT/scripts/vendor-tools.py"
make -C "$ROOT" lint-python
python3 "$ROOT/scripts/render-runtime.py" --check
python3 "$ROOT/tests/public-source.py"
python3 "$ROOT/tests/docs-check.py"
actionlint "$ROOT"/.github/workflows/*.yml
mkdir -p "$ROOT/.state/rendered" "$ROOT/.cache/schemas"
mapfile -t shell_files < <(rg --files "$ROOT/scripts" "$ROOT/tests" -g '*.sh')
for script in "${shell_files[@]}"; do bash -n "$script"; done
shellcheck -x -P "$ROOT/scripts" -e SC1091 "${shell_files[@]}"
python3 -m compileall -q "$ROOT/scripts" "$ROOT/tests" "$ROOT/helm/odoo/files"
helm lint "$ROOT/helm/odoo" "$ROOT/helm/odoo-edge" "$ROOT/helm/postgresql" "$ROOT/gitops/platform/monitoring" "$ROOT/gitops/platform/openbao" "$ROOT/gitops/platform/external-secrets" --strict
for app in odoo postgresql; do
  helm template "$app" "$ROOT/helm/$app" -n odoo-dev -f "$ROOT/gitops/environments/dev/$app.yaml" > "$ROOT/.state/rendered/$app.yaml"
done
helm template odoo-ha "$ROOT/helm/odoo" -n odoo-dev -f "$ROOT/gitops/environments/dev/odoo-ha.yaml" > "$ROOT/.state/rendered/odoo-ha.yaml"
helm template odoo-edge "$ROOT/helm/odoo-edge" -n odoo-edge > "$ROOT/.state/rendered/odoo-edge.yaml"
helm template cilium "$ROOT/vendor/cilium-$(version cilium).tgz" -n kube-system -f "$ROOT/infrastructure/networking/cilium-values.yaml" > "$ROOT/.state/rendered/cilium.yaml"
helm template argocd "$ROOT/vendor/argo-cd-$(version argocd_chart).tgz" -n argocd -f "$ROOT/gitops/bootstrap/argocd-values.yaml" > "$ROOT/.state/rendered/argocd.yaml"
helm template monitoring "$ROOT/gitops/platform/monitoring" -n monitoring -f "$ROOT/gitops/environments/dev/monitoring.yaml" > "$ROOT/.state/rendered/monitoring.yaml"
helm template openbao "$ROOT/gitops/platform/openbao" -n secrets-system > "$ROOT/.state/rendered/openbao.yaml"
helm template external-secrets "$ROOT/gitops/platform/external-secrets" -n secrets-system --set external-secrets.installCRDs=true --include-crds > "$ROOT/.state/rendered/external-secrets.yaml"
kubectl kustomize "$ROOT/gitops/platform/secrets-system" > "$ROOT/.state/rendered/secrets-system.yaml"
kubectl kustomize "$ROOT/gitops/platform/metrics-server" > "$ROOT/.state/rendered/metrics-server.yaml"
kubectl kustomize "$ROOT/infrastructure/database-ha" > "$ROOT/.state/rendered/postgres-ha.yaml"
cp "$ROOT/infrastructure/odoo-ha/storage.yaml" "$ROOT/.state/rendered/odoo-shared-storage.yaml"
python3 "$ROOT/tests/check-rendered.py"
python3 -m unittest discover -s "$ROOT/tests/unit" -v
# Validate built-ins strictly; custom resources are separately checked against vendored CRD schemas below.
kubeconform -strict -summary -kubernetes-version 1.35.0 -cache "$ROOT/.cache/schemas" \
  -schema-location "$ROOT/vendor/schemas/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json" -schema-location default \
  -skip 'Application,AppProject,Prometheus,ServiceMonitor,PodMonitor,PrometheusRule,Alertmanager,CiliumNodeConfig,CiliumNetworkPolicy,PostgresCluster,ExternalSecret,ClusterSecretStore,SecretStore' \
  -ignore-filename-pattern 'argocd-values.yaml$' \
  "$ROOT/.state/rendered" "$ROOT/gitops/bootstrap" "$ROOT/gitops/applications"
python3 "$ROOT/tests/custom-schemas.py"
python3 "$ROOT/tests/secret-scan.py"
echo 'PASS: publication safety, actionlint, shell syntax/lint, Python compilation, Helm renders, safety assertions, Kubernetes schemas, CRD schemas, secret scan'
