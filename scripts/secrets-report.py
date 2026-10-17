#!/usr/bin/env python3
"""Summarize actual acceptance artifacts without inferring unrun checks."""

import json
from pathlib import Path
import openbao as b

REQUIRED = [
    ('live-application-prerequisite', 'Live application prerequisites'),
    ('etcd-encryption', 'Encryption at rest'),
    ('etcd-ciphertext-openbao-openbao-seal', 'Raw etcd ciphertext'),
    ('openbao-status', 'Three-node unsealed Raft'),
    ('openbao-all-restart', 'Automatic recovery of all Bao members'),
    ('openbao-auth-tls-rbac', 'TLS and least privilege'),
    ('authenticated-quorum-recovery', 'Root recovery and revocation'),
    ('openbao-restore', 'Encrypted Raft restore on clone'),
    ('static-seal-missing-key', 'Missing seal key'),
    ('static-seal-wrong-key', 'Incorrect seal key'),
    ('isolated-two-rotations', 'A B A rotation with sessions and attachments'),
    ('isolated-hard-expiry', 'Five-second expiry guarantee'),
    ('isolated-expiry-transactions', 'Committed and open transactions'),
    ('pre-authentication-expiry', 'Expiry of handshake started before issuance'),
    ('isolated-both-guards-down', 'Expected loss of expiry guarantee'),
    ('isolated-controller-resume-issued', 'Resume after password application'),
    ('isolated-controller-resume-published', 'Resume after publication'),
    ('isolated-controller-resume-rollout_started', 'Resume during rollout'),
    ('isolated-transfer-failures', 'Failure without deadline extension'),
    ('isolated-stale-snapshot', 'Reject publication of old snapshot credential'),
    ('isolated-module-upgrade', 'Module maintenance with current login'),
    ('isolated-load-rotation', 'Ten concurrent users during rotation'),
    ('isolated-rollback', 'Fresh credential rollback rehearsal'),
    ('secrets-migration', 'Live Odoo migration'),
    ('live-application-ha', 'Live web proxy and websocket faults'),
    ('live-database-ha', 'Live PostgreSQL failover and strict sync'),
    ('live-erp-restore', 'Coordinated ERP restore on clone'),
    ('secrets-monitoring', 'Live metrics and alert evaluation'),
    ('real-24h-observation', 'Real 24-hour automatic rotation observation'),
]


def report():
    rows = []
    for name, title in REQUIRED:
        path = b.EVIDENCE / (name + '.json')
        if path.exists():
            entry = json.loads(path.read_text())
        else:
            entry = {'test': name, 'status': 'NOT RUN'}
        rows.append({'title': title, **entry})
    state = (
        'PASS'
        if all(row['status'] == 'PASS' for row in rows)
        else 'BLOCKED'
        if any(row['status'] == 'BLOCKED' for row in rows)
        else 'NOT RUN'
        if all(row['status'] in ('PASS', 'NOT RUN') for row in rows)
        else 'FAIL'
    )
    document = {'status': state, 'criteria': rows, 'final_acceptance': state == 'PASS'}
    b.EVIDENCE.mkdir(exist_ok=True, parents=True)
    (b.EVIDENCE / 'acceptance.json').write_text(json.dumps(document, indent=2) + '\n')
    (b.EVIDENCE / 'acceptance.md').write_text(
        '| Criterion | Result |\n|---|---|\n'
        + ''.join('| ' + row['title'] + ' | ' + row['status'] + ' |\n' for row in rows)
    )
    print(
        json.dumps(
            {
                'status': state,
                'final_acceptance': document['final_acceptance'],
                'counts': {
                    tag: sum(row['status'] == tag for row in rows)
                    for tag in ('PASS', 'FAIL', 'BLOCKED', 'NOT RUN')
                },
            },
            indent=2,
        )
    )
    return document


if __name__ == '__main__':
    report()
