#!/usr/bin/env python3
"""Persistent OCI layers and per-process proxy fallback, without host configuration edits."""

import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

root = Path(__file__).resolve().parents[1]
image, archive_name = sys.argv[1:]
archive = Path(archive_name)
key = hashlib.sha256(image.encode()).hexdigest()
layout = root / '.cache/oci' / key
layout.mkdir(parents=True, exist_ok=True)
lock = open(root / '.cache/images' / (key + '.lock'), 'w')
fcntl.flock(lock, fcntl.LOCK_EX)
if archive.exists() and archive.stat().st_size:
    raise SystemExit(0)
source = image
if '@sha256:' in image:
    repository, digest = image.split('@', 1)
    if ':' in repository.rsplit('/', 1)[-1]:
        repository = repository.rsplit(':', 1)[0]
    source = repository + '@' + digest
mode = os.environ.get('IMAGE_PROXY_MODE', 'auto')
if mode not in ('auto', 'host', 'direct'):
    raise SystemExit('IMAGE_PROXY_MODE must be auto, host, or direct')
host_environment = os.environ.copy()
direct_environment = os.environ.copy()
for variable in (
    'HTTP_PROXY',
    'HTTPS_PROXY',
    'ALL_PROXY',
    'http_proxy',
    'https_proxy',
    'all_proxy',
):
    direct_environment.pop(variable, None)
# Recover only completed, content-verified layers left by an interrupted Skopeo archive.
for interrupted in Path('/var/tmp').glob('container_images_oci*'):
    for blob in (interrupted / 'blobs/sha256').glob('*'):
        if not blob.is_file() or len(blob.name) != 64:
            continue
        target = layout / 'blobs/sha256' / blob.name
        if target.exists():
            continue
        try:
            with blob.open('rb') as stream:
                actual = hashlib.file_digest(stream, 'sha256').hexdigest()
            if actual == blob.name:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(blob, target)
        except PermissionError:
            continue
environments = (
    [('host', host_environment), ('direct', direct_environment)]
    if mode == 'auto'
    else [(mode, host_environment if mode == 'host' else direct_environment)]
)
success = False
for label, environment in environments:
    print(f'Image transport: {label}; existing verified OCI layers will be reused', flush=True)
    result = subprocess.run(
        [
            'timeout',
            '600',
            'skopeo',
            '--override-os',
            'linux',
            '--override-arch',
            'amd64',
            'copy',
            '--retry-times',
            '3',
            '--preserve-digests',
            'docker://' + source,
            'oci:' + str(layout) + ':' + image,
        ],
        env=environment,
    )
    if result.returncode == 0:
        success = True
        break
if not success:
    raise SystemExit(
        'Image download failed through the allowed transports; retained layers for retry'
    )
# Archive creation is atomic, so an interrupted archive is never treated as complete.
partial = archive.with_suffix('.partial')
with tarfile.open(partial, 'w') as stream:
    for child in layout.iterdir():
        stream.add(child, arcname=child.name)
with partial.open('rb') as stream:
    os.fsync(stream.fileno())
partial.replace(archive)
print('Cached and verified ' + image, flush=True)
