#!/usr/bin/env python3
"""Prepare extensions as postgres; restore application objects with the odoo role."""

import contextlib
from pathlib import Path
import re
import subprocess
import sys

from database import K, active


def instrumentation_policy(execute, database='odoo_dev'):
    query = """WITH e AS (SELECT oid FROM pg_extension WHERE extname='pgaudit'),
    members AS (SELECT classid,objid FROM pg_depend,e WHERE refclassid='pg_extension'::regclass AND refobjid=e.oid AND deptype='e')
    SELECT json_build_object('pgaudit',json_build_object(
      'present',EXISTS(SELECT 1 FROM e),
      'application_dependencies',(SELECT count(*) FROM pg_depend d JOIN members m ON d.refclassid=m.classid AND d.refobjid=m.objid WHERE d.deptype='n' AND NOT EXISTS(SELECT 1 FROM members own WHERE own.classid=d.classid AND own.objid=d.objid)),
      'data_relations',(SELECT count(*) FROM pg_class c JOIN members m ON m.classid='pg_class'::regclass AND m.objid=c.oid WHERE c.relkind IN ('r','p','S'))))"""
    import json

    return json.loads(
        execute(['psql', '-X', '-U', 'postgres', '-d', database, '-Atc', query]).decode()
    )


def permitted_omissions(manifest):
    audit = manifest.get('operator_instrumentation', {}).get('pgaudit', {})
    if (
        audit.get('present') is True
        and audit.get('application_dependencies') == 0
        and audit.get('data_relations') == 0
    ):
        return {'pgaudit'}
    return set()


def restore_archive(execute, data, database='odoo_dev', omittable_extensions=None):
    assert database in ('odoo_dev', 'ha_rehearsal')
    directory = execute(['mktemp', '-d', '/tmp/erp-restore.XXXXXXXX']).decode().strip()
    assert re.fullmatch(r'/tmp/erp-restore\.[a-zA-Z0-9]+', directory)
    try:
        archive = directory + '/database.dump'
        listing = directory + '/restore.list'
        # Consume all stdin before reading the TOC: kubectl exec can hang if a
        # command exits while its large input is still being sent.
        execute(['sh', '-ceu', 'umask 077; cat > "$1"', '-', archive], input=data, timeout=60)
        toc = execute(['pg_restore', '--list', archive]).decode()
        names = []
        lines = []
        comments = []
        for line in toc.splitlines():
            fields = line.split()
            if len(fields) > 5 and fields[0].endswith(';') and fields[3] == 'EXTENSION':
                name = fields[5]
                assert re.fullmatch('[a-z_][a-z0-9_]*', name), 'Unsupported extension identifier'
                names.append(name)
                continue
            if (
                len(fields) > 6
                and fields[0].endswith(';')
                and fields[3:6] == ['COMMENT', '-', 'EXTENSION']
            ):
                comments.append(line)
                continue
            lines.append(line)
        allowed = set(omittable_extensions or ())
        assert allowed <= {'pgaudit'}
        values = ','.join("'" + name + "'" for name in names)
        available = (
            set(
                execute(
                    [
                        'psql',
                        '-X',
                        '-U',
                        'postgres',
                        '-d',
                        database,
                        '-Atc',
                        'SELECT name FROM pg_available_extensions WHERE name IN (' + values + ')',
                    ]
                )
                .decode()
                .splitlines()
            )
            if names
            else set()
        )
        skipped = set(names) - available
        assert skipped <= allowed, 'Target lacks required extension packages: ' + ','.join(
            sorted(skipped - allowed)
        )
        comments = [line for line in comments if line.split()[6] not in skipped]
        for name in names:
            if name in skipped:
                continue
            execute(
                [
                    'psql',
                    '-X',
                    '-v',
                    'ON_ERROR_STOP=1',
                    '-U',
                    'postgres',
                    '-d',
                    database,
                    '-c',
                    f'CREATE EXTENSION IF NOT EXISTS "{name}"',
                ]
            )
        execute(
            ['sh', '-ceu', 'umask 077; cat > "$1"', '-', listing],
            input=('\n'.join(lines) + '\n').encode(),
        )
        execute(
            [
                'pg_restore',
                '-U',
                'postgres',
                '--role=odoo',
                '-d',
                database,
                '--no-owner',
                '--no-acl',
                '--exit-on-error',
                '--use-list=' + listing,
                archive,
            ],
            timeout=120,
        )
        if comments:
            extension_comments = directory + '/extension-comments.list'
            execute(
                ['sh', '-ceu', 'umask 077; cat > "$1"', '-', extension_comments],
                input=('\n'.join(comments) + '\n').encode(),
            )
            execute(
                [
                    'pg_restore',
                    '-U',
                    'postgres',
                    '-d',
                    database,
                    '--no-owner',
                    '--no-acl',
                    '--exit-on-error',
                    '--use-list=' + extension_comments,
                    archive,
                ]
            )
        return [name for name in names if name not in skipped]
    finally:
        with contextlib.suppress(Exception):
            execute(['rm', '-rf', '--', directory])


def main():
    if sys.argv[1] == 'active':
        _, namespace, pod, container = active()
        filename = sys.argv[2]
    else:
        namespace, pod, container, filename = sys.argv[1:5]
        assert namespace == 'odoo-restore-test', (
            'Explicit targets are restricted to the owned ERP restore namespace'
        )

    def execute(args, input=None, timeout=30):
        result = subprocess.run(
            K + ['-n', namespace, 'exec', '-i', pod, '-c', container, '--', *args],
            input=input,
            capture_output=True,
            timeout=timeout,
        )
        if result.returncode:
            raise RuntimeError(
                'Archive restore operation failed: '
                + result.stderr.decode(errors='replace')[-1000:]
            )
        return result.stdout

    import json

    manifest = json.loads(Path(filename).with_name('manifest.json').read_text())
    names = restore_archive(
        execute, Path(filename).read_bytes(), omittable_extensions=permitted_omissions(manifest)
    )
    print(
        'PASS: extensions prepared administratively; application objects restored as odoo; extensions='
        + ','.join(names)
    )


if __name__ == '__main__':
    main()
