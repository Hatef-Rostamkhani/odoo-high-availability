"""Pure configuration builders for database HA and isolated PITR clones."""

import copy
import datetime as dt
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[2]
NS = 'postgres-ha'
CLUSTER = 'odoo-ha'


def clone_manifest(target):
    parsed = dt.datetime.fromisoformat(target.replace('Z', '+00:00'))
    assert parsed.tzinfo is not None and parsed.utcoffset() == dt.timedelta(0), (
        'PITR target must explicitly be UTC'
    )
    target = parsed.strftime('%Y-%m-%d %H:%M:%S.%f') + '+00'
    doc = copy.deepcopy(
        yaml.safe_load((ROOT / 'infrastructure/database-ha/cluster.yaml').read_text())
    )
    doc['metadata']['name'] = 'odoo-ha-pitr'
    doc['metadata']['labels'] = {'odoo.io/restore-test': 'true'}
    instance = doc['spec']['instances'][0]
    instance['replicas'] = 1
    instance['minAvailable'] = 1
    instance.pop('affinity', None)
    instance.pop('tolerations', None)
    config = doc['spec']['patroni']['dynamicConfiguration']
    config['synchronous_mode'] = False
    config['synchronous_mode_strict'] = False
    config['synchronous_node_count'] = 0
    for repo in doc['spec']['backups']['pgbackrest']['repos']:
        repo.pop('schedules', None)
    doc['spec']['dataSource'] = {
        'postgresCluster': {
            'clusterName': CLUSTER,
            'repoName': 'repo1',
            'options': ['--type=time', '--target="' + target + '"'],
        }
    }
    return doc


def backend_values(original, backend, cron=0):
    if backend not in ('crunchy', 'standalone'):
        raise ValueError('Unknown database backend')
    value = copy.deepcopy(original)
    value['initialization'] = {'enabled': False}
    value['cronThreads'] = cron
    if backend == 'crunchy':
        value['database'] = {
            'backend': 'crunchy',
            'host': 'odoo-ha-primary.postgres-ha.svc',
            'name': 'odoo_dev',
            'port': 5432,
            'user': 'odoo',
            'sslmode': 'verify-full',
            'existingSecret': 'odoo-ha-database',
            'tls': {'caSecret': 'odoo-ha-ca', 'caKey': 'ca.crt'},
            'networkPeer': {
                'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': NS}},
                'podSelector': {
                    'matchLabels': {'postgres-operator.crunchydata.com/cluster': CLUSTER}
                },
            },
        }
    else:
        if value.get('application', {}).get('mode') == 'ha':
            value.update(
                application={'mode': 'single'},
                replicaCount=1,
                workers=0,
                proxyMode=False,
                dbMaxConnections=16,
            )
            value['cron'] = {'enabled': False}
        value['database'] = {
            'backend': 'standalone',
            'host': 'postgresql',
            'name': 'odoo_dev',
            'port': 5432,
            'user': 'odoo',
            'sslmode': 'prefer',
            'existingSecret': 'odoo-database',
            'tls': {'caSecret': '', 'caKey': 'ca.crt'},
            'networkPeer': {
                'podSelector': {'matchLabels': {'app.kubernetes.io/name': 'postgresql'}}
            },
        }
    if value.get('application', {}).get('mode') == 'ha':
        value['cronThreads'] = 0
        value['cron']['replicaCount'] = 2 if cron else 0
    return value
