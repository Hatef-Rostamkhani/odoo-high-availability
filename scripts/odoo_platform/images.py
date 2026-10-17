"""Verify a cached platform manifest against a pinned upstream image index."""

import hashlib
import json
import tarfile


def verify_cached_image(root, application, reference, archive):
    payload = (root / 'tests/ci/images' / (application + '-index.json')).read_bytes()
    root_digest = reference.split('@')[1]
    if 'sha256:' + hashlib.sha256(payload).hexdigest() != root_digest:
        raise RuntimeError('Pinned upstream image index mismatch')
    allowed = {
        entry['digest']
        for entry in json.loads(payload)['manifests']
        if entry.get('platform', {}).get('architecture') == 'amd64'
        and entry.get('platform', {}).get('os') == 'linux'
    } | {root_digest}
    with tarfile.open(archive) as stream:
        index = json.load(stream.extractfile('index.json'))
        if not index.get('manifests'):
            raise RuntimeError('Cached image contains no manifests')
        for entry in index['manifests']:
            digest = entry['digest']
            if digest not in allowed:
                raise RuntimeError(
                    'Cached platform digest does not belong to the pinned upstream image'
                )
            content = stream.extractfile('blobs/sha256/' + digest.split(':')[1]).read()
            if 'sha256:' + hashlib.sha256(content).hexdigest() != digest:
                raise RuntimeError('Cached manifest content does not match its digest')
    return allowed
