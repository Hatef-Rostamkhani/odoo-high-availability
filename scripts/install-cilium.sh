#!/usr/bin/env bash
source "$(dirname -- "$0")/common.sh"
assert_cluster; need helm; need cilium
chart="$ROOT/vendor/cilium-$(version cilium).tgz"
h template cilium "$chart" -n kube-system -f "$ROOT/infrastructure/networking/cilium-values.yaml" > "$ROOT/.state/cilium.yaml"
bash "$ROOT/scripts/cache-images.sh" "$ROOT/.state/cilium.yaml"
h upgrade --install cilium "$chart" -n kube-system -f "$ROOT/infrastructure/networking/cilium-values.yaml" --wait --timeout 10m
k wait --for=condition=Ready node --all --timeout=600s
cilium --context "$CONTEXT" status --wait --wait-duration 5m
wait_deploy kube-system hubble-relay
wait_deploy kube-system hubble-ui
echo 'Cilium and Hubble are ready. Run make test-network for connectivity/policy evidence.'
