#!/usr/bin/env bash
source "$(dirname -- "$0")/common.sh"
assert_cluster; need tar; need sha256sum
secrets_maintenance 600 "$@"
directory="$ROOT/backups/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$directory"; chmod 700 "$ROOT/backups" "$directory"
if [[ ${ODOO_MAINTENANCE_HELD:-0} == 1 ]]; then
  [[ $(readlink "/proc/$$/fd/9") == "$ROOT/.state/maintenance.lock" ]] || die 'Inherited maintenance lock is missing'
else
  exec 9> "$ROOT/.state/maintenance.lock"; flock -n 9 || die 'Another backup/module operation is active'
fi
runtime_state="$ROOT/.state/backup-runtime.json"
python3 "$ROOT/scripts/odoo-runtime.py" snapshot "$runtime_state"
data_claim=$(python3 "$ROOT/scripts/odoo-runtime.py" claim)
# IgnoreDifferences on replicas prevents optional self-heal from undoing maintenance.
cleanup() {
  k -n odoo-dev delete pod odoo-backup --ignore-not-found --wait=false >/dev/null || true
  if [[ ${ODOO_KEEP_STOPPED:-false} != true ]]; then python3 "$ROOT/scripts/odoo-runtime.py" resume "$runtime_state" >/dev/null || true; fi
}
trap cleanup EXIT
python3 "$ROOT/scripts/odoo-runtime.py" stop
python3 "$ROOT/scripts/volume-helper.py" odoo-dev odoo-backup "$data_claim" | k apply -f -
k -n odoo-dev wait --for=condition=Ready pod/odoo-backup --timeout=180s
umask 077
python3 "$ROOT/scripts/database.py" exec pg_dump -U postgres -d odoo_dev --format=custom --no-owner --no-acl > "$directory/database.dump.partial"
k -n odoo-dev exec odoo-backup -- tar -C /data -czf - filestore > "$directory/filestore.tar.gz.partial"
mv "$directory/database.dump.partial" "$directory/database.dump"
mv "$directory/filestore.tar.gz.partial" "$directory/filestore.tar.gz"
python3 - "$directory" "$ROOT" <<'PY'
import hashlib,json,pathlib,subprocess,sys
p=pathlib.Path(sys.argv[1]);r=pathlib.Path(sys.argv[2]);files={}
for name in ('database.dump','filestore.tar.gz'):
 with (p/name).open('rb') as stream:files[name]=hashlib.file_digest(stream,'sha256').hexdigest()
import importlib.util
sys.path.insert(0,str(r/'scripts'))
spec=importlib.util.spec_from_file_location('restore_archive',r/'scripts/restore-archive.py');module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
from database import command
def execute(args):return subprocess.check_output(command(args))
instrumentation=module.instrumentation_policy(execute)
metadata={'application_mode':json.loads(subprocess.check_output([str(r/'.tools/bin/kubectl'),'--kubeconfig',str(r/'.state/kubeconfig'),'--context','kind-odoo-dev','-n','odoo-dev','get','configmap','odoo-configuration','-o','json']))['data']['settings.json'], 'operator_instrumentation':instrumentation,'database':'odoo_dev','postgresql_major':17,'backend':subprocess.check_output([sys.executable,str(r/'scripts/database.py'),'backend'],text=True).strip(),'revision':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),'versions':json.loads((r/'versions.lock.json').read_text()),'sha256':files,'consistency':'Odoo HTTP and cron stopped during database/filestore capture'}
metadata['application_mode']=json.loads(metadata['application_mode']).get('mode','single')
metadata['data_claim']=subprocess.check_output([sys.executable,str(r/'scripts/odoo-runtime.py'),'claim'],text=True).strip()
(p/'manifest.json').write_text(json.dumps(metadata,indent=2)+'\n')
PY
cleanup; trap - EXIT
if [[ ${ODOO_KEEP_STOPPED:-false} != true ]]; then wait_deploy odoo-dev odoo; fi
echo "Consistent development backup complete: $directory"
echo 'This is host-local sensitive data. Copy encrypted backups to independent storage for disaster protection.'
