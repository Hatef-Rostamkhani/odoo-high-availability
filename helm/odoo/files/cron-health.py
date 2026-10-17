"""Check cron-only runtime and its database without restarting on DB outages."""

import configparser
from pathlib import Path
import psycopg2

assert b'--no-http' in Path('/proc/1/cmdline').read_bytes()
config = configparser.ConfigParser(interpolation=None)
config.read('/run/odoo/odoo.conf')
c = config['options']
with psycopg2.connect(
    host=c['db_host'],
    port=c['db_port'],
    dbname=c['db_name'],
    user=c['db_user'],
    password=c['db_password'],
    sslmode=c['db_sslmode'],
    connect_timeout=2,
    options='-c statement_timeout=2000',
) as connection:
    with connection.cursor() as cursor:
        cursor.execute('SELECT 1 FROM ir_cron LIMIT 1')
