#!/usr/bin/env bash
source "$(dirname -- "$0")/common.sh"
assert_cluster
k get nodes -o wide
k get pods -A -o wide
k -n odoo-dev get deployment,statefulset,service,pvc
k -n argocd get applications
k top nodes || echo 'Metrics are not ready yet.'
free -h
