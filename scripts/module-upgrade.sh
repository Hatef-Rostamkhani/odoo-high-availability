#!/usr/bin/env bash
source "$(dirname -- "$0")/common.sh"
assert_cluster
secrets_maintenance 1800 "$@"
[[ ${MODULES:-} =~ ^[a-z][a-z0-9_,]*$ ]] || die 'Set MODULES=module_name[,another_module]'
export MODULE_ACTION=${MODULE_ACTION:-upgrade}
[[ $MODULE_ACTION == upgrade || $MODULE_ACTION == install ]] || die 'MODULE_ACTION must be install or upgrade'
exec 9> "$ROOT/.state/maintenance.lock"; flock -n 9 || die 'Maintenance is already active'
export ODOO_MAINTENANCE_HELD=1
successful=false
runtime_state="$ROOT/.state/module-runtime.json"
python3 "$ROOT/scripts/odoo-runtime.py" snapshot "$runtime_state"
cleanup() {
  if $successful; then
    python3 "$ROOT/scripts/odoo-runtime.py" resume "$runtime_state" >/dev/null || true
    k -n odoo-dev delete job odoo-module-upgrade --ignore-not-found --wait=false >/dev/null || true
  else echo 'Odoo remains stopped after failed maintenance; inspect the job and restore its backup.' >&2; fi
}
trap cleanup EXIT
ODOO_KEEP_STOPPED=true bash "$ROOT/scripts/backup.sh"
python3 "$ROOT/scripts/odoo-runtime.py" stop
MODULES="$MODULES" python3 "$ROOT/scripts/module-job.py" | k apply -f -
k -n odoo-dev wait --for=condition=complete job/odoo-module-upgrade --timeout=900s || { k -n odoo-dev logs job/odoo-module-upgrade --tail=100; die 'Module upgrade failed; restore the backup before further writes'; }
successful=true
cleanup; trap - EXIT
wait_deploy odoo-dev odoo
