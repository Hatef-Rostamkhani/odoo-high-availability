#!/usr/bin/env bash
source "$(dirname -- "$0")/common.sh"
assert_cluster
credentials_mode=$(python3 - "$ROOT" <<'PY'
import json,pathlib,subprocess,sys
r=pathlib.Path(sys.argv[1]);d=json.loads(subprocess.check_output([str(r/'.tools/bin/kubectl'),
    '--kubeconfig',str(r/'.state/kubeconfig'),'-n','odoo-dev','get','configmap','odoo-configuration','-o','json']))
print(json.loads(d['data']['settings.json']).get('credentialsMode','static'))
PY
)
[[ $credentials_mode != rotating ]] || die 'Recreate is closed for rotating credentials: discarding Kind also discards Raft/control state. Recover verified encrypted Raft and ERP snapshots with the original external keys in an explicit recovery workflow before rebuilding.'
bash "$ROOT/scripts/backup.sh"
backup=$(python3 - "$ROOT" <<'PY'
import pathlib,sys
print(max((pathlib.Path(sys.argv[1])/'backups').glob('*/manifest.json'),key=lambda p:p.stat().st_mtime_ns).parent)
PY
)
# destroy-cluster normally backs up; this invocation follows a just-completed backup.
ALLOW_DATA_LOSS="$CLUSTER" bash "$ROOT/scripts/destroy-cluster.sh"
for script in create-cluster install-cilium bootstrap-argocd; do bash "$ROOT/scripts/$script.sh"; done
backend=$(python3 - "$backup" <<'PY'
import json,pathlib,sys
print(json.loads((pathlib.Path(sys.argv[1])/"manifest.json").read_text()).get("backend","standalone"))
PY
)
if [[ $backend == crunchy ]]; then
  bash "$ROOT/scripts/ha.sh" install
  bash "$ROOT/scripts/ha-gitops-bootstrap.sh"
fi
application_mode=$(python3 - "$ROOT" <<'PY'
import pathlib,sys,yaml
print((yaml.safe_load((pathlib.Path(sys.argv[1])/'gitops/environments/dev/odoo.yaml').read_text()) or {}).get('application',{}).get('mode','single'))
PY
)
if [[ $application_mode == ha ]]; then
  # Host NFS survives Kind deletion; its owned export must still serve new nodes.
  ODOO_HA_ALLOW_UNINITIALIZED=1 bash "$ROOT/scripts/odoo-ha.sh" install
fi
ODOO_MAINTENANCE=true bash "$ROOT/scripts/deploy.sh" application
BACKUP="$backup" bash "$ROOT/scripts/restore.sh"
k -n argocd patch application odoo --type=json -p '[{"op":"remove","path":"/spec/source/helm/parameters"}]'
argocd --core --kube-context "$CONTEXT" app sync odoo --timeout 120
bash "$ROOT/scripts/deploy.sh" monitoring
if [[ $application_mode == ha ]]; then
  python3 "$ROOT/scripts/odoo-ha.py" entry-stop-private
  python3 "$ROOT/scripts/odoo-ha.py" entry-start
fi
bash "$ROOT/tests/application/run.sh"
echo 'Environment recreated and the consistent application backup restored.'
