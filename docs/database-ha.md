# PostgreSQL HA and Odoo migration

The existing `odoo-dev` Kind cluster runs Crunchy PGO 6.0.2 independently of
Argo CD in `postgres-ha`. `odoo-ha` has PostgreSQL 17.10 on three distinct
Kubernetes nodes, one primary and two replicas. Required hostname anti-affinity,
a database-only control-plane toleration, and `minAvailable: 2` preserve this
placement without removing the control-plane taint. All three nodes still share
one physical computer. Odoo has one replica and its existing local filestore PVC.

Patroni uses a 30-second lease, a 10-second loop, synchronous mode, strict mode,
and one synchronous standby. PostgreSQL uses `synchronous_commit=on`, 60 maximum
connections and 128 MB shared buffers. The acceptance test counts only transactions
whose successful commits were acknowledged to the client. Cancelled or timed-out
commits may be ambiguous and are not treated as acknowledged transactions.

## Bootstrap and validation

```bash
make ha-install
make ha-status
make ha-test
make ha-backup
make ha-restore-test
bash scripts/ha.sh rehearse
HA_ROLLBACK_REHEARSAL=true make ha-migrate
make ha-migrate
```

Installation checks 12 GiB available memory and 35 GiB free disk before preparing
images; cached, completed images require at least 20 GiB free disk before bootstrap.
Images are verified, pinned by digest in `images.lock.json`, and imported into all
three Kind nodes. The vendored Helm archive installs the operator and CRDs without
Argo CD. Repeat installation reuses the existing private HA password. The namespace
and operator remain bootstrap-owned; the manual, non-pruning Argo CD application
owns only the PostgresCluster, policies and monitoring objects. Operator-owned
StatefulSets and generated Secrets are not GitOps sources. PostgresCluster deletion
and PVC pruning are protected; Argo's HA role cannot delete either.

The operator and Patroni API policies use Cilium’s `kube-apiserver` entity,
restricted to TCP 443/6443 and the operator/database pod selectors. pgBackRest
backup/restore Jobs and the repository configuration sidecar also need this
access to discover repository pods and reload configuration; separate policies
select only these workloads. Installation
also verifies the current API Service and control-plane addresses.
Default-deny policies allow DNS, database replication, Patroni, pgBackRest and
Prometheus scraping. The Odoo chart selects `database.backend=standalone|crunchy`.
Crunchy requires `verify-full` and a CA Secret mount. All HTTP probe timeouts are
configurable; development uses five seconds. Secrets live in Kubernetes and private
`.state` files, never in Git. Odoo application administrator passwords are preserved
by restoring the database; the separate HA database login password is generated
privately. `ha-install` copies only the public CA to `odoo-ha-ca`.

`ha-test` stops only the new operator, scales the current primary's StatefulSet to
zero, observes election and a successful write through the fixed primary Service
within 90 seconds, checks 100 acknowledged commits, and restores all instances.
Its strict-mode phase stops both standby instances and requires a client write to
time out without acknowledgement; successful writes must recover after cleanup.
SIGINT and SIGTERM restore the saved StatefulSet replicas and operator. Kind nodes
are never paused or stopped by these tests.

`ha-backup` waits for a newly completed Full backup rather than reporting request
submission as success. pgBackRest 2.58.0 archives WAL continuously, schedules daily
Full backups at 03:00 UTC (06:30 Tehran), and retains two Full backups on a 5 GiB
local repository PVC. `ha-restore-test` inserts markers before and after a UTC target,
waits for WAL archival, restores a single-instance clone with sync disabled and
three-node affinity removed, and checks that only the before marker exists. Only
test-owned clone PVCs are cleaned up. Database PITR is not complete Odoo recovery:
the filestore must be consistent with the selected database recovery point.

## Measured migration and reverse transfer

Migration requires fresh passing replication, TLS, failover, strict, backup, PITR
and dump/restore rehearsal evidence from the current source files. A deliberate
pre-commit rollback rehearsal must also succeed before the real migration.
The migration command publishes reviewed local Git commits to the existing local
GitOps repository and manually syncs Odoo; it does not push to an external server.

The shared maintenance lock excludes backup and module changes. Only the owned
Odoo forward on 8069 stops; other forwards stay active. Odoo scales to zero and the
command confirms all server pods have terminated, stopping HTTP and cron. It records
hashes and row counts for every public table, captures a consistent database dump
and filestore archive with checksums and versions, and keeps the original database
and filestore PVC available. The restore retains `odoo_dev`, record IDs, user
passwords and ownership by the unprivileged `odoo` role.

Odoo's declarative connection changes to
`odoo-ha-primary.postgres-ha.svc` with `verify-full`, initialization disabled,
and cron zero. A private forward checks login, CRM records, attachment checksums,
attachment create/read/delete, and another full application check after planned
primary switchover. The public forward stays closed during validation.

The pre-commit budget is 240 seconds, with 60 seconds reserved for rollback. Before
commit, failures restore the source backend automatically. The private commitment
marker is persisted before cron/public writes resume. On success cron returns to
one, the public forward reopens, and GitOps scales the old database to zero while
retaining its PVC. Actual outage and recovery times are recorded, including any
five-minute target breach.

```bash
make ha-rollback
```

After commitment this performs a **fresh coordinated HA database/filestore backup
and reverse transfer** into the retained standalone database. It never reconnects
to the stale pre-migration database. Before commitment the cutover command itself
performs automatic rollback. `backup`, `restore`, `recreate`, and application tests
resolve the active backend rather than assuming `postgresql-0` exists. A complete
ERP restore test still restores both the dump and matching filestore in an isolated
namespace. HA dumps use a disposable single-instance Crunchy cluster so that
`pgaudit` and `pg_stat_statements` packages match the source. Extensions and their
comments are prepared as postgres; application objects are restored as odoo.
Standalone dumps use the original standalone image. For reverse transfer to the original standalone image, a fresh backup records
whether the operator’s `pgaudit` extension has any application dependencies or
data-bearing relations. Only when both counts are zero may that server audit
instrumentation be omitted on a target without its package; all ERP data and
application objects remain in the dump. Other missing packages fail before
commitment and leave HA authoritative. Full HA ERP recovery retains all extensions.

Evidence is stored in `evidence/ha/` with PASS, FAIL, NOT RUN or BLOCKED status,
revision, source SHA256, UTC time, versions and measured duration. Host-local backups
and the pgBackRest repository do not protect against loss of the whole computer.

References: [Patroni replication modes](https://patroni.readthedocs.io/en/latest/replication_modes.html),
[Crunchy supported platforms](https://access.crunchydata.com/documentation/postgres-operator/latest/overview/supported-platforms),
[Crunchy TLS connections](https://access.crunchydata.com/documentation/postgres-operator/latest/tutorials/basic-setup/connect-cluster).
