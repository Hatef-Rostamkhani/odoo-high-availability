import importlib.util
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('public_source', ROOT / 'scripts/public_source.py')
source = importlib.util.module_from_spec(spec)
spec.loader.exec_module(source)


class PublicSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git('init', '-q')
        (self.root / '.gitignore').write_text('.state/\nbackups/\n.env\ndist/\n')
        (self.root / 'README.md').write_text('Odoo development platform\n')
        self.git('add', '.')

    def git(self, *args):
        subprocess.run(['git', '-C', str(self.root), *args], check=True, capture_output=True)

    def test_export_excludes_history_and_ignored_credentials(self):
        (self.root / '.state').mkdir()
        (self.root / '.state/credentials.json').write_text('{"password":"private"}')
        (self.root / '.env').write_text('TOKEN=private')
        (self.root / 'new-test.py').write_text('print("public")\n')
        destination = self.root / 'dist/source.tar.gz'
        source.export_source(self.root, destination)
        with tarfile.open(destination) as archive:
            self.assertEqual(
                set(archive.getnames()),
                {'odoo-ha/.gitignore', 'odoo-ha/README.md', 'odoo-ha/new-test.py'},
            )
            self.assertTrue(all(entry.uname == '' and entry.uid == 0 for entry in archive))

    def test_force_tracked_private_file_rejected(self):
        (self.root / '.env').write_text('TOKEN=private')
        self.git('add', '-f', '.env')
        with self.assertRaisesRegex(ValueError, 'Private file'):
            source.source_files(self.root)

    def test_legacy_name_rejected_even_in_untracked_source(self):
        (self.root / 'new.md').write_text('Super' + 'pipe')
        with self.assertRaisesRegex(ValueError, 'Legacy name'):
            source.source_files(self.root)

    def test_persian_legacy_name_rejected(self):
        (self.root / 'new.md').write_text('سوپر ' + 'پایپ')
        with self.assertRaisesRegex(ValueError, 'Legacy name'):
            source.source_files(self.root)

    def test_symlink_to_private_file_rejected(self):
        (self.root / 'linked').symlink_to('/etc/passwd')
        with self.assertRaisesRegex(ValueError, 'regular files'):
            source.source_files(self.root)

    def test_deleted_file_excluded(self):
        (self.root / 'README.md').unlink()
        self.assertEqual(source.source_files(self.root), [Path('.gitignore')])

    def test_supplier_restricted_chart_rejected_when_force_tracked(self):
        (self.root / 'vendor').mkdir()
        (self.root / 'vendor/pgo-6.0.2.tgz').write_bytes(b'not for redistribution')
        self.git('add', '-f', 'vendor/pgo-6.0.2.tgz')
        with self.assertRaisesRegex(ValueError, 'not redistributed'):
            source.source_files(self.root)
