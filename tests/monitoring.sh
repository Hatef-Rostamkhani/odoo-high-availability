#!/usr/bin/env bash
source "$(dirname -- "$0")/../scripts/common.sh"
assert_cluster
k top nodes
k top pods -n odoo-dev
k -n monitoring rollout status deployment/monitoring-operator --timeout=300s
k -n monitoring rollout status statefulset/prometheus-monitoring-prometheus --timeout=300s
k -n monitoring rollout status deployment/monitoring-grafana --timeout=300s
python3 "$ROOT/tests/monitoring.py"
