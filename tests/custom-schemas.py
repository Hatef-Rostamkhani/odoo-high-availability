#!/usr/bin/env python3
"""Validate custom objects with pinned upstream OpenAPI schemas, not missing-schema skips."""

import json
from pathlib import Path
import tarfile
import subprocess
import yaml

yaml.SafeLoader.add_constructor('tag:yaml.org,2002:value', yaml.SafeLoader.construct_scalar)

root = Path(__file__).resolve().parents[1]
schemas = root / '.cache/custom-schemas'
schemas.mkdir(parents=True, exist_ok=True)
crds = []
for rendered in (root / '.state/rendered').glob('*.yaml'):
    crds.extend(
        d
        for d in yaml.safe_load_all(rendered.read_text())
        if d and d.get('kind') == 'CustomResourceDefinition'
    )
for archive in (root / 'vendor').glob('*.tgz'):
    with tarfile.open(archive) as tf:
        for member in tf.getmembers():
            if (
                '/crds/' in member.name
                and member.isfile()
                and member.name.endswith(('.yaml', '.yml'))
            ):
                # Charts also contain helper templates; only raw YAML CRDs are accepted.
                text = tf.extractfile(member).read().decode()
                try:
                    documents = list(yaml.safe_load_all(text))
                except yaml.YAMLError:
                    continue
                for doc in documents:
                    if doc and doc.get('kind') == 'CustomResourceDefinition':
                        crds.append(doc)
for document in crds:
    group = document['spec']['group']
    kind = document['spec']['names']['kind']
    for version in document['spec']['versions']:
        schema = version.get('schema', {}).get('openAPIV3Schema')
        if schema:
            # kubeconform validates the full object, including standard apiVersion/kind/metadata.
            props = schema.setdefault('properties', {})
            props.setdefault('apiVersion', {'type': 'string'})
            props.setdefault('kind', {'type': 'string'})
            props.setdefault('metadata', {'type': 'object'})
            path = schemas / group / (kind.lower() + '_' + version['name'] + '.json')
            path.parent.mkdir(exist_ok=True)
            path.write_text(json.dumps(schema))
custom = {
    'Application',
    'AppProject',
    'Prometheus',
    'ServiceMonitor',
    'PodMonitor',
    'PrometheusRule',
    'PostgresCluster',
    'CiliumNetworkPolicy',
    'ExternalSecret',
    'ClusterSecretStore',
    'SecretStore',
}
objects = []
for path in (
    list((root / '.state/rendered').glob('*.yaml'))
    + list((root / 'gitops/applications').glob('*.yaml'))
    + [
        root / 'gitops/bootstrap/projects.yaml',
        root / 'infrastructure/networking/service-monitors.yaml',
    ]
):
    objects.extend(d for d in yaml.safe_load_all(path.read_text()) if d and d.get('kind') in custom)
target = root / '.state/custom-resources.yaml'
target.write_text(yaml.safe_dump_all(objects, sort_keys=False))
subprocess.run(
    [
        str(root / '.tools/bin/kubeconform'),
        '-summary',
        '-schema-location',
        str(schemas) + '/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json',
        '-schema-location',
        str(root / 'vendor/schemas') + '/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json',
        str(target),
    ],
    check=True,
)
