#!/usr/bin/env python3
import json
import sys
import yaml

images = set()


def walk(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key == 'image' and isinstance(child, str):
                images.add(child)
            else:
                walk(child)
    elif isinstance(value, list):
        for child in value:
            walk(child)


for filename in sys.argv[1:]:
    with open(filename) as stream:
        for document in yaml.safe_load_all(stream):
            walk(document)
for image in sorted(images):
    if '@sha256:' not in image and (
        ':' not in image.rsplit('/', 1)[-1] or image.endswith(':latest')
    ):
        raise SystemExit(f'Unpinned image: {image}')
    print(image)
