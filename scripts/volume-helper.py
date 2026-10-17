#!/usr/bin/env python3
"""Create a restricted helper pod for a selected development PVC."""

import json
from pathlib import Path
import sys

root = Path(__file__).resolve().parents[1]
namespace, name, claim = sys.argv[1:4]
label = {'odoo.io/environment': 'dev', 'odoo.io/volume-helper': 'true'}
pod = {
    'apiVersion': 'v1',
    'kind': 'Pod',
    'metadata': {'name': name, 'namespace': namespace, 'labels': label},
    'spec': {
        'restartPolicy': 'Never',
        'terminationGracePeriodSeconds': 5,
        'automountServiceAccountToken': False,
        'securityContext': {
            'runAsUser': 1000,
            'runAsGroup': 1000,
            'runAsNonRoot': True,
            'fsGroup': 1000,
            'seccompProfile': {'type': 'RuntimeDefault'},
        },
        'containers': [
            {
                'name': 'helper',
                'image': json.loads((root / 'versions.lock.json').read_text())['odoo_image'],
                'imagePullPolicy': 'IfNotPresent',
                'command': [
                    'python3',
                    '-c',
                    'import signal,sys,time; signal.signal(signal.SIGTERM,lambda *_:sys.exit(0)); time.sleep(1800)',
                ],
                'securityContext': {
                    'allowPrivilegeEscalation': False,
                    'readOnlyRootFilesystem': True,
                    'capabilities': {'drop': ['ALL']},
                },
                'resources': {
                    'requests': {'cpu': '25m', 'memory': '32Mi'},
                    'limits': {'cpu': '200m', 'memory': '128Mi'},
                },
                'volumeMounts': [{'name': 'data', 'mountPath': '/data'}],
            }
        ],
        'volumes': [{'name': 'data', 'persistentVolumeClaim': {'claimName': claim}}],
    },
}
pod['spec']['securityContext']['fsGroupChangePolicy'] = 'OnRootMismatch'
print(json.dumps(pod))
