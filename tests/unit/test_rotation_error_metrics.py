import contextlib
import datetime as dt
import importlib.util
import io
from pathlib import Path
import signal
import sys
import types
import unittest
from unittest import mock


spec = importlib.util.spec_from_file_location(
    'rotation_worker', Path(__file__).resolve().parents[2] / 'infrastructure/secrets/worker.py'
)
worker_module = importlib.util.module_from_spec(spec)
try:
    import psycopg2
except ModuleNotFoundError:
    # These unit cases never open a database; only the retry/metric distinction
    # is under test. Live acceptance separately uses the real pinned adapter.
    with mock.patch.dict(sys.modules, {'psycopg2': types.ModuleType('psycopg2')}):
        spec.loader.exec_module(worker_module)
else:
    spec.loader.exec_module(worker_module)


class UnreachableDatabase(worker_module.Worker):
    def serve_metrics(self):
        pass

    def close(self):
        pass

    def sql(self, *args):
        self.stopping = True
        raise OSError('Database is unavailable')

    def status(self):
        return self.sql()


class RotationErrorMetrics(unittest.TestCase):
    def simulate(self, mode, remaining):
        worker = UnreachableDatabase({'mode': mode})
        worker.state = {
            'configuration': {'enabled': True, 'active_generation': 'test', 'lead_seconds': 900},
            'generations': [
                {
                    'generation': 'test',
                    'expires_at': (
                        dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=remaining)
                    ).isoformat(),
                }
            ],
            'operation': None,
        }
        handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                worker.loop()
        finally:
            for sig, handler in handlers.items():
                signal.signal(sig, handler)
        return worker.last_error

    def test_unrelated_database_fault_is_not_rotation_failure(self):
        self.assertEqual(self.simulate('controller', 3600), 0)

    def test_database_fault_when_rotation_is_due_is_failure(self):
        self.assertGreater(self.simulate('controller', 60), 0)

    def test_guard_fault_does_not_emit_controller_rotation_failure(self):
        self.assertEqual(self.simulate('guard', 60), 0)


if __name__ == '__main__':
    unittest.main()
