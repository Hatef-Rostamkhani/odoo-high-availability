#!/usr/bin/env bash
source "$(dirname -- "$0")/../../scripts/common.sh"
assert_cluster
if [[ $(python3 "$ROOT/scripts/database.py" backend) == standalone ]]; then
  k -n odoo-dev rollout status statefulset/postgresql --timeout=300s
else
  bash "$ROOT/scripts/ha.sh" status
fi
wait_deploy odoo-dev odoo
python3 "$ROOT/scripts/database.py" verify
mode=$(k -n odoo-dev get configmap odoo-configuration -o jsonpath='{.data.settings\.json}' | python3 -c 'import json,sys;print(json.load(sys.stdin).get("mode","single"))')
if [[ $mode == ha && -z ${TEST_URL:-} && ${ODOO_TEST_PRIVATE:-false} != true ]]; then export TEST_URL=http://127.0.0.1:8069; fi
[[ "$(python3 "$ROOT/scripts/database.py" exec psql -U postgres -d postgres -Atc "SELECT rolname,rolsuper,rolcreatedb,rolcreaterole FROM pg_roles WHERE rolname='odoo';")" == 'odoo|f|f|f' ]] || die 'Odoo database role has excessive privileges'
python3 "$ROOT/tests/application/check.py"
