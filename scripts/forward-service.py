#!/usr/bin/env python3
"""Keep one localhost Kubernetes forward connected when its selected pod changes."""

import signal
import subprocess
import sys
import threading

stopping = threading.Event()
child = None


def stop(_signal, _frame):
    stopping.set()
    if child is not None and child.poll() is None:
        child.terminate()


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
command = sys.argv[1:]
if not command or 'port-forward' not in command or '127.0.0.1' not in command:
    raise SystemExit('A localhost kubectl port-forward command is required')
command += ['--pod-running-timeout=10s']
while not stopping.is_set():
    child = subprocess.Popen(command)
    while child.poll() is None:
        if stopping.wait(0.2):
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
            break
    if stopping.is_set():
        break
    print('Port-forward exited; reconnecting to the current service pod.', flush=True)
    stopping.wait(1)
