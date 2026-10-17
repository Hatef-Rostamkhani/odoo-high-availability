#!/usr/bin/env bash
# NFS is bound to the private Docker bridge, which appears after Docker starts.
set -euo pipefail
configuration=/etc/nfs.conf.d/odoo.conf
[[ -r $configuration ]] || exit 1
address=$(awk -F= '$1 == "host" {print $2}' "$configuration")
[[ $address =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || exit 1
for _attempt in $(seq 1 60); do
  if ip -4 address show | grep -Fq "inet $address/"; then exit 0; fi
  sleep 1
done
echo 'The private Docker bridge address did not become available for NFS.' >&2
exit 1
