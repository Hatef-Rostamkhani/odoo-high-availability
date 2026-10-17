#!/usr/bin/env bash
# Bootstrap-owned NFS, independent of Kubernetes and Argo CD.
set -Eeuo pipefail
[[ $EUID -eq 0 ]] || { echo 'Run: sudo bash scripts/odoo-ha-host.sh'; exit 2; }
source /etc/os-release
[[ $ID == ubuntu && $VERSION_ID == 24.04 ]] || { echo 'Ubuntu 24.04 is required'; exit 2; }
mapfile -t node_addresses < <(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' odoo-dev-control-plane odoo-dev-worker odoo-dev-worker2)
[[ ${#node_addresses[@]} == 3 ]] || exit 2
server_address=$(docker network inspect kind -f '{{range .IPAM.Config}}{{if .Gateway}}{{.Gateway}}{{println}}{{end}}{{end}}' | awk '/^[0-9]+\./ {print; exit}')
[[ -n $server_address ]] || exit 2
if [[ ! -f /etc/exports.d/odoo.exports ]] && systemctl is-active --quiet nfs-server; then
  echo 'An existing NFS server needs review before adding the project export'; exit 2
fi
# Kind shares host UID accounting: its root processes can exhaust inotify slots.
current_instances=$(sysctl -n fs.inotify.max_user_instances)
if ((current_instances < 1024)); then
  printf 'fs.inotify.max_user_instances = 1024\n' > /etc/sysctl.d/90-odoo-nfs.conf
  sysctl -p /etc/sysctl.d/90-odoo-nfs.conf
fi
apt-get install -y nfs-kernel-server=1:2.6.4-3ubuntu5.1
install -d -o 1000 -g 1000 -m 2770 /srv/odoo/odoo-data
install -d /etc/exports.d /etc/nfs.conf.d
temporary_export=$(mktemp)
trap 'rm -f "$temporary_export"' EXIT
printf '/srv/odoo/odoo-data' > "$temporary_export"
for address in "${node_addresses[@]}"; do
  [[ $address =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || exit 2
  printf ' %s(rw,sync,root_squash,no_subtree_check,fsid=0)' "$address" >> "$temporary_export"
done
printf '\n' >> "$temporary_export"
install -m 0644 "$temporary_export" /etc/exports.d/odoo.exports
cat > /etc/nfs.conf.d/odoo.conf <<EOF
[nfsd]
host=$server_address
vers3=n
vers4=y
EOF
systemctl enable nfs-server
install -d /usr/local/libexec /etc/systemd/system/nfs-server.service.d
install -m 0755 "$(dirname -- "$0")/odoo-ha-nfs-ready.sh" /usr/local/libexec/odoo-nfs-ready
cat > /etc/systemd/system/nfs-server.service.d/odoo-docker.conf <<'EOF'
[Unit]
Requires=docker.service
After=docker.service
[Service]
ExecStartPre=/usr/local/libexec/odoo-nfs-ready
EOF
systemctl daemon-reload
systemctl restart nfs-mountd nfs-idmapd nfs-server
exportfs -ra
for service in nfs-mountd nfs-idmapd nfs-server; do systemctl is-active --quiet "$service"; done
echo 'Project NFSv4 export ready; access is restricted to the three Kind node addresses.'
