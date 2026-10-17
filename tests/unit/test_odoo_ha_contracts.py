"""Reject unsafe HA combinations and retain the single-instance recovery path."""

import json
from pathlib import Path
import subprocess
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def render(*args):
    result = subprocess.run(
        [
            str(ROOT / '.tools/bin/helm'),
            'template',
            'odoo',
            str(ROOT / 'helm/odoo'),
            '-n',
            'odoo-dev',
            '-f',
            str(ROOT / 'gitops/environments/dev/odoo-ha.yaml'),
            *args,
        ],
        capture_output=True,
        text=True,
    )
    return result, [
        d for d in yaml.safe_load_all(result.stdout) if d
    ] if result.returncode == 0 else []


class OdooHAContracts(unittest.TestCase):
    def test_ha_requires_shared_claim(self):
        result, _ = render('--set', 'storage.existingClaim=')
        self.assertNotEqual(result.returncode, 0)

    def test_web_cannot_run_cron(self):
        result, _ = render('--set', 'cronThreads=1')
        self.assertNotEqual(result.returncode, 0)

    def test_ha_cannot_reinitialize_database(self):
        result, _ = render('--set', 'initialization.enabled=true')
        self.assertNotEqual(result.returncode, 0)

    def test_rwo_cannot_be_used_for_ha(self):
        result, _ = render('--set-json', 'storage.accessModes=["ReadWriteOnce"]')
        self.assertNotEqual(result.returncode, 0)

    def test_cron_is_excluded_from_web_routing(self):
        result, docs = render()
        self.assertEqual(result.returncode, 0, result.stderr)
        service = next(
            d for d in docs if d['kind'] == 'Service' and d['metadata']['name'] == 'odoo'
        )
        self.assertEqual(service['spec']['selector']['odoo.io/component'], 'server')
        cron = next(
            d for d in docs if d['kind'] == 'Deployment' and d['metadata']['name'] == 'odoo-cron'
        )
        self.assertEqual(cron['spec']['template']['spec']['containers'][0]['args'], ['cron'])
        self.assertNotIn('ports', cron['spec']['template']['spec']['containers'][0])

    def test_surge_can_schedule_on_two_workers(self):
        result, docs = render()
        self.assertEqual(result.returncode, 0, result.stderr)
        web = next(d for d in docs if d['kind'] == 'Deployment' and d['metadata']['name'] == 'odoo')
        self.assertEqual(
            web['spec']['strategy']['rollingUpdate'], {'maxSurge': 1, 'maxUnavailable': 0}
        )
        affinity = web['spec']['template']['spec']['affinity']['podAntiAffinity']
        self.assertNotIn('requiredDuringSchedulingIgnoredDuringExecution', affinity)

    def test_maintenance_can_stop_all_writers(self):
        result, docs = render('--set', 'replicaCount=0', '--set', 'cron.replicaCount=0')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(all(d['spec']['replicas'] == 0 for d in docs if d['kind'] == 'Deployment'))


if __name__ == '__main__':
    unittest.main()
