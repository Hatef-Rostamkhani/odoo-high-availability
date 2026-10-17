"""Safety contracts for HA placement, TLS and disposable PITR recovery."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
from ha import clone_manifest, backend_values


class HAContracts(unittest.TestCase):
    def render(self, chart, *values):
        return subprocess.run(
            [
                str(ROOT / '.tools/bin/helm'),
                'template',
                'test',
                str(ROOT / 'helm' / chart),
                *values,
            ],
            capture_output=True,
            text=True,
        )

    def test_crunchy_rejects_unverified_tls(self):
        result = self.render(
            'odoo',
            '--set',
            'database.backend=crunchy',
            '--set',
            'initialization.enabled=false',
            '--set',
            'database.tls.caSecret=odoo-ha-ca',
        )
        self.assertNotEqual(result.returncode, 0)

    def test_crunchy_mounts_ca_and_disables_initializer(self):
        result = self.render(
            'odoo',
            '--set',
            'database.backend=crunchy',
            '--set',
            'initialization.enabled=false',
            '--set',
            'database.tls.caSecret=odoo-ha-ca',
            '--set',
            'database.sslmode=verify-full',
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        docs = list(yaml.safe_load_all(result.stdout))
        self.assertNotIn('Job', [d['kind'] for d in docs if d])
        pod = next(d for d in docs if d and d['kind'] == 'Deployment')['spec']['template']['spec']
        self.assertIn(
            {'name': 'PGSSLROOTCERT', 'value': '/run/database-ca/ca.crt'},
            pod['containers'][0]['env'],
        )
        self.assertTrue(
            any(v.get('secret', {}).get('secretName') == 'odoo-ha-ca' for v in pod['volumes'])
        )
        for name in ('startupProbe', 'readinessProbe', 'livenessProbe'):
            self.assertEqual(pod['containers'][0][name]['timeoutSeconds'], 5)
        policy = next(d for d in docs if d and d['kind'] == 'NetworkPolicy')
        labels = policy['spec']['egress'][0]['to'][0]['podSelector']['matchLabels']
        self.assertEqual(labels, {'postgres-operator.crunchydata.com/cluster': 'odoo-ha'})

    def test_old_database_zero_retains_volumes(self):
        result = self.render('postgresql', '--set', 'replicaCount=0')
        self.assertEqual(result.returncode, 0, result.stderr)
        sts = next(d for d in yaml.safe_load_all(result.stdout) if d and d['kind'] == 'StatefulSet')
        self.assertEqual(sts['spec']['replicas'], 0)
        self.assertEqual(
            sts['spec']['persistentVolumeClaimRetentionPolicy'],
            {'whenDeleted': 'Retain', 'whenScaled': 'Retain'},
        )
        self.assertNotEqual(self.render('postgresql', '--set', 'replicaCount=2').returncode, 0)

    def test_pitr_clone_can_run_without_a_standby(self):
        doc = clone_manifest('2026-10-12 10:00:00+00')
        self.assertEqual(doc['metadata']['name'], 'odoo-ha-pitr')
        instance = doc['spec']['instances'][0]
        self.assertEqual(instance['replicas'], 1)
        self.assertEqual(instance['minAvailable'], 1)
        self.assertNotIn('affinity', instance)
        config = doc['spec']['patroni']['dynamicConfiguration']
        self.assertFalse(config['synchronous_mode'])
        self.assertFalse(config['synchronous_mode_strict'])
        self.assertTrue(
            all('schedules' not in repo for repo in doc['spec']['backups']['pgbackrest']['repos'])
        )

    def test_migration_values_preserve_database_and_filestore(self):
        original = {'storage': {'existingClaim': 'odoo-data'}}
        values = backend_values(original, 'crunchy')
        self.assertEqual(values['storage'], original['storage'])
        self.assertEqual(values['database']['name'], 'odoo_dev')
        self.assertEqual(values['cronThreads'], 0)
        self.assertFalse(values['initialization']['enabled'])
        self.assertEqual(values['database']['sslmode'], 'verify-full')

    def test_ha_requires_three_nodes_and_strict_replication(self):
        doc = yaml.safe_load((ROOT / 'infrastructure/database-ha/cluster.yaml').read_text())
        instance = doc['spec']['instances'][0]
        self.assertEqual(instance['replicas'], 3)
        self.assertEqual(instance['minAvailable'], 2)
        term = instance['affinity']['podAntiAffinity'][
            'requiredDuringSchedulingIgnoredDuringExecution'
        ][0]
        self.assertEqual(term['topologyKey'], 'kubernetes.io/hostname')
        config = doc['spec']['patroni']['dynamicConfiguration']
        self.assertTrue(config['synchronous_mode_strict'])
        self.assertEqual(config['synchronous_node_count'], 1)


if __name__ == '__main__':
    unittest.main()
