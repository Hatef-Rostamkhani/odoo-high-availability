#!/usr/bin/env python3
"""Exercise the real public entry, retained cookies and acknowledged Odoo writes."""

import base64
import hashlib
import http.cookiejar
import importlib.util
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
spec = importlib.util.spec_from_file_location('odoo_ha', ROOT / 'scripts/odoo-ha.py')
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)
from ha import k, run, wait, maintenance

TOKENS = set()


class Client:
    def __init__(self, url='http://127.0.0.1:8069', request_timeout=5):
        self.url = url
        self.jar = http.cookiejar.CookieJar()
        self.request_timeout = request_timeout
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(self.jar)
        )
        self.uid = None

    def rpc(self, path, params, timeout=None):
        request = urllib.request.Request(
            self.url + path,
            json.dumps({'jsonrpc': '2.0', 'method': 'call', 'params': params, 'id': 1}).encode(),
            {'Content-Type': 'application/json'},
        )
        with self.opener.open(
            request, timeout=self.request_timeout if timeout is None else timeout
        ) as response:
            result = json.load(response)
        if 'error' in result:
            raise RuntimeError('Odoo RPC failed; user content omitted')
        return result['result']

    def authenticate(self):
        credentials = json.loads((ROOT / '.state/credentials.json').read_text())
        self.uid = self.rpc(
            '/web/session/authenticate',
            {'db': 'odoo_dev', 'login': 'admin', 'password': credentials['odoo-admin']},
            30,
        )['uid']
        assert self.uid
        return self

    def call(self, model, method, args, kwargs=None):
        return self.rpc(
            '/web/dataset/call_kw',
            {'model': model, 'method': method, 'args': args, 'kwargs': kwargs or {}},
        )

    def session(self):
        assert self.rpc('/web/session/get_session_info', {})['uid'] == self.uid, (
            'Existing authenticated session was lost'
        )
        return True

    def websocket(self, version):
        request = urllib.request.Request(self.url + '/websocket?version=' + version)
        self.jar.add_cookie_header(request)
        cookie = request.get_header('Cookie', '')
        key = base64.b64encode(os.urandom(16)).decode()
        port = int(self.url.rsplit(':', 1)[1])
        conn = socket.create_connection(('127.0.0.1', port), timeout=2)
        try:
            agent = dict(self.opener.addheaders).get('User-agent', 'Python-urllib/3.12')
            conn.sendall(
                (
                    f'GET /websocket?version={version} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nOrigin: {self.url}\r\nUser-Agent: {agent}\r\nConnection: Upgrade\r\nUpgrade: websocket\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: {key}\r\nCookie: {cookie}\r\n\r\n'
                ).encode()
            )
            data = b''
            while b'\r\n\r\n' not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    raise EOFError('WebSocket closed before completing the handshake')
                data += chunk
                assert len(data) < 16384, 'Oversized WebSocket response headers'
            expected = base64.b64encode(
                hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()
            ).decode()
            status = data.split(b'\r\n')[0].decode(errors='replace')
            assert b' 101 ' in data.split(b'\r\n')[0] and expected.encode() in data, (
                'WebSocket upgrade failed: ' + status
            )
            return True
        finally:
            conn.close()


def fault(component, abrupt=False, which=0):
    namespace = 'odoo-edge' if component == 'edge' else 'odoo-dev'
    pods = app.ready(namespace, component)
    assert pods
    pod = pods[which]
    name = pod['metadata']['name']
    if abrupt:
        container = next(c for c in pod['status']['containerStatuses'] if c['name'] == 'odoo')[
            'containerID'
        ].split('://', 1)[1]
        run(
            [
                'docker',
                'exec',
                pod['spec']['nodeName'],
                'ctr',
                '--namespace',
                'k8s.io',
                'tasks',
                'kill',
                '--signal',
                'SIGKILL',
                container,
            ]
        )
    else:
        k('-n', namespace, 'delete', 'pod', name, '--wait=false')
    return name


def recovery(client, operation, version=None, poll_interval=0.2, write_interval=0.25):
    token = 'odoo-ha-' + secrets.token_hex(8)
    acknowledged = []
    uncertain = []
    errors = []
    TOKENS.add(token)
    # Seed confirmed writes BEFORE the fault, then keep issuing writes across it.
    for i in range(100):
        acknowledged.append(
            client.call('res.partner', 'create', [{'name': token + '-seed-' + str(i)}])
        )
    writer = Client(client.url).authenticate()
    stop = threading.Event()

    def write():
        index = 0
        while not stop.is_set():
            try:
                acknowledged.append(
                    writer.call('res.partner', 'create', [{'name': token + '-live-' + str(index)}])
                )
            except Exception:
                uncertain.append(index)
            index += 1
            stop.wait(write_interval)

    thread = threading.Thread(target=write, daemon=True)
    thread.start()
    started = time.monotonic()
    stable_start = None
    try:
        operation()
        while time.monotonic() - started < 50:
            try:
                client.session()
                if version:
                    client.websocket(version)
                if stable_start is None:
                    stable_start = time.monotonic()
                if time.monotonic() - stable_start >= 30:
                    break
            except Exception as error:
                errors.append(
                    {
                        'seconds': round(time.monotonic() - started, 3),
                        'error_type': type(error).__name__,
                    }
                )
                stable_start = None
            time.sleep(poll_interval)
        stop.set()
        thread.join(timeout=10)
        assert not thread.is_alive()
        assert stable_start is not None and time.monotonic() - stable_start >= 30, (
            'No 30-second stable recovery window'
        )
        seconds = stable_start - started
        if seconds > 10:
            app.evidence(
                'recovery-failure',
                status='FAIL',
                seconds=round(seconds, 3),
                errors=errors,
                poll_interval=poll_interval,
                write_interval=write_interval,
            )
        assert seconds <= 10, f'Instance recovery took {seconds:.3f}s'
        rows = client.call(
            'res.partner',
            'search_read',
            [[['name', '=like', token + '%']]],
            {'fields': ['id', 'name']},
        )
        assert set(acknowledged) <= {r['id'] for r in rows}
    finally:
        stop.set()
        thread.join(timeout=10)
        ids = client.call('res.partner', 'search', [[['name', '=like', token + '%']]])
        if ids:
            client.call('res.partner', 'unlink', [ids])
    return {
        'seconds': round(seconds, 3),
        'stable_seconds': 30,
        'errors': errors,
        'acknowledged_preserved': len(acknowledged),
        'unacknowledged_attempts': uncertain,
    }


def cron_test(client):
    token = 'odoo-cron-' + secrets.token_hex(6)
    TOKENS.add(token)
    model_id = client.call('ir.model', 'search', [[['model', '=', 'ir.config_parameter']]])[0]
    # One due execution; PostgreSQL's scheduler lock serializes both cron processes.
    code = (
        "env['ir.config_parameter'].sudo().set_param('"
        + token
        + "', str(int(env['ir.config_parameter'].sudo().get_param('"
        + token
        + "', '0')) + 1)); env.cr.execute('SELECT host(inet_client_addr())'); env['ir.config_parameter'].sudo().set_param('"
        + token
        + ".pod_ip', env.cr.fetchone()[0]); env['ir.cron']._commit_progress(1, remaining=0, deactivate=True)"
    )
    identifier = client.call(
        'ir.cron',
        'create',
        [
            {
                'name': token,
                'model_id': model_id,
                'state': 'code',
                'code': code,
                'interval_number': 1,
                'interval_type': 'minutes',
                'nextcall': time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(time.time() + 86400)),
            }
        ],
    )
    try:
        # With two schedulers alive, wake both for the same due job.
        client.call(
            'ir.cron',
            'write',
            [
                [identifier],
                {'nextcall': time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(time.time() - 60))},
            ],
        )
        app.ha.sql("SELECT pg_notify('cron_trigger','odoo_dev')", database='postgres')
        wait(
            lambda: client.call('ir.config_parameter', 'get_param', [token, '0']) == '1',
            timeout=90,
            interval=1,
        )
        time.sleep(3)
        assert client.call('ir.config_parameter', 'get_param', [token, '0']) == '1'
        client.call('ir.config_parameter', 'set_param', [token, '0'])
        # Rearm in the future before the fault, then wake the surviving scheduler.
        client.call(
            'ir.cron',
            'write',
            [
                [identifier],
                {
                    'active': True,
                    'nextcall': time.strftime(
                        '%Y-%m-%d %H:%M:%S', time.gmtime(time.time() + 86400)
                    ),
                },
            ],
        )
        original_pods = app.ready('odoo-dev', 'cron')
        original_ips = {p['status']['podIP'] for p in original_pods}
        started = time.monotonic()
        # Hold the replacement out of this test so only an existing scheduler
        # can demonstrate recovery; respect the same saved-replica cleanup.
        k('-n', 'odoo-dev', 'scale', 'deployment/odoo-cron', '--replicas=1')
        live = wait(lambda: app.ready('odoo-dev', 'cron', 1), timeout=30, interval=0.5)
        survivor = live[0]['status']['podIP']
        assert survivor in original_ips
        removed = next(
            p['metadata']['name'] for p in original_pods if p['status']['podIP'] != survivor
        )
        k('-n', 'odoo-dev', 'wait', '--for=delete', 'pod/' + removed, '--timeout=30s', timeout=35)
        client.call(
            'ir.cron',
            'write',
            [
                [identifier],
                {'nextcall': time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(time.time() - 60))},
            ],
        )
        app.ha.sql("SELECT pg_notify('cron_trigger','odoo_dev')", database='postgres')
        wait(
            lambda: client.call('ir.config_parameter', 'get_param', [token, '0']) == '1',
            timeout=90,
            interval=1,
        )
        seconds = time.monotonic() - started
        time.sleep(3)
        assert client.call('ir.config_parameter', 'get_param', [token, '0']) == '1', (
            'Cron job executed more than once'
        )
        actual = client.call('ir.config_parameter', 'get_param', [token + '.pod_ip', ''])
        assert actual == survivor, (
            f'The surviving cron instance did not execute the job: SQL client {actual}, pod {survivor}'
        )
        app.evidence(
            'cron',
            seconds=round(seconds, 3),
            concurrent_execution=False,
            surviving_instance_executed=True,
            fault='One cron replica removed; replacement held until verification',
        )
    finally:
        k('-n', 'odoo-dev', 'scale', 'deployment/odoo-cron', '--replicas=2')
        client.call('ir.cron', 'unlink', [[identifier]])
        ids = client.call('ir.config_parameter', 'search', [[['key', '=like', token + '%']]])
        if ids:
            client.call('ir.config_parameter', 'unlink', [ids])
        wait(lambda: app.ready('odoo-dev', 'cron'), timeout=180)


def load_test(version):
    duration = int(os.environ.get('ODOO_HA_LOAD_SECONDS', '600'))
    assert duration >= 600, 'Acceptance load requires ten minutes'
    clients = [Client(request_timeout=30).authenticate() for _ in range(10)]
    token = 'odoo-load-' + secrets.token_hex(6)
    stop = threading.Event()
    lock = threading.Lock()
    TOKENS.add(token)
    ledger = []
    lead_ledger = []
    errors = []
    latencies = []
    started = time.monotonic()

    def worker(index, client):
        while not stop.is_set():
            tick = time.monotonic()
            identifier = None
            attachment = None
            try:
                client.session()
                identifier = client.call(
                    'res.partner', 'create', [{'name': token + '-' + str(index)}]
                )
                with lock:
                    ledger.append(identifier)
                lead = client.call(
                    'crm.lead',
                    'create',
                    [
                        {
                            'name': token + '-' + str(index),
                            'type': 'opportunity',
                            'partner_id': identifier,
                        }
                    ],
                )
                with lock:
                    lead_ledger.append(lead)
                assert (
                    client.call('crm.lead', 'read', [[lead]], {'fields': ['partner_id']})[0][
                        'partner_id'
                    ][0]
                    == identifier
                )
                attachment = client.call(
                    'ir.attachment',
                    'create',
                    [
                        {
                            'name': token,
                            'res_model': 'res.partner',
                            'res_id': identifier,
                            'datas': base64.b64encode(b'HA load attachment').decode(),
                        }
                    ],
                )
                with client.opener.open(
                    client.url + '/web/content/' + str(attachment) + '?download=true', timeout=30
                ) as response:
                    assert response.read() == b'HA load attachment'
                client.call('ir.attachment', 'unlink', [[attachment]])
                with lock:
                    latencies.append(time.monotonic() - tick)
            except Exception as error:
                with lock:
                    errors.append(
                        {
                            'seconds': round(time.monotonic() - started, 3),
                            'started_seconds': round(tick - started, 3),
                            'type': type(error).__name__,
                            'http_status': getattr(error, 'code', None),
                        }
                    )
            stop.wait(2)

    threads = [
        threading.Thread(target=worker, args=(i, c), daemon=True) for i, c in enumerate(clients)
    ]
    for t in threads:
        t.start()
    try:
        while time.monotonic() - started < 60:
            time.sleep(1)
        fault_context = {}

        def induce():
            fault_context['at'] = time.monotonic() - started
            return fault('server', True)

        # The ten business users define the load. Keep the extra observer/writer
        # at 1 Hz instead of adding a second stress workload to the same workers.
        result = recovery(
            Client().authenticate(), induce, version, poll_interval=1, write_interval=1
        )
        app.evidence('load-fault', **result)
        while time.monotonic() - started < duration:
            time.sleep(1)
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=15)
    client = clients[0]
    client.session()
    rows = client.call('res.partner', 'search', [[['name', '=like', token + '%']]])
    leads = client.call('crm.lead', 'search', [[['name', '=like', token + '%']]])
    preserved = set(ledger) <= set(rows) and set(lead_ledger) <= set(leads)
    fault_at = fault_context['at']
    unexpected = [e for e in errors if not fault_at - 5 <= e['started_seconds'] <= fault_at + 10]
    try:
        pods = app.ready('odoo-dev', 'server')
        assert pods
        oom = any(
            c.get('lastState', {}).get('terminated', {}).get('reason') == 'OOMKilled'
            for p in pods
            for c in p['status']['containerStatuses']
        )
        app.evidence(
            'load',
            status='PASS' if preserved and not unexpected and ledger and not oom else 'FAIL',
            seconds=round(time.monotonic() - started, 3),
            users=10,
            acknowledged_preserved=len(ledger),
            crm_opportunities_preserved=len(lead_ledger),
            errors=errors,
            unexpected_errors=unexpected,
            fault_seconds=round(fault_at, 3),
            oom=oom,
            max_operation_seconds=round(max(latencies, default=0), 3),
        )
        assert not unexpected, 'Load errors outside the induced-fault window'
        assert preserved, 'Acknowledged Odoo records were lost'
        assert ledger, 'Load produced no acknowledged writes'
        assert not oom, 'A web container was OOM-killed'
    finally:
        attachments = client.call('ir.attachment', 'search', [[['name', '=', token]]])
        if attachments:
            client.call('ir.attachment', 'unlink', [attachments])
        if leads:
            client.call('crm.lead', 'unlink', [leads])
        if rows:
            client.call('res.partner', 'unlink', [rows])


def main():
    client = Client().authenticate()
    pod = app.ready('odoo-dev', 'server')[0]['metadata']['name']
    version = (
        k(
            '-n',
            'odoo-dev',
            'exec',
            pod,
            '--',
            'python3',
            '-c',
            'from odoo.addons.bus.websocket import WebsocketConnectionHandler; print(WebsocketConnectionHandler._VERSION)',
        )
        .decode()
        .strip()
    )
    client.websocket(version)
    app.evidence('websocket', upgrade=True, version=version)
    for name, component, abrupt, which in [
        ('web-delete', 'server', False, 0),
        ('web-kill', 'server', True, 0),
        ('edge-delete-0', 'edge', False, 0),
        ('edge-delete-1', 'edge', False, 1),
    ]:
        with maintenance():
            result = recovery(client, lambda: fault(component, abrupt, which), version)
            wait(
                lambda: app.ready('odoo-edge' if component == 'edge' else 'odoo-dev', component),
                timeout=180,
            )
            namespace = 'odoo-edge' if component == 'edge' else 'odoo-dev'
            selector = 'app=odoo-edge' if component == 'edge' else 'odoo.io/component=' + component
            wait(
                lambda: (
                    len(
                        json.loads(k('-n', namespace, 'get', 'pods', '-l', selector, '-o', 'json'))[
                            'items'
                        ]
                    )
                    == 2
                ),
                timeout=180,
            )
            app.evidence(name, retained_session=True, **result)
    with maintenance():
        cron_test(client)
    with maintenance():
        load_test(version)
    subprocess.run(['bash', str(ROOT / 'scripts/ha.sh'), 'test'], check=True, timeout=300)
    client.session()
    client.websocket(version)
    app.evidence('database-reconnect', retained_session=True)
    started = time.monotonic()
    ensure_heartbeat(client, newer_than=time.time())
    app.evidence(
        'cron-database-reconnect',
        seconds=round(time.monotonic() - started, 3),
        scheduled_job_executed=True,
    )
    app.status()


def ensure_heartbeat(client, newer_than=None):
    name = 'Odoo HA heartbeat'
    ids = client.call('ir.cron', 'search', [[['name', '=', name]]])
    # Telemetry must not invalidate every worker's configuration/asset cache each minute.
    code = 'env.cr.execute("INSERT INTO ir_config_parameter (key,value) VALUES (\'odoo.ha.cron_heartbeat\',extract(epoch from clock_timestamp())::text) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value")'
    if not ids:
        model = client.call('ir.model', 'search', [[['model', '=', 'ir.config_parameter']]])[0]
        client.call(
            'ir.cron',
            'create',
            [
                {
                    'name': name,
                    'model_id': model,
                    'state': 'code',
                    'code': code,
                    'interval_number': 1,
                    'interval_type': 'minutes',
                    'nextcall': time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(time.time() - 60)),
                }
            ],
        )
    elif client.call('ir.cron', 'read', [ids], {'fields': ['code']})[0]['code'] != code:
        client.call('ir.cron', 'write', [ids, {'code': code}])
    app.ha.sql("SELECT pg_notify('cron_trigger','odoo_dev')", database='postgres')
    threshold = time.time() - 120 if newer_than is None else newer_than
    wait(
        lambda: (
            float(
                app.ha.sql(
                    "SELECT value FROM ir_config_parameter WHERE key='odoo.ha.cron_heartbeat'",
                    database='odoo_dev',
                )
                or 0
            )
            > threshold
        ),
        timeout=90,
    )


def cleanup():
    client = Client().authenticate()
    for token in TOKENS:
        for model in ('ir.attachment', 'crm.lead', 'res.partner', 'ir.cron'):
            ids = client.call(model, 'search', [[['name', '=like', token + '%']]])
            if ids:
                client.call(model, 'unlink', [ids])
        ids = client.call('ir.config_parameter', 'search', [[['key', '=like', token + '%']]])
        if ids:
            client.call('ir.config_parameter', 'unlink', [ids])
    for namespace, component in [
        ('odoo-dev', 'server'),
        ('odoo-dev', 'cron'),
        ('odoo-edge', 'edge'),
    ]:
        wait(lambda: app.ready(namespace, component), timeout=180)


if __name__ == '__main__':

    def interrupted(signum, frame):
        raise InterruptedError('Odoo HA test interrupted')

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, interrupted)
    try:
        ensure_heartbeat(Client().authenticate())
        main()
    finally:
        cleanup()
