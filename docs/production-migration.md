# Migration to VMware and RKE2

| Area | Reusable work | Production changes required |
|---|---|---|
| Application | Helm templates, Secret references, services, probes, resource interfaces | Benchmark sizing; addon build/promotion; licensed edition/version alignment |
| GitOps | Repository layout, app boundaries, GitHub CI checks | Remote Git source, credentials, protected branches, promotion policy, resilient Argo CD |
| Network | DNS/DB allow-policy intent and Cilium/Hubble configuration principles | Verify RKE2/Cilium versions, disable competing bundled CNI, choose one kube-proxy strategy, validate MTU/firewall/routes |
| Database | Least-privilege role, connectivity/restore acceptance tests | Crunchy/Patroni HA, connection pooling evaluation, TLS, maintenance windows, production backup/PITR |
| Storage | PVC and existing-claim interfaces | Supported CSI, expansion/snapshots, RWX filestore, shared session behaviour, failure-domain tests |
| Routing | Service and disabled HTTPRoute interface | GatewayClass/controller, external load balancer, TLS/DNS, WebSocket routing and trusted proxy settings |
| Observability | Basic metrics, health probes, test evidence | Alertmanager routing, exporters, retention/remote storage, SLOs and tested alerts |
| Secrets | Out-of-band references and separate credentials | External secret manager, rotation, encryption at rest, audited access |
| Host/bootstrap | Assessment and recovery principles | OpenTofu/Terraform VMware VMs, Ansible OS baseline, RKE2 installation, independent recovery access |

Provision VMs declaratively through vCenter with OpenTofu/Terraform. Configure
supported Linux OS, time sync, DNS, kernel prerequisites, certificates, firewall,
and RKE2 prerequisites with Ansible. Use multiple control-plane/etcd nodes and
worker placement across actual ESXi hosts and resilient shared infrastructure.
Verify vCenter, RKE2, Kubernetes, Cilium, CSI, and operator compatibility together
against their official matrices at implementation time; local pins are not a
production version recommendation.

Database HA must preserve acknowledged writes, tolerate a real host failure, and
restore a consistent database plus filestore. PostgreSQL PITR without matching
attachment storage can recover references to missing files. Establish shared
filestore snapshot/versioning policy and coordinate recovery points. Test backups
from independent object storage with a documented RPO/RTO.

Before increasing Odoo replicas, validate RWX filestore permissions/locking,
shared or external session behaviour, database pool totals across workers,
WebSocket routing, shutdown/draining, and cron/module-update coordination.
Use a dedicated cron role/process or another explicitly validated execution model.
Single mode rejects multiple replicas and multiprocess workers. The HA mode
allows the tested two-web/two-cron configuration on shared NFS. Replace the
lab host NFS and entry with resilient production infrastructure before claiming
physical HA; repeat the failure-domain and capacity tests there.

For multiprocess Odoo, HTTP remains on 8069 while `/websocket` needs the gevent
endpoint, normally 8072, and trusted proxy mode. A Cilium Gateway deployment may
require kube-proxy replacement/NodePort capabilities; validate its documented
prerequisites before enabling it. Do not enable Gateway simply by turning on the
HTTPRoute template in this initial kube-proxy-enabled cluster.

Set production SLOs, alerts, capacity headroom, change windows, and rollback plans.
Promote immutable image digests and reviewed configuration, never development
credential files, synthetic databases, Kind local-path storage, or host Git mounts.
