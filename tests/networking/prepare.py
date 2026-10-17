#!/usr/bin/env python3
import json
from pathlib import Path
import yaml

root = Path(__file__).resolve().parents[2]
image = json.loads((root / 'versions.lock.json').read_text())['odoo_image']
objects = [
    {
        'apiVersion': 'v1',
        'kind': 'Namespace',
        'metadata': {
            'name': 'odoo-network-test',
            'labels': {
                'odoo.io/environment': 'dev',
                'odoo.io/network-test': 'true',
                'pod-security.kubernetes.io/enforce': 'restricted',
            },
        },
    }
]
for name, node in [
    ('server', 'odoo-dev-worker'),
    ('allowed', 'odoo-dev-worker2'),
    ('denied', 'odoo-dev-worker2'),
]:
    command = (
        ['python3', '-m', 'http.server', '8080', '--directory', '/tmp']
        if name == 'server'
        else ['python3', '-c', 'import time; time.sleep(1800)']
    )
    objects.append(
        {
            'apiVersion': 'v1',
            'kind': 'Pod',
            'metadata': {'name': name, 'namespace': 'odoo-network-test', 'labels': {'role': name}},
            'spec': {
                'nodeName': node,
                'automountServiceAccountToken': False,
                'securityContext': {
                    'runAsUser': 1000,
                    'runAsGroup': 1000,
                    'runAsNonRoot': True,
                    'seccompProfile': {'type': 'RuntimeDefault'},
                },
                'containers': [
                    {
                        'name': 'test',
                        'image': image,
                        'imagePullPolicy': 'IfNotPresent',
                        'command': command,
                        'securityContext': {
                            'allowPrivilegeEscalation': False,
                            'readOnlyRootFilesystem': True,
                            'capabilities': {'drop': ['ALL']},
                        },
                        'resources': {
                            'requests': {'cpu': '25m', 'memory': '32Mi'},
                            'limits': {'cpu': '200m', 'memory': '128Mi'},
                        },
                    }
                ],
            },
        }
    )
objects.append(
    {
        'apiVersion': 'v1',
        'kind': 'Service',
        'metadata': {'name': 'server', 'namespace': 'odoo-network-test'},
        'spec': {'selector': {'role': 'server'}, 'ports': [{'port': 8080}]},
    }
)
(root / '.state/network-test.yaml').write_text(yaml.safe_dump_all(objects, sort_keys=False))
