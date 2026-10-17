#!/usr/bin/env python3
"""Verify actual scrape data and exercise alert rules without live faults."""

import json
import base64
import urllib.parse
import urllib.request

import yaml

import openbao as b
from secrets_platform import apply


def alert_tests():
    rules = yaml.safe_load(
        (b.ROOT / 'gitops/platform/monitoring/files/secrets-rules.yaml').read_text()
    )['spec']
    by_name = {rule['alert']: rule for group in rules['groups'] for rule in group['rules']}
    cases = [
        (
            'OdooExpiryGuardMissing',
            [
                (
                    'kube_deployment_status_replicas_available{namespace="secrets-system",deployment="secret-expiry-guard"}',
                    '1+0x180',
                )
            ],
            {'namespace': 'secrets-system', 'deployment': 'secret-expiry-guard'},
        ),
        (
            'OdooExpiryGuaranteeLost',
            [],
            {'namespace': 'secrets-system', 'service': 'secret-expiry-guard'},
        ),
        (
            'OdooCredentialDeadlineNear',
            [
                (
                    'odoo_secret_credential_remaining_seconds{namespace="secrets-system"}',
                    '500+0x180',
                ),
                ('odoo_secrets_enabled{namespace="secrets-system"}', '1+0x180'),
            ],
            {},
        ),
        (
            'OdooCredentialRotationFailed',
            [
                (
                    'odoo_secret_rotation_last_error_timestamp_seconds{namespace="secrets-system",service="secret-rotator"}',
                    '100+0x180',
                ),
                (
                    'odoo_secret_rotation_last_success_timestamp_seconds{namespace="secrets-system",service="secret-rotator"}',
                    '50+0x180',
                ),
                ('odoo_secrets_enabled{namespace="secrets-system"}', '1+0x180'),
            ],
            {},
        ),
    ]
    tests = []
    for name, series, labels in cases:
        rule = by_name[name]
        tests.append(
            {
                'name': name,
                'interval': '1s',
                'input_series': [{'series': name, 'values': values} for name, values in series],
                'alert_rule_test': [
                    {
                        'eval_time': '2m',
                        'alertname': name,
                        'exp_alerts': [
                            {
                                'exp_labels': {**labels, **rule['labels']},
                                'exp_annotations': rule['annotations'],
                            }
                        ],
                    }
                ],
            }
        )
    tests.append(
        {
            'name': 'Healthy guards and unexpired credential',
            'interval': '1s',
            'input_series': [
                {
                    'series': 'kube_deployment_status_replicas_available{namespace="secrets-system",deployment="secret-expiry-guard"}',
                    'values': '2+0x180',
                },
                {
                    'series': 'odoo_secret_guard_last_success_timestamp_seconds{namespace="secrets-system",service="secret-expiry-guard"}',
                    'values': '0+1x180',
                },
                {
                    'series': 'odoo_secret_credential_remaining_seconds{namespace="secrets-system"}',
                    'values': '20000+0x180',
                },
                {'series': 'odoo_secrets_enabled{namespace="secrets-system"}', 'values': '1+0x180'},
            ],
            'alert_rule_test': [
                {'eval_time': '2m', 'alertname': name, 'exp_alerts': []}
                for name in (
                    'OdooExpiryGuardMissing',
                    'OdooExpiryGuaranteeLost',
                    'OdooCredentialDeadlineNear',
                )
            ],
        }
    )
    document = {'rule_files': ['/tests/rules.yaml'], 'evaluation_interval': '1s', 'tests': tests}
    name = 'odoo-secrets-alert-test'
    pods = json.loads(
        b.k(
            '-n',
            'monitoring',
            'get',
            'pods',
            '-l',
            'app.kubernetes.io/name=prometheus',
            '-o',
            'json',
        )
    )['items']
    image = next(c['image'] for c in pods[0]['spec']['containers'] if c['name'] == 'prometheus')
    try:
        apply(
            {
                'apiVersion': 'v1',
                'kind': 'ConfigMap',
                'metadata': {'name': name, 'namespace': 'monitoring'},
                'data': {
                    'rules.yaml': yaml.safe_dump(rules),
                    'tests.yaml': yaml.safe_dump(document),
                },
            }
        )
        apply(
            {
                'apiVersion': 'batch/v1',
                'kind': 'Job',
                'metadata': {'name': name, 'namespace': 'monitoring'},
                'spec': {
                    'backoffLimit': 0,
                    'activeDeadlineSeconds': 60,
                    'template': {
                        'spec': {
                            'restartPolicy': 'Never',
                            'automountServiceAccountToken': False,
                            'securityContext': {
                                'runAsUser': 65534,
                                'runAsGroup': 65534,
                                'runAsNonRoot': True,
                                'fsGroup': 65534,
                            },
                            'containers': [
                                {
                                    'name': 'promtool',
                                    'image': image,
                                    'command': [
                                        '/bin/promtool',
                                        'test',
                                        'rules',
                                        '/tests/tests.yaml',
                                    ],
                                    'resources': {
                                        'requests': {'cpu': '25m', 'memory': '32Mi'},
                                        'limits': {'cpu': '250m', 'memory': '128Mi'},
                                    },
                                    'securityContext': {
                                        'allowPrivilegeEscalation': False,
                                        'readOnlyRootFilesystem': True,
                                        'capabilities': {'drop': ['ALL']},
                                    },
                                    'volumeMounts': [
                                        {'name': 'tests', 'mountPath': '/tests', 'readOnly': True},
                                        {'name': 'tmp', 'mountPath': '/tmp'},
                                    ],
                                }
                            ],
                            'volumes': [
                                {'name': 'tests', 'configMap': {'name': name}},
                                {'name': 'tmp', 'emptyDir': {'sizeLimit': '64Mi'}},
                            ],
                        }
                    },
                },
            }
        )

        def complete():
            status = b.obj('monitoring', 'job', name).get('status', {})
            if status.get('failed'):
                print(b.k('-n', 'monitoring', 'logs', 'job/' + name).decode())
                raise AssertionError('Alert evaluation job failed')
            return status.get('succeeded') == 1

        b.wait(complete, 75)
        return [case[0] for case in cases]
    finally:
        b.k(
            '-n',
            'monitoring',
            'delete',
            'job,configmap',
            name,
            '--ignore-not-found',
            '--wait=true',
            '--timeout=30s',
        )


def verify():
    alerts = alert_tests()
    with b.forward('prometheus-monitoring-prometheus-0', 'monitoring', 9090) as address:
        address = address.replace('https:', 'http:')

        def api(path):
            with urllib.request.urlopen(address + '/api/v1/' + path, timeout=10) as response:
                return json.load(response)['data']

        targets = api('targets')['activeTargets']
        selected = [
            target
            for target in targets
            if target['labels'].get('namespace')
            in ('openbao', 'external-secrets', 'secrets-system')
        ]
        assert selected and all(target['health'] == 'up' for target in selected)
        checks = {
            'unsealed_members': 'count(max by(pod) (vault_core_unsealed{namespace="openbao"}) == 1)',
            'guard_samples': 'count(odoo_secret_guard_last_success_timestamp_seconds{namespace="secrets-system",service="secret-expiry-guard"})',
            'live_credential': 'odoo_secret_credential_remaining_seconds{namespace="secrets-system",service="secret-rotator"}',
            'cron': 'odoo_odoo_cron_heartbeat_timestamp_seconds{namespace="odoo-dev"}',
            'session': 'odoo_odoo_session_up',
        }
        results = {
            name: api('query?' + urllib.parse.urlencode({'query': query}))['result']
            for name, query in checks.items()
        }
        assert float(results['unsealed_members'][0]['value'][1]) == 3
        assert float(results['guard_samples'][0]['value'][1]) == 2
        assert all(results.values()), 'Required monitoring series is missing'
        assert all(0 < float(item['value'][1]) <= 86400 for item in results['live_credential'])
        assert all(float(item['value'][1]) == 1 for item in results['session'])
        groups = api('rules')['groups']
        live = next(group for group in groups if group['name'] == 'odoo.secrets')
        assert all(rule['health'] == 'ok' for rule in live['rules'])
    dashboard = json.loads((b.ROOT / 'gitops/platform/monitoring/files/secrets.json').read_text())
    deployment = b.obj('monitoring', 'deployment', 'monitoring-grafana')
    environment = {
        entry['name']: entry
        for container in deployment['spec']['template']['spec']['containers']
        for entry in container.get('env', [])
    }
    credentials = []
    for name in ('GF_SECURITY_ADMIN_USER', 'GF_SECURITY_ADMIN_PASSWORD'):
        reference = environment[name]['valueFrom']['secretKeyRef']
        credentials.append(
            base64.b64decode(
                b.obj('monitoring', 'secret', reference['name'])['data'][reference['key']]
            )
        )
    user, password = credentials
    pod = json.loads(
        b.k('-n', 'monitoring', 'get', 'pods', '-l', 'app.kubernetes.io/name=grafana', '-o', 'json')
    )['items'][0]
    with b.forward(pod['metadata']['name'], 'monitoring', 3000) as address:
        request = urllib.request.Request(
            address.replace('https:', 'http:') + '/api/dashboards/uid/' + dashboard['uid'],
            headers={'Authorization': 'Basic ' + base64.b64encode(user + b':' + password).decode()},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            actual = json.load(response)['dashboard']
        assert actual['title'] == dashboard['title'] and len(actual['panels']) == len(
            dashboard['panels']
        )
    return b.record(
        'secrets-monitoring',
        healthy_scrape_targets=len(selected),
        actual_series=list(results),
        alert_rules_healthy=len(live['rules']),
        alert_failure_cases=alerts,
        grafana_dashboard_loaded=True,
        dashboard_panels=len(actual['panels']),
    )


if __name__ == '__main__':
    try:
        verify()
    except Exception as error:
        b.record('secrets-monitoring', 'FAIL', error_type=type(error).__name__)
        raise
