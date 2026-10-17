# Version decisions and authoritative sources

This snapshot targets Kubernetes 1.35.8 rather than Kind's newest default.
Cilium 1.20.2 documents Kubernetes 1.33–1.36 compatibility; Argo CD 3.5 is
tested against 1.33–1.36. Helm 4.3 supports 1.34–1.37, and metrics-server 0.9
supports 1.34+. Kubernetes 1.35 is inside every documented range.

- [Kind 0.33.0 node images and digests](https://github.com/kubernetes-sigs/kind/releases/tag/v0.33.0)
- [Cilium 1.20.2 Kind instructions](https://github.com/cilium/cilium/blob/v1.20.2/Documentation/installation/kind.rst)
- [Cilium Kubernetes compatibility](https://github.com/cilium/cilium/blob/v1.20.2/Documentation/network/kubernetes/compatibility.rst)
- [Cilium kernel prerequisites](https://github.com/cilium/cilium/blob/v1.20.2/Documentation/operations/system_requirements.rst)
- [Argo CD tested Kubernetes versions](https://github.com/argoproj/argo-cd/blob/v3.5.4/docs/operator-manual/tested-kubernetes-versions.md)
- [Helm version support](https://helm.sh/docs/topics/version_skew/)
- [metrics-server compatibility and requirements](https://github.com/kubernetes-sigs/metrics-server/blob/v0.9.0/README.md)
- [Docker Ubuntu installation](https://docs.docker.com/engine/install/ubuntu/)
- [Odoo 19 PostgreSQL prerequisites](https://www.odoo.com/documentation/19.0/administration/on_premise/source.html)
- [Odoo 19 runtime/deployment](https://www.odoo.com/documentation/19.0/administration/on_premise/deploy.html)
- [Official Odoo container source](https://github.com/odoo/docker/tree/master/19.0)
- [Prometheus chart source](https://github.com/prometheus-community/helm-charts/tree/kube-prometheus-stack-92.3.0/charts/kube-prometheus-stack)
- [Crunchy PGO 6.0.2 release](https://github.com/CrunchyData/postgres-operator/releases/tag/v6.0.2)

The host has cgroup v2 and the required BPF, cgroup-BPF, ingress classifier,
and VXLAN kernel configuration. Agent readiness and actual policy/connectivity
tests are the runtime proof; a sufficiently recent kernel alone is not proof.

`versions.lock.json` pins versions and principal image digests.
`downloads.lock.json` pins tool/package URLs, archive checksums, and extracted
binary checksums. `images.lock.json` records rendered image references and the
official test images. `vendor/charts.lock.json` records package digests and sources.
`tests/ci/images` preserves digest-verified upstream Odoo/PostgreSQL image indexes
for safe reuse of platform-specific OCI caches. GitHub Actions are pinned to
commit hashes; actionlint is installed from `downloads.lock.json`.
The tools installer resolves no latest versions. Versions change only through
reviewed lock/values changes followed by static and live compatibility tests.

Upstream chart-managed images use explicit release tags or digests. Host caches
retain fetched artifacts; mutable tags are not a production promotion mechanism.
Use immutable image digests for production and for custom addon image promotion.
The initial CNI/controller pins require no advanced kernel features beyond those
checked during assessment.
