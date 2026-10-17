#!/usr/bin/env python3
"""Keep a replicated maintenance lease across PostgreSQL primary changes."""

import argparse
import contextlib
import json
import os
import secrets
import select
import signal
import subprocess
import sys
import time

import openbao as b
from secrets_admin import local_worker, w


def rotating():
    if not b.exists('odoo-dev', 'configmap', 'odoo-configuration'):
        return False
    return (
        json.loads(
            b.obj('odoo-dev', 'configmap', 'odoo-configuration')['data']['settings.json']
        ).get('credentialsMode', 'static')
        == 'rotating'
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--budget', type=int, default=1200)
    parser.add_argument('--hold', action='store_true')
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command and args.command[0] == '--' else args.command
    if not rotating():
        if args.hold:
            print('READY static', flush=True)
            sys.stdin.readline()
            return
        env = os.environ.copy()
        env['ODOO_ROTATION_MAINTENANCE_HELD'] = '1'
        os.execvpe(command[0], command, env)
    owner = secrets.token_hex(16)
    stack = contextlib.ExitStack()
    worker = stack.enter_context(local_worker(bao_address='https://not-used.invalid'))
    if not worker.sql('SELECT pg_try_advisory_lock(%s)', (w.LOCK,)):
        raise RuntimeError('Another rotation or maintenance is active')
    worker.sql('SELECT secret_control.maintenance_begin(%s,%s)', (owner, args.budget))
    deadline = time.monotonic() + args.budget
    child = None
    if args.hold:
        print('READY ' + owner, flush=True)
    else:
        env = os.environ.copy()
        env['ODOO_ROTATION_MAINTENANCE_TOKEN'] = owner
        env['ODOO_ROTATION_MAINTENANCE_HELD'] = '1'
        child = subprocess.Popen(command, env=env, start_new_session=True)
    stopped = False

    def interrupted(*_):
        nonlocal stopped
        stopped = True
        if child and child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM)

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, interrupted)
    try:
        while not stopped and time.monotonic() < deadline:
            if args.hold:
                if select.select([sys.stdin], [], [], 1)[0]:
                    sys.stdin.readline()
                    break
            elif child.poll() is not None:
                break
            try:
                worker.sql('SELECT 1')
            except Exception:
                stack.close()
                stack = contextlib.ExitStack()
                try:
                    worker = stack.enter_context(
                        local_worker(bao_address='https://not-used.invalid')
                    )
                    if not worker.sql('SELECT pg_try_advisory_lock(%s)', (w.LOCK,)):
                        raise RuntimeError('Maintenance advisory lock could not be reacquired')
                    # The acknowledged lease survives failover; never extend it.
                    state = worker.status()
                    if not state.get('maintenance') or state['maintenance']['owner'] != owner:
                        raise RuntimeError('Replicated maintenance lease was lost')
                except Exception:
                    time.sleep(1)
                    continue
            if not args.hold:
                time.sleep(1)
        if child:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait()
                raise RuntimeError('Maintenance budget exhausted')
            if child.returncode:
                raise SystemExit(child.returncode)
    finally:
        try:
            worker.sql('SELECT secret_control.maintenance_end(%s)', (owner,))
        except Exception:
            pass
        stack.close()


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('Maintenance stopped: ' + type(error).__name__, file=sys.stderr)
        raise SystemExit(1)
