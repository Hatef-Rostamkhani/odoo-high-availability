#!/usr/bin/env python3
"""Regenerate embedded ConfigMap code from the canonical Python files."""

import argparse
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SOURCES = {
    'worker.py': ROOT / 'infrastructure/secrets/worker.py',
    'snapshot.py': ROOT / 'infrastructure/secrets/snapshot.py',
}
TARGETS = (
    'infrastructure/secrets/runtime.yaml',
    'gitops/platform/secrets-system/runtime.yaml',
    'infrastructure/secrets/snapshot-job.yaml',
    'gitops/platform/openbao/snapshot.yaml',
)


class ReadableDumper(yaml.SafeDumper):
    pass


def readable_string(dumper, value):
    return dumper.represent_scalar(
        'tag:yaml.org,2002:str', value, style='|' if '\n' in value else None
    )


ReadableDumper.add_representer(str, readable_string)


def synchronize(check=False):
    differences = []
    for target in TARGETS:
        path = ROOT / target
        documents = list(yaml.safe_load_all(path.read_text()))
        for document in documents:
            if document and document.get('kind') == 'ConfigMap':
                for key, source in SOURCES.items():
                    if key in document.get('data', {}):
                        document['data'][key] = source.read_text()
        rendered = yaml.dump_all(documents, Dumper=ReadableDumper, sort_keys=False, width=100)
        if path.read_text() != rendered:
            differences.append(target)
            if not check:
                path.write_text(rendered)
    if check and differences:
        raise SystemExit(
            'Generated manifests are stale; run make render-runtime: ' + ', '.join(differences)
        )
    print('PASS: runtime ConfigMaps match their canonical Python source')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    synchronize(parser.parse_args().check)
