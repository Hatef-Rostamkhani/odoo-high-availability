# Odoo instance HA in the existing Kind cluster

Odoo HA mode runs two multiprocess web pods, two independent cron pods and two
HAProxy edge pods across the existing Workers. The existing Crunchy primary
Service and TLS identity remain authoritative. The loopback address stays
`http://127.0.0.1:8069`; an independently bootstrapped Docker TCP proxy replaces
the Odoo port-forward. Other dashboard forwards are unaffected.

This is instance HA. Host NFS, the Docker entry proxy and all Kind nodes share
one physical machine. Host or storage failure is outside the acceptance boundary.
Production needs independent hosts, a resilient external load balancer and HA
shared storage, HTTPS and off-host backups.

## Bootstrap and migration

```bash
sudo bash scripts/odoo-ha-host.sh
make odoo-ha-install
bash scripts/odoo-ha.sh rehearse
make odoo-ha-migrate
make odoo-ha-status
make odoo-ha-test
```

The host script creates only the project NFS export and configuration, pins its
Ubuntu package and raises `fs.inotify.max_user_instances` to at least 1024.
Kind shares host UID resource accounting; the default limit of 128 prevented
mountd/idmapd from starting. The server listens on the Kind bridge address,
exports only to the three node addresses and uses NFSv4, synchronous writes and
root squashing. `/srv/odoo/odoo-data` belongs to UID/GID 1000 with mode 2770.
The static RWX claim uses hard NFSv4.1 mounts and a Retain PV. Its 5 GiB claim is
nominal; free space comes from the underlying host filesystem.

Install validates a shared write/rename/read across both Workers, prepares pinned
images and proxies and exposes a private validation entry on loopback 18069.
It leaves the original application and data claim active. The isolated rehearsal
restores an existing checksummed ERP archive, moves its files to a private NFS
data directory, starts two web replicas and validates a fresh reverse copy.

Migration holds the common maintenance lock, stops the owned Odoo forward and
all HTTP/cron writers, captures a coordinated database/filestore backup and
verifies the final file copy. The private web deployment has cron disabled;
existing session cookies, database signatures, login, CRM and attachments are
checked before the commitment marker. Public writes and cron then resume.
The target outage is 300 seconds, with a 240-second precommit budget and 60
seconds reserved for rollback. Actual timing and any failure are retained.

## Runtime and failure tests

HA web pods have two HTTP workers, a gevent endpoint on 8072 and no cron workers.
The proxy routes `/websocket` to gevent. HTTP pools use three connections and
the separate gevent pool uses four; cron
needs four for its listener, scheduler, job and registry cursors. The calculated
application budget including one web surge, exporters and readiness probes is
48 connections, within the 48-connection target and PostgreSQL limit of 60.
This reserves 30 for three web pods (two HTTP workers at three and gevent at four),
8 for cron, 5 for exporters, 2 for cron probes and 3 for maintenance clients.
The database role also has CONNECTION LIMIT 45, reserving three maintenance
connections within 48. Kubernetes can briefly overlap terminating and surge
pods; this role limit bounds all their application, exporter and probe clients
together. PostgreSQL retains its overall 60-connection limit.
The proposed three-connection gevent pool exhausted during repeated WebSocket
reconnections with one remaining web instance, so it has its own bounded pool.
Worker soft/hard virtual-memory limits are 384/512 MiB; the initially proposed
256 MiB soft limit recycled workers before their first request. Shared file sessions make a retained
login usable across both pods without sticky sessions. HTTP POSTs are not
automatically replayed. In-flight requests can fail during an induced fault.

Cron runs without HTTP in two separate processes. Odoo's PostgreSQL row locks
coordinate scheduled jobs. This does not guarantee exactly-once external effects;
custom integrations still need idempotency.

The cron launcher exits if Odoo's scheduler thread terminates unexpectedly, so
kubelet restarts the process. This covers a PostgreSQL failover closing the
LISTEN connection: a live parent with a dead scheduler must not remain silently
healthy. Database probes alone cannot establish that jobs are running.
The heartbeat updates only its dedicated telemetry key with SQL, avoiding a
global configuration/asset-cache invalidation on every minute tick.

HAProxy probes `/ready` on the existing sidecar port 9116 every second. It uses
a fresh background database check and verifies both local application ports;
the response avoids queueing behind business HTTP workers. This prevents busy
workers from being incorrectly removed by the one-second health timeout. The
sidecar bounds health and metrics to one simultaneous database connection.
Kubernetes startup/liveness use `/live` (local listeners only) and readiness
uses `/ready` (listeners and database). These probes keep the five-second
timeout while avoiding business worker queues and database-triggered restarts.
The ten virtual business users use a 30-second request timeout; the separate
failure observer retains a five-second timeout and the ten-second recovery SLA.

`odoo-ha-test` checks WebSocket upgrades, deletes and kills individual web
instances, deletes each edge replica, checks the same authentication cookies and
confirmed writes, tests cron recovery, and runs ten virtual users for ten minutes.
It then invokes the existing PostgreSQL failover/strict test. Workers themselves
remain running. Tests cause brief service disruptions and use temporary records.
Cleanup removes their records and waits for healthy replacement instances.

Web rollouts keep a new pod Ready for 35 seconds before replacing an old one,
covering the existing CoreDNS 30-second TTL. A 35-second preStop drain keeps
the old process serving until cached DNS answers expire. Headless DNS publishes
Ready pods; direct health checks remove abruptly failed backends sooner.

Acceptance is at most 10 seconds to the start of 30 seconds of stable recovery
after a web/edge fault, at most 90 seconds for database/cron recovery, no lost
acknowledged test records, no OOM and healthy final replicas on distinct Workers.
Evidence under `evidence/odoo-ha/` includes PASS/FAIL/BLOCKED/NOT RUN, revision,
versions, source fingerprint and timings. HTTP availability, HAProxy backends,
replicas, latency/errors, cron heartbeat, sessions, NFS free space and database
connections/backups appear on the Odoo HA Grafana dashboard.
The availability exporter also checks a retained authenticated cookie through
the entry proxy, using a dedicated portal-only account. Its random credential
is stored privately and mounted only into the monitoring probe; it has no
administrator privileges. Lost session identity is reported before reauthentication.

## Maintenance and rollback

Backup/restore/module operations discover the active data claim and save/stop
both web and cron replica counts under one maintenance lock. Complete ERP backup
still contains a matching database dump and filestore. Restore clears existing
session files instead of reviving archived login sessions. Failed restore or
module maintenance leaves writers stopped for inspection.

```bash
make odoo-ha-rollback
```

Application rollback stops all writers and copies the CURRENT shared filestore
and sessions back to the retained local claim before starting one Odoo instance.
Crunchy remains the database backend. Never reconnect an old filestore to current
database records without that fresh synchronization. `ha-rollback` is a separate
database operation and has its own fresh-transfer requirements.

Argo CD synchronization remains manual with pruning disabled. NFS, the static
PV/PVC and host entry bootstrap remain recoverable independently of Argo CD.
No cluster recreation is run as part of these acceptance tests.
