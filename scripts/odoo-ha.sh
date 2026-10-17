#!/usr/bin/env bash
source "$(dirname -- "$0")/common.sh"
assert_cluster
case ${1:-} in
  install|status|migrate|test|rollback|rehearse)
    exec python3 "$ROOT/scripts/report.py" "odoo-ha-$1" python3 "$ROOT/scripts/odoo-ha.py" "$1";;
  *) die 'Usage: odoo-ha.sh install|status|migrate|test|rollback|rehearse';;
esac
