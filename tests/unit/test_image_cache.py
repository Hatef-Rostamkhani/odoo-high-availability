"""Refuse corrupted or unrelated OCI caches before importing a cluster image."""

import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
from odoo_platform.images import verify_cached_image


class ImageCacheTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.snapshot = self.root / 'tests/ci/images/postgres-index.json'
        self.snapshot.parent.mkdir(parents=True)
        self.content = b'{"schemaVersion":2,"layers":[]}'
        self.child = 'sha256:' + hashlib.sha256(self.content).hexdigest()
        payload = json.dumps(
            {
                'manifests': [
                    {'digest': self.child, 'platform': {'architecture': 'amd64', 'os': 'linux'}}
                ]
            }
        ).encode()
        self.snapshot.write_bytes(payload)
        self.reference = 'example.invalid/postgres@sha256:' + hashlib.sha256(payload).hexdigest()
        self.archive = self.root / 'image.tar'

    def cache(self, manifests, content=None):
        with tarfile.open(self.archive, 'w') as archive:
            files = {
                'index.json': json.dumps({'manifests': manifests}).encode(),
                'blobs/sha256/' + self.child.split(':')[1]: content or self.content,
            }
            for name, payload in files.items():
                info = tarfile.TarInfo(name)
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))

    def test_valid_platform_then_corrupted_manifest_is_rejected(self):
        self.cache([{'digest': self.child}])
        verify_cached_image(self.root, 'postgres', self.reference, self.archive)
        self.cache([{'digest': self.child}], b'corrupted image content')
        with self.assertRaisesRegex(RuntimeError, 'content does not match'):
            verify_cached_image(self.root, 'postgres', self.reference, self.archive)

    def test_unrelated_platform_and_empty_cache_are_rejected(self):
        for manifests in ([{'digest': 'sha256:' + '0' * 64}], []):
            self.cache(manifests)
            with self.assertRaises(RuntimeError):
                verify_cached_image(self.root, 'postgres', self.reference, self.archive)

    def test_changed_upstream_index_is_rejected(self):
        self.cache([{'digest': self.child}])
        self.snapshot.write_text('{}')
        with self.assertRaisesRegex(RuntimeError, 'upstream image index mismatch'):
            verify_cached_image(self.root, 'postgres', self.reference, self.archive)
