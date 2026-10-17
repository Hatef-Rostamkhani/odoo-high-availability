# OpenBao and Odoo database credentials

This development installation retains the existing Kind nodes, PostgreSQL HA,
Odoo image, database owner, application passwords, NFS filestore, and sessions.
It adds three TLS-protected Raft members with Static Auto-unseal, namespace-scoped
External Secrets, a rotation controller, and two independent expiry guards.
The host and its local keys remain a shared trust and failure boundary.

```bash
make openbao-install         # Prerequisites, encrypted etcd, Raft, ESO, guards; no app cutover
make openbao-status
make openbao-backup          # AES-256-GCM encrypted Raft snapshot
make openbao-restore-test    # Separate owned clone; missing/wrong seal key tests
make secrets-test           # Owned DB/app/Bao clones; accelerated deadlines only here
make secrets-migrate        # Gated, coordinated live cutover
make secrets-status
make secrets-rotate         # Fresh generation, normal 24-hour lifetime
make secrets-revoke         # Immediately invalidate current and pending app credentials
make secrets-rollback       # Resume a valid transition or issue a fresh generation
make secrets-observe        # Actual 24-hour observation; never changes live TTL
```

After all shorter gates pass, `bash scripts/secrets-observation-service.sh`
starts the actual observation as a user service. It keeps the same private
cookie, checks cron/WebSocket, records acknowledged writes, and requires a
completion emitted by the automatic controller. It refreshes the acceptance
report after completion or failure. Gaps over 75 seconds fail the observation;
wall time without samples cannot become a PASS. Status is available with
`systemctl --user status odoo-secrets-observation.service`.

Results are in `evidence/secrets/`. `scripts/secrets-report.py` assembles the
acceptance matrix. PASS for installation is separate from final acceptance;
final acceptance also requires live application/database faults, ERP restore,
monitoring, and a completed real 24-hour observation. Each artifact records the
revision, source fingerprint, versions, timestamp, and non-sensitive generation.
Previous attempts remain under `evidence/secrets/attempts/`.

## Keys and bootstrap

Private keys and recovery material are outside the checkout in
`~/.local/share/odoo-dev/secrets/` (directory 0700, files 0600). The 32-byte
static seal key, independent AES-GCM etcd key, snapshot encryption key, and CA
private key are separate. Bootstrap preserves existing material and refuses to
invent a replacement seal key or initialize new Raft data over an existing
recovery record. Back up this private directory separately from encrypted
snapshots, using independently protected storage.

Secrets in etcd use AES-GCM before the seal Secret is created. The API server's
original manifest is retained privately. Bootstrap rewrites existing Secrets and
checks their actual etcd ciphertext format. Only the API server is restarted;
Kind nodes are not stopped.

The initial root token is revoked after bootstrap. The `bao-operator` identity
has permission to start **authenticated** quorum recovery, with three recovery
shares also required. No operator token is mounted in a running controller.
`python scripts/openbao-recovery.py` with the project secrets interpreter tests
this procedure and immediately revokes the recovered root token. Both
unauthenticated generation APIs and untrusted TLS connections are rejected.

Cluster bootstrap RBAC is installed independently and skipped by Argo sync.
Argo watches allowed namespaces with `resource.respectRBAC=strict`, following
the [Argo CD RBAC discovery documentation](https://argo-cd.readthedocs.io/en/stable/operator-manual/declarative-setup/#auto-respect-rbac-for-controller).
This permits namespaced discovery without granting Secret reads in OpenBao or
watch permissions for unrelated ExternalSecrets across the cluster.

The management Service is ClusterIP. If a UI forward is needed, bind it explicitly
to `127.0.0.1`, use the private CA, and a short-lived authorized token. Never send
recovery material, seal keys, passwords, or tokens to logs, Git, or chat.

## Rotation and failure behavior

`odoo` remains the database/object owner and becomes NOLOGIN after commitment.
`odoo_login_a` and `odoo_login_b` are fixed, unprivileged login roles. Their database
role setting gives `session_user=odoo_login_*` and `current_user=odoo`; this also
keeps newly created module objects owned by `odoo`.

The control database records the generation, first usable time, expiry, phase,
publication version, and replicated maintenance lease. Passwords reside in KV v2,
not in those control tables. The controller can execute bounded administrative
functions and patch only the credential-generation annotations of the two Odoo
Deployments. It also selects the validated, immutable KV version in the one
ExternalSecret. GitOps ignores only those operational fields.

Immutable KV version selection is necessary for snapshot recovery: restoring an
older Bao snapshot makes the requested newer version unavailable. ESO then retains
the existing Secret instead of publishing an older password. The controller uses
PostgreSQL's generation/deadline as authority and issues fresh credentials when
Bao's active generation disagrees. A staged credential already marked as issued
is never reused as a fresh password after recovery.

Each live generation expires at most 86,400 seconds after PostgreSQL first makes
it usable. Preparation starts 900 seconds before expiry; the controller checks
every 30 seconds. Web pods roll first, cron pods replace one at a time, a real cron
heartbeat is required, and the old account is retired after the old processes
finish. App connection limits across both accounts sum to 45. During overlap the
controller reallocates that same budget only after old capacity is released.

Expiration is independent of Bao and ESO. PostgreSQL VALID UNTIL rejects new
password-authenticated connections. Each guard checks every second and terminates
expired sessions, checking the latest role generation and backend identity/start
 time. Its expiry function performs no writes, so strict synchronous replication
cannot block session termination. With healthy guards and reachable PostgreSQL,
termination must happen within five seconds. Removing both guards loses that
specific guarantee and raises a critical alert. No failed transfer or maintenance
operation extends the old deadline.

`secrets-revoke` causes immediate disruption by design. Automatic recovery may
issue fresh credentials on the next controller pass. `secrets-rollback` never
reactivates the fixed owner password or an expired generation.

## Maintenance and backups

Backup, restore, restart, module operations, and database fault tools share an
advisory lock plus a replicated, bounded maintenance lease. PostgreSQL failover
can drop the session lock, while the acknowledged lease still prevents rotation
from competing with maintenance. Operations require enough credential lifetime
for their budget and recovery margin. Expiry guards keep running throughout.
A failed restore or module operation keeps all web/cron writers closed.

ERP backups contain a dump and matching filestore with all writers stopped.
Session files are not restored from archives. A live ERP restore issues fresh
credentials before reopening writers and repairs database-specific role settings.
The isolated restore tests likewise use freshly issued login credentials.
Database PITR alone is not a complete Odoo recovery.

Daily Raft snapshots run at 03:15 UTC, retain seven days, and are encrypted before
writing to their retained PVC. `openbao-backup` creates an encrypted host archive
with the same format. Raft data/audit PVCs and snapshot storage have Retain and
pruning/deletion protection. Local snapshots provide no protection from complete
host loss. `recreate` must not be used to discard Raft state without an explicit,
verified encrypted-snapshot recovery workflow and the original external keys.

The Grafana dashboard and alerts cover Raft readiness, ESO, phase/deadline,
per-login connection counts, guard heartbeat, authentication failures, cron,
session, NFS capacity, and snapshot age. Alert evaluation and actual Prometheus
targets are part of acceptance, rather than inferred from applied manifests.
`scripts/secrets-monitor.py` checks actual scrape series and exercises critical
alert rules with synthetic samples in a temporary isolated promtool job.
