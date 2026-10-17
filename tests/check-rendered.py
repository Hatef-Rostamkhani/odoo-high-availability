#!/usr/bin/env python3
import hashlib
import json
from pathlib import Path
import sys
import yaml

root = Path(__file__).resolve().parents[1]
versions = json.loads((root / 'versions.lock.json').read_text())
for application in ('odoo', 'postgres'):
    payload = (root / 'tests/ci/images' / (application + '-index.json')).read_bytes()
    assert (
        'sha256:' + hashlib.sha256(payload).hexdigest()
        == versions[application + '_image'].split('@')[1]
    ), 'CI image index lock differs from the pinned image'
for name, entry in json.loads((root / 'vendor/charts.lock.json').read_text()).items():
    assert hashlib.sha256((root / entry['path']).read_bytes()).hexdigest() == entry['sha256'], (
        f'{name}: vendor checksum mismatch'
    )
subprocess_files = list((root / '.state/rendered').glob('*.yaml'))
for path in subprocess_files:
    for doc in yaml.safe_load_all(path.read_text()):
        if not doc:
            continue
        if path.stem == 'monitoring':
            assert doc['kind'] not in (
                'ClusterRole',
                'ClusterRoleBinding',
                'CustomResourceDefinition',
            ), 'Argo monitoring source contains bootstrap-owned resource'
        if doc['kind'] in ('Deployment', 'StatefulSet', 'DaemonSet', 'Job'):
            pod = doc['spec']['template']['spec']
            for container in pod.get('containers', []) + pod.get('initContainers', []):
                image = container['image']
                assert '@sha256:' in image or (
                    ':' in image.rsplit('/', 1)[-1] and not image.endswith(':latest')
                ), f'Unpinned image: {image}'
                if path.stem in ('odoo', 'postgresql'):
                    assert pod['automountServiceAccountToken'] is False
                    assert pod['securityContext']['runAsNonRoot'] is True
                    assert container['securityContext']['allowPrivilegeEscalation'] is False
                    assert container['securityContext']['capabilities']['drop'] == ['ALL']
                    assert {'requests', 'limits'} <= container['resources'].keys()
print(
    'PASS: vendor checksums, pinned images, separated platform ownership, restricted application pods'
)
