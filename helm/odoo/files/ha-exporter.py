"""Export only aggregate storage/session/cron health; never session contents."""

import configparser
import contextlib
import http.server
import json
import os
from pathlib import Path
import signal
import sys
import threading
import time
import psycopg2

signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
database_lock = threading.Lock()
health = (False, 0)
authentication_failures = 0


def options():
    config = configparser.ConfigParser(interpolation=None)
    config.read('/run/odoo/odoo.conf')
    return config['options']


def connect(c):
    global authentication_failures
    try:
        return psycopg2.connect(
            host=c['db_host'],
            port=c['db_port'],
            dbname=c['db_name'],
            user=c['db_user'],
            password=c['db_password'],
            sslmode=c['db_sslmode'],
            connect_timeout=1,
            options='-c statement_timeout=1000',
        )
    except psycopg2.OperationalError as error:
        if error.pgcode in ('28P01', '28000') or 'password authentication failed' in str(error):
            authentication_failures += 1
        raise


def live_ports():
    # Inspect this pod's network namespace; do not add connections to busy queues.
    listening = set()
    for name in ('tcp', 'tcp6'):
        for row in Path('/proc/net/' + name).read_text().splitlines()[1:]:
            fields = row.split()
            if fields[3] == '0A':
                listening.add(int(fields[1].split(':')[-1], 16))
    return {8069, 8072} <= listening


def health_loop():
    global health
    while True:
        ok = False
        try:
            c = options()
            if c['workers'] == '2' and live_ports():
                # One connection across health and metrics, including during surge.
                with database_lock, contextlib.closing(connect(c)) as connection:
                    with connection.cursor() as cursor:
                        cursor.execute('SELECT 1')
                        cursor.fetchone()
                ok = True
        except (OSError, KeyError, psycopg2.Error):
            pass
        health = (ok, time.monotonic())
        time.sleep(1)


threading.Thread(target=health_loop, daemon=True).start()


class Exporter(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/identity':
            try:
                body = Path('/run/odoo/credential.json').read_bytes()
                identity = json.loads(body)
                if 'password' in identity:
                    raise ValueError('Unsafe identity payload')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(body)
            except (OSError, ValueError):
                self.send_error(503)
            return
        if self.path == '/live':
            self.send_response(200 if live_ports() else 503)
            self.end_headers()
            return
        if self.path == '/ready':
            ok, sampled = health
            self.send_response(200 if ok and time.monotonic() - sampled < 3 else 503)
            self.end_headers()
            return
        if self.path != '/metrics':
            self.send_error(404)
            return
        metrics = {
            'odoo_odoo_cron_heartbeat_timestamp_seconds': 0,
            'odoo_odoo_database_authentication_failures_total': authentication_failures,
            'odoo_odoo_ha_exporter_database_up': 0,
        }
        try:
            c = options()
            data_dir = c['data_dir']
            stat = os.statvfs(data_dir)
            metrics['odoo_odoo_shared_storage_free_bytes'] = stat.f_bavail * stat.f_frsize
            metrics['odoo_odoo_session_files'] = sum(
                p.is_file() for p in (Path(data_dir) / 'sessions').rglob('*')
            )
            with database_lock, contextlib.closing(connect(c)) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT value FROM ir_config_parameter WHERE key='odoo.ha.cron_heartbeat'"
                    )
                    row = cursor.fetchone()
                    if row:
                        metrics['odoo_odoo_cron_heartbeat_timestamp_seconds'] = float(row[0])
                    metrics['odoo_odoo_ha_exporter_database_up'] = 1
        except (OSError, ValueError, KeyError, psycopg2.Error):
            pass
        content = ''.join(
            f'# TYPE {key} '
            + ('counter' if key.endswith('_total') else 'gauge')
            + f'\n{key} {value}\n'
            for key, value in metrics.items()
        ).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain; version=0.0.4')
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, *args):
        pass


http.server.ThreadingHTTPServer(('0.0.0.0', 9116), Exporter).serve_forever()
