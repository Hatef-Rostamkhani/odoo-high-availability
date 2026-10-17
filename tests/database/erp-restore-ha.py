#!/usr/bin/env python3
"""Restore a complete HA ERP dump using the same Crunchy extension packages."""

import contextlib
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import signal
import sys
import yaml

# Rotating credential preparation shares the audited bootstrap driver. Keep its
# bounded PostgreSQL dependency in the project-local virtual environment.
try:
    import psycopg2
except ModuleNotFoundError:
    import os

    interpreter = Path(__file__).resolve().parents[2] / '.tools/secrets-python/bin/python'
    if interpreter.exists():
        os.execv(str(interpreter), [str(interpreter), *sys.argv])

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
from ha import obj, k, apply, wait, healthy, primary, sql, evidence, run

backup = Path(sys.argv[1])
name = 'odoo-ha-erp-restore'
namespace = 'odoo-restore-test'
cluster = copy.deepcopy(
    yaml.safe_load((ROOT / 'infrastructure/database-ha/cluster.yaml').read_text())
)
cluster['metadata'] = {
    'name': name,
    'namespace': 'postgres-ha',
    'labels': {'odoo.io/restore-test': 'true'},
}
instance = cluster['spec']['instances'][0]
instance['replicas'] = 1
instance['minAvailable'] = 1
instance.pop('affinity', None)
instance.pop('tolerations', None)
config = cluster['spec']['patroni']['dynamicConfiguration']
config['synchronous_mode'] = False
config['synchronous_mode_strict'] = False
config['synchronous_node_count'] = 0
cluster['spec'].pop('monitoring', None)
for repo in cluster['spec']['backups']['pgbackrest']['repos']:
    repo.pop('schedules', None)


def cleanup():
    exists = subprocess.run(
        [
            *__import__('database').K,
            '-n',
            'postgres-ha',
            'get',
            'postgrescluster',
            name,
            '-o',
            'json',
        ],
        capture_output=True,
    )
    if exists.returncode == 0:
        assert (
            json.loads(exists.stdout)['metadata'].get('labels', {}).get('odoo.io/restore-test')
            == 'true'
        )
        k(
            '-n',
            'postgres-ha',
            'delete',
            'postgrescluster',
            name,
            '--wait=true',
            '--timeout=120s',
            timeout=130,
        )
        k(
            '-n',
            'postgres-ha',
            'delete',
            'pvc',
            '-l',
            'postgres-operator.crunchydata.com/cluster=' + name,
            '--wait=true',
            '--timeout=120s',
            timeout=130,
        )
    k('-n', 'postgres-ha', 'delete', 'networkpolicy', 'erp-restore-access', '--ignore-not-found')


def interrupted(signum, frame):
    raise InterruptedError('ERP restore interrupted; cleaning isolated resources')


for sig in (signal.SIGINT, signal.SIGTERM):
    signal.signal(sig, interrupted)
cleanup()
try:
    password = obj(namespace, 'secret', 'odoo-database')['data']['password']
    secret = {
        'apiVersion': 'v1',
        'kind': 'Secret',
        'metadata': {
            'name': name + '-pguser-odoo',
            'namespace': 'postgres-ha',
            'labels': {
                'postgres-operator.crunchydata.com/cluster': name,
                'postgres-operator.crunchydata.com/pguser': 'odoo',
            },
        },
        'type': 'Opaque',
        'data': {'password': password},
    }
    k('create', '-f', '-', input=json.dumps(secret).encode())
    apply(cluster)
    wait(lambda: healthy(name, count=1), timeout=300)
    wait(
        lambda: (
            sql(
                "SELECT count(*) FROM pg_database WHERE datname='odoo_dev'",
                database='postgres',
                cluster=name,
            )
            == '1'
        ),
        timeout=60,
    )
    sql('ALTER DATABASE odoo_dev OWNER TO odoo', database='postgres', cluster=name)
    ca = obj('postgres-ha', 'secret', name + '-cluster-cert')['data']['ca.crt']
    k(
        'create',
        '-f',
        '-',
        input=json.dumps(
            {
                'apiVersion': 'v1',
                'kind': 'Secret',
                'metadata': {'name': 'odoo-ha-ca', 'namespace': namespace},
                'type': 'Opaque',
                'data': {'ca.crt': ca},
            }
        ).encode(),
    )
    apply(
        {
            'apiVersion': 'networking.k8s.io/v1',
            'kind': 'NetworkPolicy',
            'metadata': {'name': 'erp-restore-access', 'namespace': 'postgres-ha'},
            'spec': {
                'podSelector': {'matchLabels': {'postgres-operator.crunchydata.com/cluster': name}},
                'policyTypes': ['Ingress'],
                'ingress': [
                    {
                        'from': [
                            {
                                'namespaceSelector': {
                                    'matchLabels': {
                                        'kubernetes.io/metadata.name': namespace,
                                        'odoo.io/restore-test': 'true',
                                    }
                                },
                                'podSelector': {'matchLabels': {'app.kubernetes.io/name': 'odoo'}},
                            }
                        ],
                        'ports': [{'port': 5432}],
                    }
                ],
            },
        }
    )
    pod = primary(name)
    module_spec = importlib.util.spec_from_file_location(
        'restore_archive', ROOT / 'scripts/restore-archive.py'
    )
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    extensions = module.restore_archive(
        lambda args, **kwargs: k(
            '-n', 'postgres-ha', 'exec', '-i', pod, '-c', 'database', '--', *args, **kwargs
        ),
        (backup / 'database.dump').read_bytes(),
    )
    assert (
        sql(
            "SELECT count(*) FROM pg_tables WHERE schemaname='public' AND tableowner<>'odoo'",
            database='odoo_dev',
            cluster=name,
        )
        == '0'
    )
    values = yaml.safe_load((ROOT / 'gitops/environments/dev/odoo.yaml').read_text())
    if values['database'].get('credentialsMode') == 'rotating':
        import base64
        import secrets
        import secrets_admin as admin

        admin.bootstrap_control(name, namespace=namespace)
        generation = secrets.token_hex(16)
        fresh_password = secrets.token_urlsafe(36)
        meta = json.loads(
            admin.sql(
                "SELECT secret_control.issue('odoo_login_a','"
                + generation
                + "','"
                + admin.w.scram_verifier(fresh_password)
                + "')",
                admin.CONTROL,
                name,
            )
        )
        bundle = {
            'username': 'odoo_login_a',
            'password': fresh_password,
            'generation': generation,
            'issued_at': meta['issued_at'],
            'expires_at': meta['expires_at'],
        }
        target = obj(namespace, 'secret', 'odoo-database')
        target['data'] = {
            key: base64.b64encode(value.encode()).decode() for key, value in bundle.items()
        }
        target['data']['credentials.json'] = base64.b64encode(json.dumps(bundle).encode()).decode()
        target['metadata'].pop('annotations', None)
        k('replace', '-f', '-', input=json.dumps(target).encode())
        sql('ALTER ROLE odoo NOLOGIN', database='postgres', cluster=name)
    values['database']['host'] = name + '-primary.postgres-ha.svc'
    values['database']['existingSecret'] = 'odoo-database'
    values['database']['tls']['caSecret'] = 'odoo-ha-ca'
    values['database']['networkPeer']['podSelector']['matchLabels'] = {
        'postgres-operator.crunchydata.com/cluster': name
    }
    values['initialization'] = {'enabled': False}
    values['cronThreads'] = 0
    values.update(
        application={'mode': 'single'},
        replicaCount=1,
        workers=0,
        proxyMode=False,
        cron={'enabled': False},
        storage={'existingClaim': '', 'accessModes': ['ReadWriteOnce']},
    )
    path = ROOT / '.state/erp-restore-values.yaml'
    path.write_text(yaml.safe_dump(values, sort_keys=False))
    rendered = run(
        [
            str(ROOT / '.tools/bin/helm'),
            'template',
            'restore-odoo',
            str(ROOT / 'helm/odoo'),
            '-n',
            namespace,
            '-f',
            str(path),
        ]
    )
    documents = list(yaml.safe_load_all(rendered))
    claims = [d for d in documents if d and d['kind'] == 'PersistentVolumeClaim']
    for claim in claims:
        claim['metadata']['namespace'] = namespace
        apply(claim)
    helper = json.loads(
        run(
            [
                sys.executable,
                str(ROOT / 'scripts/volume-helper.py'),
                namespace,
                'restore-filestore',
                'odoo-data',
            ]
        )
    )
    apply(helper)
    k(
        '-n',
        namespace,
        'wait',
        '--for=condition=Ready',
        'pod/restore-filestore',
        '--timeout=120s',
        timeout=130,
    )
    k(
        '-n',
        namespace,
        'exec',
        '-i',
        'restore-filestore',
        '--',
        'tar',
        '-C',
        '/data',
        '-xzf',
        '-',
        input=(backup / 'filestore.tar.gz').read_bytes(),
    )
    k(
        '-n',
        namespace,
        'delete',
        'pod',
        'restore-filestore',
        '--wait=true',
        '--timeout=30s',
        timeout=35,
    )
    k('-n', namespace, 'apply', '-f', '-', input=rendered)
    k('-n', namespace, 'rollout', 'status', 'deployment/odoo', '--timeout=90s', timeout=100)
    environment = __import__('os').environ.copy()
    environment['TEST_NAMESPACE'] = namespace
    subprocess.run(
        [sys.executable, str(ROOT / 'tests/application/check.py')],
        env=environment,
        check=True,
        timeout=90,
    )
    assert (ROOT / 'evidence/application-odoo-dev.json.snapshot').read_bytes() == (
        ROOT / 'evidence/application-odoo-restore-test.json.snapshot'
    ).read_bytes()
    if __import__('os').environ.get('ERP_RESTORE_MODULE_TEST') == '1':
        k('-n', namespace, 'scale', 'deployment/odoo', '--replicas=0')
        wait(
            lambda: (
                not json.loads(
                    k(
                        '-n',
                        namespace,
                        'get',
                        'pods',
                        '-l',
                        'odoo.io/component=server',
                        '-o',
                        'json',
                    )
                )['items']
            ),
            timeout=90,
        )
        module_env = environment.copy()
        module_env.update(MODULE_NAMESPACE=namespace, MODULES='crm', MODULE_ACTION='upgrade')
        job = json.loads(
            subprocess.check_output(
                [sys.executable, str(ROOT / 'scripts/module-job.py')], env=module_env
            )
        )
        assert len(job['spec']['template']['spec']['containers']) == 1
        apply(job)
        k(
            '-n',
            namespace,
            'wait',
            '--for=condition=complete',
            'job/odoo-module-upgrade',
            '--timeout=180s',
            timeout=190,
        )
        k(
            '-n',
            namespace,
            'delete',
            'job/odoo-module-upgrade',
            '--wait=true',
            '--timeout=60s',
            timeout=70,
        )
        k('-n', namespace, 'scale', 'deployment/odoo', '--replicas=1')
        k('-n', namespace, 'rollout', 'status', 'deployment/odoo', '--timeout=90s', timeout=100)
        subprocess.run(
            [sys.executable, str(ROOT / 'tests/application/check.py')],
            env=environment,
            check=True,
            timeout=90,
        )
        assert (ROOT / 'evidence/application-odoo-dev.json.snapshot').read_bytes() == (
            ROOT / 'evidence/application-odoo-restore-test.json.snapshot'
        ).read_bytes()
        evidence(
            'module-lifecycle',
            isolated=True,
            module='crm',
            job_completed=True,
            fixture_ids_and_attachments_preserved=True,
        )
    if __import__('os').environ.get('ERP_RESTORE_ODOO_HA_REHEARSAL') == '1':
        spec = importlib.util.spec_from_file_location('odoo_ha', ROOT / 'scripts/odoo-ha.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.isolated_rehearsal(values, namespace)
    evidence(
        'erp-restore',
        backup=str(backup),
        backend='crunchy',
        isolated=True,
        extensions=extensions,
        scope='Database dump and matching filestore; authenticated Odoo and attachment roundtrip',
        application_objects_owner='odoo',
    )
finally:
    cleanup()
