#!/usr/bin/env bash
source "$(dirname -- "$0")/../../scripts/common.sh"
assert_cluster
if k get namespace odoo-network-test >/dev/null 2>&1; then
  [[ "$(k get namespace odoo-network-test -o jsonpath='{.metadata.labels.odoo\.io/network-test}')" == true ]] || die 'Existing network-test namespace is not labeled as owned'
  k delete namespace odoo-network-test --wait=true --timeout=120s
fi
python3 "$ROOT/tests/networking/prepare.py"
bash "$ROOT/scripts/cache-images.sh" "$ROOT/.state/network-test.yaml"
k apply -f "$ROOT/.state/network-test.yaml"
cleanup() { k delete namespace odoo-network-test --wait=false >/dev/null || true; }
trap cleanup EXIT
k -n odoo-network-test wait --for=condition=Ready pod --all --timeout=180s
python3 "$ROOT/tests/networking/matrix.py"
if [[ ${NETWORK_MATRIX_ONLY:-false} == true ]]; then
  echo 'PASS: explicit cross-node connectivity/policy matrix; official suite is a separate pending gate'
  exit 0
fi
# Cilium's official suite includes its own explicit policy and cross-node tests.
bash "$ROOT/scripts/cache-images.sh" "$ROOT/tests/networking/cilium-images.yaml"
cilium --context "$CONTEXT" connectivity test --ip-families ipv4 \
  --test-namespace odoo-connectivity --test-concurrency 1 --timeout 20m \
  --junit-file "$ROOT/evidence/cilium-connectivity.xml"
echo 'PASS: explicit matrix and official Cilium connectivity suite'
