#!/usr/bin/env bash
source "$(dirname -- "$0")/common.sh"
assert_cluster
case ${1:-} in
  install|status|backup|test|restore-test|pitr|migrate|rollback|rehearse)
    exec python3 "$ROOT/scripts/report.py" "ha-$1" python3 "$ROOT/scripts/ha.py" "$1"
    ;;
  *) die 'Usage: ha.sh install|status|backup|test|restore-test|migrate|rollback|rehearse|pitr';;
esac
