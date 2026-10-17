#!/usr/bin/env bash
set -euo pipefail
root=$(cd -- "$(dirname -- "$0")/.." && pwd)
if [[ ! -x $root/.tools/secrets-python/bin/python ]]; then
  python3 -m venv --system-site-packages "$root/.tools/secrets-python"
  "$root/.tools/secrets-python/bin/python" -m pip install --disable-pip-version-check 'psycopg2-binary==2.9.11'
fi
"$root/.tools/secrets-python/bin/python" - <<'PY'
import psycopg2,yaml
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
if not psycopg2.__version__.startswith('2.9.11 '):
    raise SystemExit('The secrets host driver requires psycopg2-binary 2.9.11')
PY
