#!/usr/bin/env bash
source "$(dirname -- "$0")/../scripts/common.sh"
assert_cluster
k get --raw='/readyz?verbose'
k wait --for=condition=Ready nodes --all --timeout=120s
k -n kube-system rollout status daemonset/kube-proxy --timeout=120s
k -n kube-system rollout status daemonset/cilium --timeout=120s
cilium --context "$CONTEXT" status --wait --wait-duration 2m
wait_deploy kube-system hubble-relay
wait_deploy kube-system hubble-ui
echo 'PASS: API, three nodes, kube-proxy, Cilium, and Hubble'
