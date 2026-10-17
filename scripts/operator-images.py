#!/usr/bin/env python3
"""Expose operator-created images to the cache without creating synthetic resources."""

import sys
import yaml

docs = list(yaml.safe_load_all(open(sys.argv[1])))
images = []
for doc in docs:
    if not doc:
        continue
    if doc['kind'] in ('Prometheus', 'Alertmanager'):
        image = doc['spec'].get('image')
        if image:
            images.append(image)
    if doc['kind'] == 'Deployment':
        for container in doc['spec']['template']['spec']['containers']:
            for arg in container.get('args', []):
                for prefix in (
                    '--prometheus-config-reloader=',
                    '--prometheus-config-reloader-image=',
                ):
                    if arg.startswith(prefix):
                        images.append(arg[len(prefix) :])
print(
    yaml.safe_dump({'images': [{'image': image} for image in sorted(set(images))]}, sort_keys=False)
)
