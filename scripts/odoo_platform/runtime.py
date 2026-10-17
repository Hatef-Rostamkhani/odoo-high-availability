"""Bounded command execution, redaction and ownership checks."""

import base64
import json
from pathlib import Path
import subprocess


class SecretRedactor:
    def __init__(self, values=()):
        self.values = set()
        for value in values:
            if isinstance(value, str) and value:
                self.values.update((value, base64.b64encode(value.encode()).decode()))

    @classmethod
    def from_files(cls, paths):
        values = []
        for path in paths:
            path = Path(path)
            if path.exists():
                values.extend(
                    value
                    for value in json.loads(path.read_text()).values()
                    if isinstance(value, str)
                )
        return cls(values)

    def redact(self, text):
        for value in sorted(self.values, key=len, reverse=True):
            text = text.replace(value, '[REDACTED]')
        return text


class CommandRunner:
    def __init__(self, root, redactor=None, *, include_stderr=True):
        self.root = Path(root)
        self.redactor = redactor or SecretRedactor()
        self.include_stderr = include_stderr

    def run(self, command, *, data=None, timeout=120, env=None):
        command = [str(value) for value in command]
        try:
            result = subprocess.run(
                command, input=data, capture_output=True, timeout=timeout, cwd=self.root, env=env
            )
        except subprocess.TimeoutExpired:
            raise TimeoutError(f'{Path(command[0]).name} exceeded its {timeout}s budget') from None
        if result.returncode:
            detail = (
                self.redactor.redact(result.stderr.decode(errors='replace'))[-2000:]
                if self.include_stderr
                else 'response omitted'
            )
            raise RuntimeError(
                f'{Path(command[0]).name} failed (exit {result.returncode}): {detail}'
            )
        return result.stdout


def require_owner(document, label, expected):
    if document.get('metadata', {}).get('labels', {}).get(label) != expected:
        raise RuntimeError('Resource ownership mismatch; operation refused')
