# Architecture and security

The local Prometheus operator serves internal HTTP metrics with its admission
webhook and webhook TLS disabled. Kubernetes scrapes use a bootstrap-owned
service-account token Secret; no token values are committed or managed through
Argo CD. This chart requires a persistent token reference for those scrapes.
Use a reviewed credential rotation and TLS design for production monitoring.

The Ubuntu desktop runs Docker Engine. Kind runs one control plane and two worker
containers, all sharing the same host kernel, CPUs, RAM, and physical disk.
Do not add node allocatable totals to estimate available desktop capacity.

```text
Ubuntu Desktop
  Docker → Kind odoo-dev
    kube-system: Cilium agents, one operator, CoreDNS, Hubble, metrics-server
    argocd: controller, repo-server, API/UI server, Redis
    gitops-system: read-only local Git service and viewer identity
    odoo-dev: Odoo web replicas + cron replicas → PostgreSQL HA
    postgres-ha: Crunchy operator → 1 primary + 2 synchronous-mode replicas
    odoo-edge: two HTTP/WebSocket proxies → shared host NFS
    monitoring: Prometheus operator/server, Grafana, kube-state-metrics, health exporter
```

Pod CIDR: `10.244.0.0/16`; Service CIDR: `10.96.0.0/16`. Docker/Kind bridge addressing
is discovered before creation. Host routes and Docker networks are checked for
overlap. kube-proxy remains enabled; Cilium uses Kubernetes IPAM and VXLAN.
Advanced kube-proxy replacement, host routing, encryption, and Gateway controller
features are not enabled implicitly.

Bootstrap owns Kind, Cilium/Hubble, Argo CD, namespaces, platform cluster RBAC/CRDs,
local secrets, and local Git. Argo CD owns namespace-scoped application and
monitoring workloads. Bootstrap can recover the platform without Argo CD.
There is no root Application that installs its own controller or CNI.

The local Git service exports only a bare repository populated from committed
HEAD. A native read-only Git protocol endpoint supplies the reference advertisement required by Argo CD. Receive-pack and upload-archive are disabled.
NetworkPolicy permits Argo's repo-server to read it; there is no host-exposed Git
port or remote push endpoint. The only Kind-specific host mount is this Git mirror.
Application charts use PVCs and Kubernetes services, without hostPath mounts.
The bootstrap-only `gitops-system` namespace has a Pod Security admission
exception for the read-only Git hostPath mount. Its Git pod still runs non-root,
drops capabilities, mounts no API token, and cannot write that path. Argo projects
cannot deploy into this namespace. Application namespaces remain restricted.

## Persistence and availability

Kind's default local-path storage binds a volume to one simulated node. Database
and filestore survive pod replacement, but not cluster deletion or a failed volume
node. Claims are protected from Argo pruning/deletion; PostgreSQL retains claims
when its StatefulSet is deleted or scaled. Cluster deletion still destroys node
storage. Consistent host backups are required before recreation/deletion.

Odoo supports single-instance development mode and the validated HA profile.
The active HA profile uses two web and two cron pods on different Workers,
shared host NFS for filestore/sessions and two HAProxy edge pods. A Docker TCP
entry outside Kind serves loopback 8069. See [Odoo HA](odoo-ha.md) for tests,
maintenance and fresh-file rollback. Host NFS and the entry share the computer's
failure domain; this does not provide physical infrastructure HA.

Target normal incremental host usage is roughly 8–12 GiB. Actual usage is measured
by `make status`; four physical cores limit concurrent load/network tests. The
initial full-stack gate requires 12 GiB available RAM and 35 GiB free disk. If
that gate fails, disable monitoring and review a one-worker configuration rather
than silently altering the tested topology.

## Security boundaries

- Rootful Kind nodes and Cilium require privileged infrastructure containers.
  Docker group membership is root-equivalent access to this machine.
- Application pods run as non-root, drop capabilities, use RuntimeDefault seccomp,
  disable privilege escalation, and do not mount service-account tokens.
- Application resources have requests/limits. Database superuser and application
  roles are separate; Odoo has no superuser, createdb, or createrole privileges.
- Namespace default-deny policies constrain workload traffic. API port-forwarding
  is administrative access and can bypass ordinary Pod network ingress policy.
- Viewer RBAC permits inspection/logs/metrics, not Secrets, exec, or writes. Its
  token expires after one hour. Bootstrap kubeconfig remains an administrator key.
- Argo CD gets limited cluster reads and writes only to designated namespaces.
  Prometheus and Cilium receive their upstream component permissions independently.
- Local Kubernetes Secrets are not a production secret store. Kind etcd data is
  not configured for application-layer encryption at rest. Protect kubeconfig,
  `.state`, and backups using OS access controls and the host's encrypted disk.
- PostgreSQL host authentication uses SCRAM. The active Crunchy backend uses
  TLS with verify-full and a mounted public CA; administrative local sockets
  remain confined to database containers.
- metrics-server uses a Kind-only `--kubelet-insecure-tls` exception. Replace it
  with validated kubelet serving certificates in production.
- Dashboards bind loopback. Argo CD uses its local self-signed HTTPS certificate;
  the other forwarded dashboards use HTTP on loopback only.

Kind worker-container pauses reproduce some node-loss symptoms. A real desktop
failure stops every node, every PVC, Git, and local backup access. Neither the
base stack nor the optional three-instance database demonstrates physical HA.
