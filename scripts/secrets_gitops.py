"""Bounded GitOps access and public-only bootstrap manifests."""

import copy
import yaml
import openbao as b
from secrets_platform import apply, namespace_policy


def configure():
    path = b.ROOT / 'gitops/bootstrap/argocd-rbac.yaml'
    documents = list(yaml.safe_load_all(path.read_text()))
    names = ('openbao', 'external-secrets', 'secrets-system')
    template = next(
        d for d in documents if d['kind'] == 'Role' and d['metadata']['namespace'] == 'odoo-dev'
    )
    verbs = ['get', 'list', 'watch', 'create', 'update', 'patch']
    for ns in names:
        if not any(d['kind'] == 'Role' and d['metadata'].get('namespace') == ns for d in documents):
            role = copy.deepcopy(template)
            role['metadata']['namespace'] = ns
            role['rules'] = [
                r
                for r in role['rules']
                if r['apiGroups'] != ['postgres-operator.crunchydata.com']
                and not (r['apiGroups'] == [''] and r['resources'] == ['secrets'])
            ]
            for r in role['rules']:
                if r['apiGroups'] == ['cilium.io']:
                    r['verbs'] = verbs
            documents.append(role)
            documents.append(
                {
                    'apiVersion': 'rbac.authorization.k8s.io/v1',
                    'kind': 'RoleBinding',
                    'metadata': {'name': 'odoo-argocd-controller', 'namespace': ns},
                    'roleRef': {
                        'apiGroup': 'rbac.authorization.k8s.io',
                        'kind': 'Role',
                        'name': 'odoo-argocd-controller',
                    },
                    'subjects': [
                        {
                            'kind': 'ServiceAccount',
                            'name': 'argocd-application-controller',
                            'namespace': 'argocd',
                        }
                    ],
                }
            )
    application = next(
        d for d in documents if d['kind'] == 'Role' and d['metadata']['namespace'] == 'odoo-dev'
    )
    if not any('external-secrets.io' in r['apiGroups'] for r in application['rules']):
        application['rules'].append(
            {
                'apiGroups': ['external-secrets.io'],
                'resources': ['secretstores', 'externalsecrets'],
                'verbs': verbs,
            }
        )
    cluster = next(
        d for d in documents if d['kind'] == 'Secret' and d['metadata']['name'] == 'odoo-in-cluster'
    )
    cluster['stringData']['namespaces'] = ','.join(
        dict.fromkeys(cluster['stringData']['namespaces'].split(',') + list(names))
    )
    cluster['stringData']['clusterResources'] = 'true'
    metadata_cluster = {
        'apiVersion': 'rbac.authorization.k8s.io/v1',
        'kind': 'ClusterRole',
        'metadata': {'name': 'odoo-secrets-gitops-cluster'},
        'rules': [
            {
                'apiGroups': ['apiextensions.k8s.io'],
                'resources': ['customresourcedefinitions'],
                'verbs': ['get', 'list', 'watch'],
            },
            {
                'apiGroups': ['rbac.authorization.k8s.io'],
                'resources': ['clusterroles', 'clusterrolebindings'],
                'verbs': ['get', 'list', 'watch'],
            },
            {
                'apiGroups': ['admissionregistration.k8s.io'],
                'resources': ['validatingwebhookconfigurations'],
                'verbs': ['get', 'list', 'watch'],
            },
        ],
    }
    # CRDs and cluster bootstrap RBAC are installed independently. Argo observes
    # them; namespaced controllers remain within explicitly allowed namespaces.
    binding = {
        'apiVersion': metadata_cluster['apiVersion'],
        'kind': 'ClusterRoleBinding',
        'metadata': metadata_cluster['metadata'],
        'roleRef': {
            'apiGroup': 'rbac.authorization.k8s.io',
            'kind': 'ClusterRole',
            'name': metadata_cluster['metadata']['name'],
        },
        'subjects': [
            {
                'kind': 'ServiceAccount',
                'name': 'argocd-application-controller',
                'namespace': 'argocd',
            }
        ],
    }
    # Fixed, vendored CRDs need tracking metadata updates after independent
    # bootstrap. No cluster Secret access or unrestricted cluster writes.
    rendered = b.run(
        [
            b.ROOT / '.tools/bin/helm',
            'template',
            'external-secrets',
            b.ROOT / 'vendor/external-secrets-2.12.0.tgz',
            '-n',
            'external-secrets',
            '-f',
            b.ROOT / 'infrastructure/secrets/external-secrets-values.yaml',
        ]
    )
    crds = [
        d['metadata']['name']
        for d in yaml.safe_load_all(rendered)
        if d and d['kind'] == 'CustomResourceDefinition'
    ]
    metadata_cluster['rules'].append(
        {
            'apiGroups': ['apiextensions.k8s.io'],
            'resources': ['customresourcedefinitions'],
            'resourceNames': crds,
            'verbs': ['update', 'patch'],
        }
    )
    metadata_cluster['rules'].append(
        {
            'apiGroups': ['admissionregistration.k8s.io'],
            'resources': ['validatingwebhookconfigurations'],
            'resourceNames': ['secretstore-validate', 'externalsecret-validate'],
            'verbs': ['update', 'patch'],
        }
    )
    documents = [d for d in documents if d['metadata']['name'] != 'odoo-secrets-gitops-cluster'] + [
        metadata_cluster,
        binding,
    ]
    path.write_text(yaml.safe_dump_all(documents, sort_keys=False))
    for document in documents:
        apply(document)
    values_path = b.ROOT / 'gitops/bootstrap/argocd-values.yaml'
    values = yaml.safe_load(values_path.read_text())
    inclusions = yaml.safe_load(values['configs']['cm']['resource.inclusions'])
    extra = [
        (['external-secrets.io'], ['ExternalSecret', 'SecretStore']),
        (['apiextensions.k8s.io'], ['CustomResourceDefinition']),
        (['admissionregistration.k8s.io'], ['ValidatingWebhookConfiguration']),
    ]
    for groups, kinds in extra:
        item = next((item for item in inclusions if item['apiGroups'] == groups), None)
        if item:
            item['kinds'] = list(dict.fromkeys(item['kinds'] + kinds))
        else:
            inclusions.append({'apiGroups': groups, 'kinds': kinds})
    rbac = next(item for item in inclusions if item['apiGroups'] == ['rbac.authorization.k8s.io'])
    rbac['kinds'] = list(dict.fromkeys(rbac['kinds'] + ['ClusterRole', 'ClusterRoleBinding']))
    values['configs']['cm']['resource.inclusions'] = yaml.safe_dump(inclusions, sort_keys=False)
    values['configs']['cm']['resource.respectRBAC'] = 'strict'
    values_path.write_text(yaml.safe_dump(values, sort_keys=False))
    apply(
        {
            'apiVersion': 'v1',
            'kind': 'ConfigMap',
            'metadata': {'name': 'argocd-cm', 'namespace': 'argocd'},
            'data': {
                'resource.inclusions': values['configs']['cm']['resource.inclusions'],
                'resource.respectRBAC': 'strict',
            },
        }
    )
    for ns in names:
        document = namespace_policy(ns)
        apply(document)
        directory = b.ROOT / 'gitops/platform' / ns
        (directory / 'network.yaml').write_text(yaml.safe_dump(document, sort_keys=False))
        if ns != 'secrets-system':
            (directory / 'templates/network.yaml').write_text('{{ .Files.Get "network.yaml" }}\n')
        else:
            p = directory / 'kustomization.yaml'
            v = yaml.safe_load(p.read_text())
            if 'network.yaml' not in v['resources']:
                v['resources'].append('network.yaml')
            p.write_text(yaml.safe_dump(v, sort_keys=False))
