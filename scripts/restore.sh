#!/usr/bin/env bash
source "$(dirname -- "$0")/common.sh"
assert_cluster
secrets_maintenance 1200 "$@"
[[ -n ${BACKUP:-} && -f $BACKUP/manifest.json ]] || die 'Set BACKUP=/absolute/path/to/a/consistent/backup'
python3 - "$BACKUP" <<'PY'
import hashlib,json,pathlib,sys
p=pathlib.Path(sys.argv[1]);manifest=json.loads((p/'manifest.json').read_text())
assert manifest['postgresql_major']==17 and manifest['database']=='odoo_dev', 'Backup is not compatible with this development database'
for name,expected in manifest['sha256'].items():
 with (p/name).open('rb') as stream:assert hashlib.file_digest(stream,'sha256').hexdigest()==expected,'Backup checksum mismatch'
PY
exec 9> "$ROOT/.state/maintenance.lock"; flock -n 9 || die 'Maintenance is already active'
successful=false
runtime_state="$ROOT/.state/restore-runtime.json"
python3 "$ROOT/scripts/odoo-runtime.py" snapshot "$runtime_state"
data_claim=$(python3 "$ROOT/scripts/odoo-runtime.py" claim)
cleanup() {
  k -n odoo-dev delete pod odoo-restore --ignore-not-found --wait=false >/dev/null || true
  if $successful; then python3 "$ROOT/scripts/odoo-runtime.py" resume "$runtime_state" >/dev/null || true; else echo 'Restore interrupted: Odoo remains stopped. Fix the error and repeat the restore.' >&2; fi
}
trap cleanup EXIT
echo "Restoring $BACKUP into dedicated development database; current data will be replaced."
python3 "$ROOT/scripts/odoo-runtime.py" stop
python3 "$ROOT/scripts/database.py" exec dropdb -U postgres --force --if-exists odoo_dev
python3 "$ROOT/scripts/database.py" exec createdb -U postgres --owner=odoo odoo_dev
python3 "$ROOT/scripts/restore-archive.py" active "$BACKUP/database.dump"
k -n odoo-dev delete pod odoo-restore --ignore-not-found --wait=true --timeout=120s
python3 "$ROOT/scripts/volume-helper.py" odoo-dev odoo-restore "$data_claim" | k apply -f -
k -n odoo-dev wait --for=condition=Ready pod/odoo-restore --timeout=180s
k -n odoo-dev exec odoo-restore -- python3 -c 'import shutil; shutil.rmtree("/data/filestore",ignore_errors=True); shutil.rmtree("/data/sessions",ignore_errors=True)'
k -n odoo-dev exec -i odoo-restore -- tar -C /data -xzf - < "$BACKUP/filestore.tar.gz"
rotating=$(python3 - "$ROOT" <<'PY'
import json,subprocess,sys
from pathlib import Path
r=Path(sys.argv[1]);d=json.loads(subprocess.check_output([str(r/'.tools/bin/kubectl'),'--kubeconfig',str(r/'.state/kubeconfig'),
    '-n','odoo-dev','get','configmap','odoo-configuration','-o','json']))
print('true' if json.loads(d['data']['settings.json']).get('credentialsMode','static')=='rotating' else 'false')
PY
)
if [[ $rotating == true ]]; then bash "$ROOT/scripts/secrets.sh" prepare-restore; fi
successful=true
cleanup; trap - EXIT
wait_deploy odoo-dev odoo
if [[ $rotating == true ]]; then
  if ! bash "$ROOT/scripts/secrets.sh" finalize-restore; then
    python3 "$ROOT/scripts/odoo-runtime.py" stop
    die 'Credential finalization failed; all Odoo writers remain stopped.'
  fi
fi
echo 'Development database and matching filestore restored.'
