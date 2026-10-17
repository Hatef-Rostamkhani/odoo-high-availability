#!/usr/bin/env python3
import copy
import json
import os
from pathlib import Path
import subprocess

root = Path(__file__).resolve().parents[1]
namespace = os.environ.get('MODULE_NAMESPACE', 'odoo-dev')
command = [
    str(root / '.tools/bin/kubectl'),
    '--kubeconfig',
    str(root / '.state/kubeconfig'),
    '--context',
    'kind-odoo-dev',
    '-n',
    namespace,
]
deployment = json.loads(
    subprocess.check_output(command + ['get', 'deployment', 'odoo', '-o', 'json'], text=True)
)
template = copy.deepcopy(deployment['spec']['template'])
template['metadata']['labels']['odoo.io/component'] = 'module-upgrade'
template['spec']['restartPolicy'] = 'Never'
container = template['spec']['containers'][0]
template['spec']['containers'] = [container]
for key in ('startupProbe', 'readinessProbe', 'livenessProbe'):
    container.pop(key, None)
# Generate the same runtime config and run only the explicit upgrade, with HTTP/cron disabled.
container['command'] = [
    'python3',
    '-c',
    "import runpy,sys,os; sys.argv=['start.py','configuration']; runpy.run_path('/opt/odoo/start.py'); os.execvp('odoo',['odoo','-c','/run/odoo/odoo.conf','-d','odoo_dev',os.environ['ODOO_ACTION'],os.environ['ODOO_MODULES'],'--stop-after-init','--no-http','--max-cron-threads=0'])",
]
container.pop('args', None)
container['env'] = container.get('env', []) + [
    {'name': 'ODOO_MODULES', 'value': os.environ['MODULES']},
    {'name': 'ODOO_ACTION', 'value': '-i' if os.environ['MODULE_ACTION'] == 'install' else '-u'},
]
print(
    json.dumps(
        {
            'apiVersion': 'batch/v1',
            'kind': 'Job',
            'metadata': {'name': 'odoo-module-upgrade', 'namespace': namespace},
            'spec': {'backoffLimit': 0, 'activeDeadlineSeconds': 900, 'template': template},
        }
    )
)
