#!/usr/bin/env python3
import ipaddress
import json
import subprocess

reserved = [ipaddress.ip_network(cidr) for cidr in ('10.244.0.0/16', '10.96.0.0/16')]
routes = json.loads(subprocess.check_output(['ip', '-j', 'route'], text=True))
existing = [
    ipaddress.ip_network(r['dst'], strict=False)
    for r in routes
    if r.get('dst') not in (None, 'default')
]
networks = subprocess.check_output(['docker', 'network', 'ls', '-q'], text=True).split()
if networks:
    for network in json.loads(
        subprocess.check_output(['docker', 'network', 'inspect', *networks], text=True)
    ):
        existing += [
            ipaddress.ip_network(c['Subnet'])
            for c in (network.get('IPAM', {}).get('Config') or [])
            if c.get('Subnet')
        ]
for candidate in reserved:
    for occupied in existing:
        if candidate.version == occupied.version and candidate.overlaps(occupied):
            raise SystemExit(
                f'CIDR overlap: {candidate} conflicts with {occupied}; no network changes made'
            )
print('Pod and Service CIDRs do not overlap discovered host routes or Docker networks')
