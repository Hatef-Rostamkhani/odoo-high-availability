import json
from pathlib import Path
import subprocess
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def render(chart, *values):
    command = [
        str(ROOT / '.tools/bin/helm'),
        'template',
        'test',
        str(ROOT / 'helm' / chart),
        '-n',
        'odoo-dev',
    ]
    command += list(values)
    result = subprocess.run(command, capture_output=True, text=True)
    return result, list(yaml.safe_load_all(result.stdout)) if result.returncode == 0 else []


class DeploymentContracts(unittest.TestCase):
    def test_initializer_can_bind_delayed_volume(self):
        result, docs = render('odoo')
        self.assertEqual(result.returncode, 0, result.stderr)
        by_kind = {d['kind']: d for d in docs if d}
        self.assertEqual(
            by_kind['Job']['metadata']['annotations']['argocd.argoproj.io/sync-wave'],
            by_kind['PersistentVolumeClaim']['metadata']['annotations'][
                'argocd.argoproj.io/sync-wave'
            ],
        )
        self.assertLess(
            int(by_kind['ConfigMap']['metadata']['annotations']['argocd.argoproj.io/sync-wave']),
            int(by_kind['Job']['metadata']['annotations']['argocd.argoproj.io/sync-wave']),
        )

    def test_multiple_replicas_rejected(self):
        result, _ = render('odoo', '--set', 'replicaCount=2')
        self.assertNotEqual(result.returncode, 0)

    def test_restore_has_no_initializer_or_cron(self):
        result, docs = render(
            'odoo', '--set', 'initialization.enabled=false', '--set', 'cronThreads=0'
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('Job', [d['kind'] for d in docs if d])
        config = next(d for d in docs if d and d['kind'] == 'ConfigMap')
        self.assertEqual(json.loads(config['data']['settings.json'])['cron'], 0)

    def test_external_database_policy_peer(self):
        result, docs = render(
            'odoo',
            '--set',
            'database.networkPeer.ipBlock.cidr=192.0.2.0/24',
            '--set',
            'database.networkPeer.podSelector=null',
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        policy = next(d for d in docs if d and d['kind'] == 'NetworkPolicy')
        self.assertEqual(policy['spec']['egress'][0]['to'][0]['ipBlock']['cidr'], '192.0.2.0/24')

    def test_existing_filestore_claim_not_recreated(self):
        result, docs = render('odoo', '--set', 'storage.existingClaim=shared-filestore')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('PersistentVolumeClaim', [d['kind'] for d in docs if d])
        deployment = next(d for d in docs if d and d['kind'] == 'Deployment')
        self.assertEqual(
            deployment['spec']['template']['spec']['volumes'][0]['persistentVolumeClaim'][
                'claimName'
            ],
            'shared-filestore',
        )


if __name__ == '__main__':
    unittest.main()
