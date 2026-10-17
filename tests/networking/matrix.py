#!/usr/bin/env python3
import json
from pathlib import Path
import subprocess
import time

root = Path(__file__).resolve().parents[2]
base = [
    'kubectl',
    '--kubeconfig',
    str(root / '.state/kubeconfig'),
    '--context',
    'kind-odoo-dev',
    '-n',
    'odoo-network-test',
]


def kubectl(args, **kwargs):
    return subprocess.run(base + args, check=True, **kwargs)


def execute(pod, code):
    return kubectl(['exec', pod, '--', 'python3', '-c', code])


def allowed(pod, url):
    execute(
        pod,
        f'import urllib.request; assert urllib.request.urlopen({url!r},timeout=5).status == 200',
    )


def denied(pod, url):
    execute(
        pod,
        f"import urllib.request,sys,socket\ntry:\n urllib.request.urlopen({url!r},timeout=3)\nexcept urllib.error.HTTPError:\n sys.exit('HTTP response arrived through deny policy')\nexcept (urllib.error.URLError,TimeoutError,socket.timeout):\n sys.exit(0)\nsys.exit('Unexpected access through deny policy')",
    )


pod = json.loads(subprocess.check_output(base + ['get', 'pod', 'server', '-o', 'json'], text=True))
service = json.loads(
    subprocess.check_output(base + ['get', 'service', 'server', '-o', 'json'], text=True)
)
pod_url = 'http://' + pod['status']['podIP'] + ':8080'
service_url = 'http://' + service['spec']['clusterIP'] + ':8080'
assert pod['spec']['nodeName'] == 'odoo-dev-worker'
for podname in ('allowed', 'denied'):
    allowed(podname, pod_url)
    allowed(podname, service_url)
execute(
    'allowed',
    "import socket; assert socket.gethostbyname('server.odoo-network-test.svc.cluster.local')",
)
allowed('allowed', 'https://github.com')
print('PASS: cross-worker Pod IP, Service IP, DNS, and external HTTPS')
default = {
    'apiVersion': 'networking.k8s.io/v1',
    'kind': 'NetworkPolicy',
    'metadata': {'name': 'default-deny'},
    'spec': {'podSelector': {}, 'policyTypes': ['Ingress', 'Egress']},
}
kubectl(['apply', '-f', '-'], input=json.dumps(default), text=True)
time.sleep(5)
denied('allowed', pod_url)
denied('denied', service_url)
print('PASS: default-deny blocks previously working transport')
server_policy = {
    'apiVersion': 'networking.k8s.io/v1',
    'kind': 'NetworkPolicy',
    'metadata': {'name': 'server-allow'},
    'spec': {
        'podSelector': {'matchLabels': {'role': 'server'}},
        'policyTypes': ['Ingress'],
        'ingress': [
            {
                'from': [{'podSelector': {'matchLabels': {'role': 'allowed'}}}],
                'ports': [{'protocol': 'TCP', 'port': 8080}],
            }
        ],
    },
}
client_policy = {
    'apiVersion': 'networking.k8s.io/v1',
    'kind': 'NetworkPolicy',
    'metadata': {'name': 'client-egress'},
    'spec': {
        'podSelector': {'matchLabels': {'role': 'allowed'}},
        'policyTypes': ['Egress'],
        'egress': [
            {
                'to': [{'podSelector': {'matchLabels': {'role': 'server'}}}],
                'ports': [{'protocol': 'TCP', 'port': 8080}],
            },
            {
                'to': [
                    {
                        'namespaceSelector': {
                            'matchLabels': {'kubernetes.io/metadata.name': 'kube-system'}
                        },
                        'podSelector': {'matchLabels': {'k8s-app': 'kube-dns'}},
                    }
                ],
                'ports': [{'protocol': 'UDP', 'port': 53}, {'protocol': 'TCP', 'port': 53}],
            },
        ],
    },
}
for policy in (server_policy, client_policy):
    kubectl(['apply', '-f', '-'], input=json.dumps(policy), text=True)
time.sleep(5)
allowed('allowed', pod_url)
allowed('allowed', 'http://server:8080')
denied('denied', service_url)
print('PASS: explicit allow restores only authorized communication')
# Read recent flows from each local agent without requiring public Relay exposure.
drops = 0
for node in ('odoo-dev-worker', 'odoo-dev-worker2'):
    selector = 'k8s-app=cilium'
    agent = json.loads(
        subprocess.check_output(
            base[:-2]
            + [
                '-n',
                'kube-system',
                'get',
                'pods',
                '-l',
                selector,
                '--field-selector',
                'spec.nodeName=' + node,
                '-o',
                'json',
            ],
            text=True,
        )
    )['items'][0]['metadata']['name']
    result = subprocess.run(
        base[:-2]
        + [
            '-n',
            'kube-system',
            'exec',
            agent,
            '-c',
            'cilium-agent',
            '--',
            'hubble',
            'observe',
            '--namespace',
            'odoo-network-test',
            '--last',
            '100',
            '-o',
            'json',
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError('Cannot collect Hubble flow evidence: ' + result.stderr)
    (root / 'evidence' / ('hubble-' + node + '.jsonl')).write_text(result.stdout)
    drops += sum(
        json.loads(line).get('flow', {}).get('verdict') == 'DROPPED'
        for line in result.stdout.splitlines()
        if line.strip()
    )
assert drops > 0, 'No policy drops were observed through Hubble'
print('PASS: Hubble recorded denied test traffic')
