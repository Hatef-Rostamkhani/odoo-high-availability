#!/usr/bin/env bash
source "$(dirname -- "$0")/common.sh"
result=0
python3 "$ROOT/scripts/report.py" static bash "$ROOT/tests/static.sh" || result=1
if ! command -v docker >/dev/null || ! docker info >/dev/null 2>&1 || [[ ! -s $KUBECONFIG ]]; then
  for gate in foundation networking application gitops monitoring resilience backup-restore desktop; do
    python3 "$ROOT/scripts/report.py" "$gate" BLOCKED 'Docker or dedicated kubeconfig is unavailable; no live test was executed.' || true
  done
  exit 2
fi
for pair in 'foundation tests/foundation.sh' 'networking tests/networking/run.sh' 'application tests/application/run.sh' 'gitops tests/gitops.sh' 'monitoring tests/monitoring.sh' 'resilience tests/resilience/run.sh' 'backup-restore tests/database/restore.sh'; do
  read -r gate script <<< "$pair"
  python3 "$ROOT/scripts/report.py" "$gate" bash "$ROOT/$script" || result=1
done
python3 "$ROOT/scripts/report.py" desktop 'NOT RUN' 'Launch make headlamp and make k9s in the desktop session and verify resource inspection.' || true
exit "$result"
