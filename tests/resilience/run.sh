#!/usr/bin/env bash
source "$(dirname -- "$0")/../../scripts/common.sh"
assert_cluster
if [[ "$(k -n odoo-dev get configmap odoo-configuration -o jsonpath='{.data.settings\.json}' | python3 -c 'import json,sys;print(json.load(sys.stdin).get("mode","single"))')" == ha ]]; then
  bash "$ROOT/scripts/odoo-ha.sh" test
  python3 "$ROOT/tests/resilience/rollout.py"
  bash "$ROOT/tests/application/run.sh"
  exit 0
fi
bash "$ROOT/tests/application/run.sh"
cp "$ROOT/evidence/application-odoo-dev.json.snapshot" "$ROOT/.state/before-recovery.json"
odoo_pod=$(k -n odoo-dev get pod -l odoo.io/component=server -o jsonpath='{.items[0].metadata.name}')
k -n odoo-dev delete pod "$odoo_pod" --wait=true --timeout=120s
wait_deploy odoo-dev odoo
bash "$ROOT/tests/application/run.sh"
cmp "$ROOT/.state/before-recovery.json" "$ROOT/evidence/application-odoo-dev.json.snapshot"
if [[ $(python3 "$ROOT/scripts/database.py" backend) == crunchy ]]; then
  bash "$ROOT/scripts/ha.sh" test
else
  k -n odoo-dev delete pod postgresql-0 --wait=true --timeout=120s
  k -n odoo-dev rollout status statefulset/postgresql --timeout=300s
fi
bash "$ROOT/tests/application/run.sh"
cmp "$ROOT/.state/before-recovery.json" "$ROOT/evidence/application-odoo-dev.json.snapshot"
python3 "$ROOT/tests/resilience/rollout.py"
bash "$ROOT/tests/application/run.sh"
cmp "$ROOT/.state/before-recovery.json" "$ROOT/evidence/application-odoo-dev.json.snapshot"
echo 'PASS: Odoo/DB pod recovery, stable persistent records and file hashes, bounded single-replica rollout'
