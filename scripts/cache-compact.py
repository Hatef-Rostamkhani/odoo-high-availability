#!/usr/bin/env python3
"""Remove unreferenced OCI cache blobs while preserving verified image archives."""

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tarfile


def required_blobs(archive):
    required = set()
    pending = [m['digest'] for m in json.load(archive.extractfile('index.json'))['manifests']]
    while pending:
        digest = pending.pop()
        if digest in required:
            continue
        if not digest.startswith('sha256:') or len(digest) != 71:
            raise ValueError('Unsupported OCI digest')
        required.add(digest)
        member = archive.getmember('blobs/sha256/' + digest[7:])
        with archive.extractfile(member) as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != digest[7:]:
                raise ValueError('Archive content digest mismatch')
        if member.size < 1024 * 1024:
            try:
                obj = json.load(archive.extractfile(member))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            pending.extend(
                m['digest']
                for m in obj.get('manifests', []) + obj.get('layers', [])
                if isinstance(m, dict) and 'digest' in m
            )
            if isinstance(obj.get('config'), dict) and 'digest' in obj['config']:
                pending.append(obj['config']['digest'])
    return required


def compact(path, write=False):
    path = Path(path)
    with path.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with tarfile.open(path) as source:
            original_index = json.load(source.extractfile('index.json'))
            required = required_blobs(source)
            keep = [
                m
                for m in source.getmembers()
                if not m.isfile()
                or not m.name.startswith('blobs/sha256/')
                or 'sha256:' + m.name.rsplit('/', 1)[-1] in required
            ]
            removable = sum(m.size for m in source.getmembers() if m not in keep)
            if write and removable:
                temporary = path.with_suffix('.compact.partial')
                try:
                    with tarfile.open(temporary, 'w') as output:
                        for member in keep:
                            output.addfile(
                                member, source.extractfile(member) if member.isfile() else None
                            )
                    with tarfile.open(temporary) as verified:
                        if required_blobs(verified) != required:
                            raise ValueError('Compacted archive changed its image graph')
                    with temporary.open('rb') as stream:
                        os.fsync(stream.fileno())
                    temporary.replace(path)
                finally:
                    temporary.unlink(missing_ok=True)
        # Completed layouts are redundant only when every file is identical to
        # the still-preserved archive. Keep unknown/partial layouts for recovery.
        layout = path.parents[1] / 'oci' / path.stem
        removed_layout = 0
        try:
            completed_layout = json.loads((layout / 'index.json').read_text()) == original_index
        except (OSError, json.JSONDecodeError):
            completed_layout = False
        if write and completed_layout:
            with tarfile.open(path) as source:
                members = {m.name: m for m in source.getmembers() if m.isfile()}
                for file in layout.glob('blobs/sha256/*'):
                    if not file.is_file() or file.is_symlink():
                        continue
                    with file.open('rb') as stream:
                        actual = hashlib.file_digest(stream, 'sha256').hexdigest()
                    # Unreferenced blobs were previously copied from interrupted
                    # downloads; delete only content-verified cache objects.
                    if actual != file.name:
                        continue
                    name = 'blobs/sha256/' + file.name
                    if 'sha256:' + file.name in required:
                        member = members.get(name)
                        if member is None or member.size != file.stat().st_size:
                            continue
                    removed_layout += file.stat().st_size
                    file.unlink()
        return {
            'archive': path.name,
            'archive_bytes_removed': removable if write else 0,
            'layout_bytes_removed': removed_layout,
            'reclaimable_archive_bytes': removable,
            'required_blobs': len(required),
        }


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--write', action='store_true')
    parser.add_argument('archives', nargs='+')
    args = parser.parse_args()
    for archive in args.archives:
        print(json.dumps(compact(archive, args.write)))
