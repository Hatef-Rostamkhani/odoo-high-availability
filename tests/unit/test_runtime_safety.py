"""Exercise failure cleanup and secret handling without touching a live cluster."""

import base64
from contextlib import ExitStack, nullcontext
import json
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
import ha
from odoo_platform.basic import BasicEnvironment, OWNER_LABEL
from odoo_platform.runtime import CommandRunner, SecretRedactor, require_owner


class RuntimeSafetyTests(unittest.TestCase):
    def test_failed_command_redacts_plain_and_encoded_credentials(self):
        value = secrets.token_urlsafe(36)
        result = subprocess.CompletedProcess(
            ['tool'], 1, b'', (value + ' ' + base64.b64encode(value.encode()).decode()).encode()
        )
        with mock.patch('odoo_platform.runtime.subprocess.run', return_value=result):
            with self.assertRaises(RuntimeError) as caught:
                CommandRunner(ROOT, SecretRedactor([value])).run(['tool', value])
        self.assertNotIn(value, str(caught.exception))
        self.assertNotIn(base64.b64encode(value.encode()).decode(), str(caught.exception))
        self.assertIn('[REDACTED]', str(caught.exception))

    def test_timeout_never_exposes_stdin_or_partial_output(self):
        value = secrets.token_urlsafe(36)
        timeout = subprocess.TimeoutExpired(
            ['tool', value], 1, output=value.encode(), stderr=value.encode()
        )
        with mock.patch('odoo_platform.runtime.subprocess.run', side_effect=timeout):
            with self.assertRaises(TimeoutError) as caught:
                CommandRunner(ROOT).run(['tool', value], data=value.encode(), timeout=1)
        self.assertNotIn(value, str(caught.exception))

    def test_sensitive_recovery_command_omits_unregistered_response_values(self):
        value = secrets.token_urlsafe(36)
        result = subprocess.CompletedProcess(['tool'], 1, b'', value.encode())
        with mock.patch('odoo_platform.runtime.subprocess.run', return_value=result):
            with self.assertRaisesRegex(RuntimeError, 'response omitted') as caught:
                CommandRunner(ROOT, include_stderr=False).run(['tool'])
        self.assertNotIn(value, str(caught.exception))

    def test_unowned_resource_rejected(self):
        for document in ({}, {'metadata': {'labels': {OWNER_LABEL: 'someone-else'}}}):
            with self.assertRaises(RuntimeError):
                require_owner(document, OWNER_LABEL, 'odoo-basic')

    def test_unowned_cluster_cannot_be_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = BasicEnvironment(root=directory)
            with (
                mock.patch.object(environment, 'exists', return_value=True),
                mock.patch.object(environment.runner, 'run') as run,
            ):
                with self.assertRaises(RuntimeError):
                    environment.down()
                run.assert_not_called()

    def test_failed_basic_backup_restarts_application(self):
        environment = BasicEnvironment()
        calls = []

        def kubectl(*args, **kwargs):
            calls.append(args)
            if 'pg_dump' in args:
                raise RuntimeError('Database backup failed')
            return b''

        with (
            mock.patch.object(environment, 'kubectl', side_effect=kubectl),
            mock.patch.object(environment, 'volume_helper'),
            mock.patch.object(environment, 'wait_application') as wait,
        ):
            with self.assertRaisesRegex(RuntimeError, 'backup failed'):
                environment.capture()
        self.assertTrue(any('--replicas=1' in args for args in calls))
        self.assertTrue(any('delete' in args and 'basic-filestore' in args for args in calls))
        wait.assert_called_once()

    def test_existing_restore_namespace_rejected_before_capture(self):
        environment = BasicEnvironment()
        existing = json.dumps({'items': [{'metadata': {'name': 'odoo-restore-test'}}]}).encode()
        with (
            mock.patch.object(environment, 'kubectl', return_value=existing),
            mock.patch.object(environment, 'capture') as capture,
        ):
            with self.assertRaisesRegex(RuntimeError, 'occupied'):
                environment.restore_test({})
        capture.assert_not_called()

    def test_expired_migration_deadline_rejects_operation(self):
        with (
            mock.patch.object(ha, 'DEADLINE', 10),
            mock.patch.object(ha.time, 'monotonic', return_value=11),
        ):
            with self.assertRaises(TimeoutError):
                ha.remaining(30)


class MigrationFailureTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.root = Path(directory)
        self.state = self.root / '.state'
        self.state.mkdir()
        values = self.root / 'gitops/environments/dev'
        values.mkdir(parents=True)
        self.original_odoo = 'database: {backend: standalone}\ninitialization: {enabled: false}\n'
        self.original_postgres = 'replicaCount: 1\n'
        (values / 'odoo.yaml').write_text(self.original_odoo)
        (values / 'postgresql.yaml').write_text(self.original_postgres)
        self.backup = self.root / 'backup'
        self.backup.mkdir()
        (self.backup / 'manifest.json').write_text('{}')
        self.patches = {}
        self.stack.enter_context(mock.patch.object(ha, 'ROOT', self.root))
        self.stack.enter_context(mock.patch.object(ha, 'STATE', self.state))
        self.stack.enter_context(mock.patch.dict(ha.os.environ, {'HA_ROLLBACK_REHEARSAL': 'false'}))
        returns = {
            'active': ('standalone',),
            'maintenance': nullcontext(),
            'app_snapshot': {'id': 1},
            'db_baseline': [{'table': 'fixture', 'rows': 1}],
            'capture': (self.backup, b'dump'),
        }
        for name in (
            'active',
            'maintenance',
            'status',
            'require_gates',
            'sync_credentials',
            'app_snapshot',
            'stop_app',
            'start_app',
            'wait',
            'db_baseline',
            'helper_start',
            'helper_stop',
            'capture',
            'restore_dump',
            'publish_values',
            'k',
            'evidence',
        ):
            self.patches[name] = self.stack.enter_context(
                mock.patch.object(ha, name, return_value=returns.get(name))
            )
        socket = self.stack.enter_context(mock.patch.object(ha.socket, 'socket'))
        socket.return_value.__enter__.return_value.connect_ex.return_value = 1

    def assert_aborted(self):
        record = json.loads((self.state / 'ha-migration.json').read_text())
        self.assertTrue(record['aborted'])
        self.assertFalse(record['committed'])
        self.assertIsNone(ha.DEADLINE)
        self.assertEqual((self.state / 'ha-migration.json').stat().st_mode & 0o777, 0o600)

    def test_backup_failure_resumes_source_without_restoring_target(self):
        self.patches['capture'].side_effect = RuntimeError('Backup failed')
        with self.assertRaisesRegex(RuntimeError, 'Backup failed'):
            ha.migration('migrate')
        self.patches['restore_dump'].assert_not_called()
        self.patches['publish_values'].assert_not_called()
        self.patches['start_app'].assert_called_once()
        self.patches['helper_stop'].assert_called_once()
        self.assert_aborted()

    def test_interruption_restores_signal_handlers_and_resumes_source(self):
        handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        self.patches['capture'].side_effect = InterruptedError('Interrupted')
        with self.assertRaises(InterruptedError):
            ha.migration('migrate')
        self.assertEqual(handlers, {sig: signal.getsignal(sig) for sig in handlers})
        self.patches['start_app'].assert_called_once()
        self.assert_aborted()

    def test_private_validation_failure_restores_original_configuration(self):
        self.patches['app_snapshot'].side_effect = [{'id': 1}, {'id': 2}]
        with self.assertRaisesRegex(AssertionError, 'changed during migration'):
            ha.migration('migrate')
        calls = self.patches['publish_values'].call_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[-1].kwargs['odoo_text'], self.original_odoo)
        self.assertEqual(calls[-1].kwargs['postgres_text'], self.original_postgres)
        self.assert_aborted()
