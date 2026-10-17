import datetime as dt
import importlib.util
import json
from pathlib import Path
import secrets
import tempfile
import unittest

spec = importlib.util.spec_from_file_location(
    'odoo_credentials', Path(__file__).resolve().parents[2] / 'helm/odoo/files/credentials.py'
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class CredentialBundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.version = self.root / 'version-1'
        self.version.mkdir()
        (self.root / '..data').symlink_to(self.version.name)
        self.now = dt.datetime.now(dt.timezone.utc)
        self.value = {
            'username': 'odoo_login_a',
            'password': secrets.token_urlsafe(36),
            'generation': secrets.token_hex(16),
            'issued_at': self.now.isoformat(),
            'expires_at': (self.now + dt.timedelta(hours=24)).isoformat(),
        }
        self.write()

    def write(self):
        (self.version / 'credentials.json').write_text(json.dumps(self.value))
        for key, value in self.value.items():
            (self.version / key).write_text(str(value))

    def test_valid_bundle(self):
        value, expiry = module.load_bundle(self.root, self.now)
        self.assertEqual(value, self.value)
        self.assertEqual(expiry, self.now + dt.timedelta(hours=24))

    def test_expired_bundle_rejected(self):
        self.value['expires_at'] = self.now.isoformat()
        self.write()
        with self.assertRaises(RuntimeError):
            module.load_bundle(self.root, self.now)

    def test_lifetime_extension_rejected(self):
        self.value['expires_at'] = (self.now + dt.timedelta(hours=24, seconds=1)).isoformat()
        self.write()
        with self.assertRaises(RuntimeError):
            module.load_bundle(self.root, self.now)

    def test_mixed_generation_rejected(self):
        (self.version / 'generation').write_text(secrets.token_hex(16))
        with self.assertRaises(RuntimeError):
            module.load_bundle(self.root, self.now)

    def test_admin_role_rejected(self):
        self.value['username'] = 'postgres'
        self.write()
        with self.assertRaises(RuntimeError):
            module.load_bundle(self.root, self.now)

    def test_naive_timestamp_rejected(self):
        self.value['expires_at'] = (
            (self.now + dt.timedelta(hours=1)).replace(tzinfo=None).isoformat()
        )
        self.write()
        with self.assertRaises(RuntimeError):
            module.load_bundle(self.root, self.now)

    def test_future_issuance_rejected(self):
        self.value['issued_at'] = (self.now + dt.timedelta(minutes=5)).isoformat()
        self.write()
        with self.assertRaises(RuntimeError):
            module.load_bundle(self.root, self.now)
