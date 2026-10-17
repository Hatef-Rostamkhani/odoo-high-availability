"""Keep secrets in a mounted runtime config, and initialize only a new database."""

import configparser
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import psycopg2

signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

settings = json.loads(Path('/opt/odoo/settings.json').read_text())
mode = sys.argv[1]
credential = None
if settings.get('credentialsMode', 'static') == 'rotating':
    sys.path.insert(0, '/opt/odoo')
    from credentials import load_bundle

    credential, expiry = load_bundle('/run/database-secret')
    settings['user'] = credential['username']
    password = credential['password']
else:
    password = Path('/run/database-secret/password').read_text().strip()
admin_password = Path('/run/credentials-secret/admin-password').read_text().strip()
master_password = Path('/run/credentials-secret/master-password').read_text().strip()
configuration = configparser.ConfigParser(interpolation=None)
configuration['options'] = {
    'db_host': settings['host'],
    'db_port': str(settings['port']),
    'db_name': settings['database'],
    'db_user': settings['user'],
    'db_password': password,
    'db_sslmode': settings['sslmode'],
    'dbfilter': '^' + settings['database'] + '$',
    'list_db': 'False',
    'data_dir': settings.get('dataDir', '/var/lib/odoo'),
    'admin_passwd': master_password,
    'addons_path': '/usr/lib/python3/dist-packages/odoo/addons,/mnt/extra-addons',
    'workers': str(0 if mode in ('cron', 'configuration', 'initialize') else settings['workers']),
    'max_cron_threads': str(settings.get('cronWorkers', 1) if mode == 'cron' else settings['cron']),
    'db_maxconn': str(settings.get('cronMaxconn', 4) if mode == 'cron' else settings['maxconn']),
    'db_maxconn_gevent': str(settings.get('geventMaxconn', 4)),
    'proxy_mode': str(settings.get('proxyMode', False)),
    'http_interface': '0.0.0.0',
    'http_port': '8069',
    'gevent_port': str(settings.get('geventPort', 8072)),
    'limit_memory_soft': str(settings.get('memorySoft', 1342177280)),
    'limit_memory_hard': str(settings.get('memoryHard', 1879048192)),
}
os.umask(0o077)
config_file = '/run/odoo/odoo.conf'
with open(config_file, 'w') as output:
    configuration.write(output)

connection_args = dict(
    host=settings['host'],
    port=settings['port'],
    dbname=settings['database'],
    user=settings['user'],
    password=password,
    sslmode=settings['sslmode'],
)
for attempt in range(120):
    try:
        connection = psycopg2.connect(**connection_args, connect_timeout=5)
        break
    except psycopg2.OperationalError:
        if attempt == 119:
            raise RuntimeError(
                'Database did not become reachable within the startup budget'
            ) from None
        time.sleep(5)

if credential:
    with connection.cursor() as cursor:
        cursor.execute(
            'SELECT session_user,current_user,rolvaliduntil FROM pg_roles WHERE rolname=session_user'
        )
        row = cursor.fetchone()
        if row != (credential['username'], 'odoo', expiry):
            raise RuntimeError(
                'Database identity or credential deadline does not match the issued bundle'
            )
    Path('/run/odoo/credential.json').write_text(
        json.dumps({key: value for key, value in credential.items() if key != 'password'})
    )

if mode == 'initialize':
    with connection.cursor() as cursor:
        cursor.execute("SELECT to_regclass('public.odoo_environment_bootstrap')")
        initialized = cursor.fetchone()[0] is not None
    connection.close()
    if initialized:
        print(
            'Existing Odoo database found; initialization skipped. Use explicit module-upgrade.',
            flush=True,
        )
        sys.exit(0)
    subprocess.run(
        [
            'odoo',
            '-c',
            config_file,
            '-d',
            settings['database'],
            '-i',
            settings['modules'],
            '--without-demo=all',
            '--stop-after-init',
            '--no-http',
            '--max-cron-threads=0',
        ],
        check=True,
    )
    # Admin password and deterministic fixtures are applied before the server is deployed.
    fixture_code = """
from pathlib import Path
env.ref('base.user_admin').write({'login': 'admin', 'password': Path('/run/credentials-secret/admin-password').read_text().strip()})
env.ref('base.main_company').write({'name': 'Odoo Development'})
"""
    if settings['synthetic']:
        fixture_code += """
partner = env['res.partner'].create({'name': 'Synthetic Odoo Customer', 'email': 'customer@example.invalid'})
env['crm.lead'].create({'name': 'Synthetic pipeline opportunity', 'type': 'opportunity', 'partner_id': partner.id})
env['ir.attachment'].create({'name': 'odoo-fixture.txt', 'res_model': 'res.partner', 'res_id': partner.id, 'raw': b'Odoo synthetic attachment v1\\n', 'mimetype': 'text/plain'})
"""
    fixture_code += "\nenv.cr.execute('CREATE TABLE odoo_environment_bootstrap (version integer NOT NULL)')\nenv.cr.execute('INSERT INTO odoo_environment_bootstrap VALUES (1)')\nenv.cr.commit()\n"
    subprocess.run(
        ['odoo', 'shell', '-c', config_file, '-d', settings['database'], '--no-http'],
        input=fixture_code,
        text=True,
        check=True,
    )
    print('Database initialized; default administrator password replaced.', flush=True)
elif mode == 'serve':
    connection.close()
    os.execvp('odoo', ['odoo', '-c', config_file])
elif mode == 'cron':
    connection.close()
    os.execvp('python3', ['python3', '/opt/odoo/cron-run.py', '-c', config_file, '--no-http'])
elif mode == 'configuration':
    connection.close()
else:
    raise RuntimeError('Unknown startup mode')
