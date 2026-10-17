#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd -- "$(dirname -- "$0")/.." && pwd)"
[[ $EUID -eq 0 ]] || { printf 'Run: sudo bash %q\n' "$0"; exit 1; }
target_user=${SUDO_USER:-${1:-}}
[[ -n $target_user && $target_user != root ]] || { echo 'Run through sudo as your desktop user, or as root: bash scripts/install-host.sh <desktop-user>'; exit 1; }
id "$target_user" >/dev/null
# shellcheck source=/dev/null
source /etc/os-release
[[ $ID == ubuntu && $VERSION_ID == 24.04 && $(dpkg --print-architecture) == amd64 ]] || { echo 'This installer is locked for Ubuntu 24.04 amd64.'; exit 1; }
for package in docker.io docker-compose docker-compose-v2 podman-docker containerd runc; do
  if dpkg-query -W -f='${Status}' "$package" 2>/dev/null | grep -q 'install ok installed'; then
    echo "Conflicting installed package: $package. No packages were removed."; exit 1
  fi
done
if [[ -f /etc/docker/daemon.json || -d /etc/systemd/system/docker.service.d ]]; then
  echo 'Existing Docker configuration detected. Review it manually; this installer will not replace it.'; exit 1
fi
echo 'Installs Docker, adds the invoking user to the root-equivalent docker group, and enables Docker.'
echo 'Docker creates networking/firewall rules. Existing proxy/firewall configuration will not be rewritten.'
apt-get update
apt-get install -y ca-certificates curl gnupg jq git make shellcheck skopeo python3-yaml python3-venv xz-utils unzip
install -m 0755 -d /etc/apt/keyrings
if [[ ! -f /etc/apt/keyrings/odoo-docker.asc ]]; then
  temporary_key=$(mktemp)
  trap 'rm -f "$temporary_key"' EXIT
  curl --fail --location --retry 5 https://download.docker.com/linux/ubuntu/gpg -o "$temporary_key"
  fingerprint=$(gpg --show-keys --with-colons "$temporary_key" | awk -F: '$1=="fpr" {print $10; exit}')
  [[ $fingerprint == 9DC858229FC7DD38854AE2D88D81803C0EBFCD88 ]] || { echo 'Docker signing key mismatch'; exit 1; }
  install -m 0644 "$temporary_key" /etc/apt/keyrings/odoo-docker.asc
fi
source_file=/etc/apt/sources.list.d/odoo-docker.sources
expected_source='Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: noble
Components: stable
Architectures: amd64
Signed-By: /etc/apt/keyrings/odoo-docker.asc'
if [[ -f $source_file ]] && [[ $(cat "$source_file") != "$expected_source" ]]; then
  echo 'Existing Odoo apt source differs; refusing to overwrite.'; exit 1
fi
printf '%s\n' "$expected_source" > "$source_file"
apt-get update
apt-get install -y docker-ce=5:29.9.0-1~ubuntu.24.04~noble docker-ce-cli=5:29.9.0-1~ubuntu.24.04~noble containerd.io=2.4.1-2~ubuntu.24.04~noble
systemctl enable --now docker
usermod -aG docker "$target_user"
if [[ -s "$ROOT/.cache/downloads/headlamp.deb" ]]; then
  expected=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["headlamp-package"]["sha256"])' "$ROOT/downloads.lock.json")
  printf '%s  %s\n' "$expected" "$ROOT/.cache/downloads/headlamp.deb" | sha256sum -c -
  apt-get install -y "$ROOT/.cache/downloads/headlamp.deb"
else
  echo 'Headlamp package not downloaded yet: run install-tools first, then repeat this installer.'
fi
echo 'Host setup complete. Start a fresh login session for Docker group access, or run project commands through sg docker.'
