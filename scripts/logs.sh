#!/usr/bin/env bash
source "$(dirname -- "$0")/common.sh"
assert_cluster
k -n odoo-dev logs deployment/odoo --tail=100 "${@}"
