#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "$0")/.." && pwd)
bash "$ROOT/scripts/secrets-tools.sh"
"$ROOT/.tools/secrets-python/bin/python" - "$ROOT" <<'PY'
import json,sys
from pathlib import Path
root=Path(sys.argv[1]);report=json.loads((root/'evidence/secrets/acceptance.json').read_text())
for result in report['criteria']:
    if result['test']!='real-24h-observation' and result['status']!='PASS':
        raise SystemExit('Observation prerequisite has not passed: '+result['test'])
PY
unit_directory="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$unit_directory"
unit_path="$unit_directory/odoo-secrets-observation.service"
cat > "$unit_path" <<EOF
[Unit]
Description=Real 24-hour Odoo credential rotation acceptance

[Service]
Type=exec
WorkingDirectory=$ROOT
ExecStart=$ROOT/.tools/secrets-python/bin/python $ROOT/scripts/secrets_admin.py observe
UMask=0077
NoNewPrivileges=true
CPUQuota=50%
MemoryMax=384M
TimeoutStopSec=30
Restart=no

[Install]
WantedBy=default.target
EOF
chmod 600 "$unit_path"
systemctl --user daemon-reload
systemctl --user enable --now odoo-secrets-observation.service
systemctl --user is-active odoo-secrets-observation.service
