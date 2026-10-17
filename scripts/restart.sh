#!/usr/bin/env bash
source "$(dirname -- "$0")/common.sh"
assert_cluster
secrets_maintenance 600 "$@"
k -n odoo-dev rollout restart deployment/odoo
wait_deploy odoo-dev odoo
if k -n odoo-dev get deployment odoo-cron >/dev/null 2>&1; then
  k -n odoo-dev rollout restart deployment/odoo-cron
  wait_deploy odoo-dev odoo-cron
fi
echo 'Odoo restarted. For Docker node-container failures, use the documented controlled resilience experiment.'
