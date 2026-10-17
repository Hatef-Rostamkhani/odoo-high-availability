#!/usr/bin/env bash
source "$(dirname -- "$0")/../../scripts/common.sh"
assert_cluster
bash "$ROOT/tests/application/run.sh"
if [[ -n ${BACKUP:-} ]]; then
  [[ -f $BACKUP/manifest.json ]] || die 'Requested ERP backup is missing its manifest'
  backup=$BACKUP
else
  bash "$ROOT/scripts/backup.sh"
  backup=$(python3 - "$ROOT" <<'PYCODE'
import pathlib,sys
print(max((pathlib.Path(sys.argv[1])/'backups').glob('*/manifest.json'),key=lambda p:p.stat().st_mtime_ns).parent)
PYCODE
)
fi
echo "Verifying complete ERP restore from $backup"
python3 "$ROOT/tests/database/prepare.py" "$backup"
# Reject an unrelated existing namespace; never delete user-owned resources.
if k get namespace odoo-restore-test >/dev/null 2>&1; then
  [[ "$(k get ns odoo-restore-test -o jsonpath='{.metadata.labels.odoo\.io/restore-test}')" == true ]] || die 'Restore namespace exists without ownership label'
  k delete namespace odoo-restore-test --wait=true --timeout=180s
fi
k apply -f "$ROOT/.state/restore-bootstrap.yaml"
cleanup() { k delete namespace odoo-restore-test --wait=false >/dev/null || true; }
trap cleanup EXIT
backend=$(python3 - "$backup/manifest.json" <<'PYCODE'
import json,sys
print(json.load(open(sys.argv[1])).get('backend','standalone'))
PYCODE
)
if [[ $backend == crunchy && ${ERP_RESTORE_BACKEND:-crunchy} == crunchy ]]; then
  python3 "$ROOT/tests/database/erp-restore-ha.py" "$backup"
  echo 'PASS: complete ERP backup restored with Crunchy packages and matching filestore'
  exit 0
fi
h template restore-postgres "$ROOT/helm/postgresql" -n odoo-restore-test > "$ROOT/.state/restore-postgres.yaml"
k apply -n odoo-restore-test -f "$ROOT/.state/restore-postgres.yaml"
k -n odoo-restore-test rollout status statefulset/postgresql --timeout=300s
python3 "$ROOT/scripts/restore-archive.py" odoo-restore-test postgresql-0 postgres "$backup/database.dump"
h template restore-odoo "$ROOT/helm/odoo" -n odoo-restore-test -f "$ROOT/gitops/environments/dev/odoo.yaml" \
  --set application.mode=single --set replicaCount=1 --set workers=0 --set cron.enabled=false \
  --set storage.existingClaim= --set-json 'storage.accessModes=["ReadWriteOnce"]' \
  --set initialization.enabled=false --set cronThreads=0 \
  --set database.backend=standalone --set database.credentialsMode=static --set database.host=postgresql --set database.existingSecret=odoo-database \
  --set database.sslmode=prefer --set database.tls.caSecret= \
  --set-json 'database.networkPeer={"podSelector":{"matchLabels":{"app.kubernetes.io/name":"postgresql"}}}' > "$ROOT/.state/restore-odoo.yaml"
# Bind a fresh filestore claim through its first consumer before starting Odoo.
python3 - "$ROOT" <<'PY'
import pathlib,sys,yaml
r=pathlib.Path(sys.argv[1]);docs=list(yaml.safe_load_all((r/'.state/restore-odoo.yaml').read_text()))
pvc=[d for d in docs if d and d['kind']=='PersistentVolumeClaim']
(r/'.state/restore-pvc.yaml').write_text(yaml.safe_dump_all(pvc,sort_keys=False))
PY
k apply -n odoo-restore-test -f "$ROOT/.state/restore-pvc.yaml"
python3 "$ROOT/scripts/volume-helper.py" odoo-restore-test restore-filestore odoo-data | k apply -f -
k -n odoo-restore-test wait --for=condition=Ready pod/restore-filestore --timeout=180s
k -n odoo-restore-test exec -i restore-filestore -- tar -C /data -xzf - < "$backup/filestore.tar.gz"
k -n odoo-restore-test delete pod restore-filestore --wait=true --timeout=120s
k apply -n odoo-restore-test -f "$ROOT/.state/restore-odoo.yaml"
wait_deploy odoo-restore-test odoo
TEST_NAMESPACE=odoo-restore-test python3 "$ROOT/tests/application/check.py"
cmp "$ROOT/evidence/application-odoo-dev.json.snapshot" "$ROOT/evidence/application-odoo-restore-test.json.snapshot"
echo 'PASS: backup restored into isolated PostgreSQL and Odoo with matching CRM IDs and attachment hashes'
