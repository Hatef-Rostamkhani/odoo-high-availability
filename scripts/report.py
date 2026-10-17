#!/usr/bin/env python3
"""Run a validation gate and retain sanitized evidence, including blocked outcomes."""

import datetime
import json
from pathlib import Path
import subprocess
import sys
import time
import os
import signal
import threading

from odoo_platform.runtime import SecretRedactor

ROOT = Path(__file__).resolve().parents[1]
directory = ROOT / 'evidence'
directory.mkdir(exist_ok=True)


def redact(text):
    paths = [ROOT / '.state/credentials.json', ROOT / '.state/ha-credentials.json']
    paths.extend((ROOT / '.state').glob('odoo-basic*/credentials.json'))
    return SecretRedactor.from_files(paths).redact(text)


name = sys.argv[1]
started = time.monotonic()
if sys.argv[2] in ('BLOCKED', 'NOT RUN'):
    status = sys.argv[2]
    output = ' '.join(sys.argv[3:])
    code = 2
    command = []
else:
    command = sys.argv[2:]
    try:
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        )

        def forward_signal(signum, _frame):
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signum)
                except ProcessLookupError:
                    pass

        signal.signal(signal.SIGTERM, forward_signal)
        signal.signal(signal.SIGINT, forward_signal)
        lines = []

        def collect():
            with (directory / (name + '.log')).open('w') as stream:
                for line in process.stdout:
                    line = redact(line)
                    lines.append(line)
                    stream.write(line)
                    stream.flush()
                    print(line, end='', flush=True)

        reader = threading.Thread(target=collect, daemon=True)
        reader.start()
        try:
            code = process.wait(timeout=2400)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            code = 1
            lines.append('Validation exceeded the 2400-second budget.\n')
        reader.join(timeout=10)
        status = 'PASS' if code == 0 else ('BLOCKED' if code == 2 else 'FAIL')
        output = ''.join(lines)
    except (OSError, subprocess.TimeoutExpired) as error:
        status = 'BLOCKED' if isinstance(error, OSError) else 'FAIL'
        code = 2 if isinstance(error, OSError) else 1
        output = str(error)
output = redact(output)
revision = (
    subprocess.run(['git', 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip()
    or 'uncommitted'
)
record = {
    'test': name,
    'status': status,
    'utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
    'duration_seconds': round(time.monotonic() - started, 2),
    'revision': revision,
    'versions': json.loads((ROOT / 'versions.lock.json').read_text()),
    'command': command,
    'exit_code': code,
    'log': name + '.log',
}
record['worktree_dirty'] = bool(
    subprocess.run(
        ['git', 'status', '--porcelain'], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()
)
(directory / (name + '.json')).write_text(json.dumps(record, indent=2) + '\n')
(directory / (name + '.log')).write_text(output)
records = [
    json.loads(p.read_text())
    for p in sorted(directory.glob('*.json'))
    if p.name != 'assessment.json' and p.name != 'results.json'
]
records = [r for r in records if {'test', 'status', 'duration_seconds', 'revision'} <= r.keys()]
(directory / 'results.json').write_text(json.dumps(records, indent=2) + '\n')
(directory / 'results.md').write_text(
    '# Validation evidence\n\n| Gate | Result | Duration | Revision |\n|---|---|---|---|\n'
    + ''.join(
        f'| {x["test"]} | {x["status"]} | {x["duration_seconds"]}s | {x["revision"][:12]} |\n'
        for x in records
    )
)
if not command:
    print(output, end='' if output.endswith('\n') else '\n')
print(f'{name}: {status} ({record["duration_seconds"]}s)')
raise SystemExit(code)
