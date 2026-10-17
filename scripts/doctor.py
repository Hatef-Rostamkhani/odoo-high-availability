#!/usr/bin/env python3
import concurrent.futures
import datetime
import ipaddress
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import urllib.parse

ROOT = Path(__file__).resolve().parents[1]


def command(args):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=20)
        return {
            'command': args,
            'exit': result.returncode,
            'output': (result.stdout + result.stderr).strip(),
        }
    except (OSError, subprocess.TimeoutExpired) as error:
        return {'command': args, 'exit': 127, 'output': str(error)}


def assess():
    report = {
        'utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'host': platform.platform(),
    }
    report['tools'] = {
        name: shutil.which(name)
        for name in (
            'docker',
            'podman',
            'kubectl',
            'helm',
            'kind',
            'k9s',
            'cilium',
            'hubble',
            'argocd',
            'make',
            'shellcheck',
            'skopeo',
            'headlamp',
        )
    }
    report['proxy'] = {}
    for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
        if key in os.environ:
            proxy = urllib.parse.urlsplit(os.environ[key])
            report['proxy'][key] = {
                'scheme': proxy.scheme,
                'hostname': proxy.hostname,
                'port': proxy.port,
                'credentials_present': bool(proxy.username or proxy.password),
            }
    report['checks'] = [
        command(c)
        for c in [
            ['cat', '/etc/os-release'],
            ['uname', '-r'],
            ['uname', '-m'],
            ['lscpu'],
            ['free', '-h'],
            ['df', '-h', str(ROOT)],
            ['ip', '-brief', 'address'],
            ['ip', '-j', 'route'],
            ['ss', '-ltn'],
            ['sudo', '-n', 'true'],
        ]
    ]
    if report['tools']['docker']:
        for args in [
            ['docker', 'version'],
            ['docker', 'info', '--format', '{{json .}}'],
            ['docker', 'network', 'ls'],
            ['docker', 'context', 'ls'],
            ['docker', 'ps', '-a', '--format', '{{.Names}} {{.Labels}}'],
        ]:
            report['checks'].append(command(args))
        network_ids = command(['docker', 'network', 'ls', '-q'])
        if network_ids['exit'] == 0 and network_ids['output']:
            report['checks'].append(
                command(['docker', 'network', 'inspect', *network_ids['output'].splitlines()])
            )
    if report['tools']['kind']:
        report['checks'].append(command(['kind', 'get', 'clusters']))
    existing = Path.home() / '.kube/config'
    if existing.exists() and report['tools']['kubectl']:
        report['checks'].append(
            command(['kubectl', '--kubeconfig', str(existing), 'config', 'get-contexts'])
        )
    urls = [
        'https://download.docker.com/linux/ubuntu/dists/noble/Release',
        'https://registry-1.docker.io/v2/',
        'https://quay.io/v2/',
        'https://ghcr.io/v2/',
        'https://registry.k8s.io/v2/',
    ]
    report['registry_checks'] = list(
        concurrent.futures.ThreadPoolExecutor(max_workers=5).map(
            lambda url: command(
                [
                    'curl',
                    '-sS',
                    '-L',
                    '-o',
                    '/dev/null',
                    '--connect-timeout',
                    '5',
                    '--max-time',
                    '15',
                    '-w',
                    '%{http_code}',
                    url,
                ]
            ),
            urls,
        )
    )
    free = shutil.disk_usage(ROOT).free
    mem = dict(line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())
    available = int(mem['MemAvailable'].split()[0]) * 1024
    report['budget'] = {
        'cpu': os.cpu_count(),
        'available_memory_bytes': available,
        'free_disk_bytes': free,
        'adequate': available >= 12 * 1024**3 and free >= 35 * 1024**3,
    }
    directory = ROOT / 'evidence'
    directory.mkdir(exist_ok=True)
    (directory / 'assessment.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
    print(
        '\nFull stack resource gate:',
        'PASS'
        if report['budget']['adequate']
        else 'BLOCKED: use reduced monitoring/one-worker profile after review',
    )
    print('No existing cluster or host configuration was changed.')
    return 0 if report['budget']['adequate'] else 1


if __name__ == '__main__':
    raise SystemExit(assess())
