#!/usr/bin/env bash
source "$(dirname -- "$0")/common.sh"
need kind; need docker; need helm
docker info >/dev/null || die 'Docker inaccessible. Run sudo bash scripts/install-host.sh in your terminal, then use a new login or sg docker.'
if kind get clusters | grep -Fxq "$CLUSTER"; then
  [[ -s "$KUBECONFIG" ]] || kind get kubeconfig --name "$CLUSTER" > "$KUBECONFIG"
  chmod 600 "$KUBECONFIG"; assert_cluster
  echo 'Dedicated cluster already exists; no recreation performed.'; exit 0
fi
python3 "$ROOT/scripts/check-subnets.py"
python3 - <<'PY'
import pathlib,shutil
mem=dict(l.split(':',1) for l in pathlib.Path('/proc/meminfo').read_text().splitlines())
assert int(mem['MemAvailable'].split()[0]) >= 12*1024*1024, 'At least 12 GiB available host RAM required'
assert shutil.disk_usage('.').free >= 35*1024**3, 'At least 35 GiB free disk required'
PY
mkdir -p "$ROOT/.state/git"
chmod 755 "$ROOT/.state/git"
python3 - "$ROOT" <<'PY'
import pathlib,sys,yaml
r=pathlib.Path(sys.argv[1]);config=yaml.safe_load((r/'kind/cluster.yaml').read_text())
config['nodes'][0]['extraMounts']=[{'hostPath':str(r/'.state/git'),'containerPath':'/var/local/odoo/git','readOnly':True}]
(r/'.state/kind.yaml').write_text(yaml.safe_dump(config,sort_keys=False))
PY
# The host loopback proxy must never be inherited by Kind's node containers.
env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u http_proxy -u https_proxy -u all_proxy \
  KIND_EXPERIMENTAL_PROVIDER=docker kind create cluster --name "$CLUSTER" --config "$ROOT/.state/kind.yaml" --kubeconfig "$KUBECONFIG" --wait 0
chmod 600 "$KUBECONFIG"
assert_cluster
k get --raw=/readyz
echo 'API is healthy. NotReady nodes are expected until make network-install.'
