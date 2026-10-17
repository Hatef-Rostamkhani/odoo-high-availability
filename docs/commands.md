# Three-node lab command reference

For the smaller basic profile, use the [README quickstart](../README.md).
This reference covers the separate three-node lab.

Reproducible local Kubernetes environment for Odoo ERP/CRM development and HA testing.
The lab stack is three Kind nodes, Cilium VXLAN/eBPF, Hubble, Argo CD,
PostgreSQL 17, Odoo 19 Community, metrics-server, Prometheus, and Grafana.
Headlamp runs on the desktop; K9s uses a read-only Kubernetes identity.

This repository contains executable deployment and validation workflows.
**Installation is successful only when live validation passes.** Read
[validation status](validation.md) and local `evidence/results.md`; a rendered
manifest is not installation evidence.

For a public repository, follow [GitHub publication, CI and releases](github.md).
`make source-archive` creates a scanned source snapshot without Git history or
private local state. GitHub Actions validates code, schemas and credentials,
runs Odoo/PostgreSQL integration tests, and packages tagged releases.

## First installation

Read [installation](installation.md) before making host changes.

```bash
cd odoo-ha
bash scripts/install-tools.sh
sudo bash scripts/install-host.sh
# If already root, use: bash scripts/install-host.sh <desktop-user>
# Re-login for Docker group membership. Scripts can also use the granted group through sg.
make doctor
make test-static
git add .
git commit -m "Build Odoo development platform"
make cluster-up
make network-install
make test-network
make argocd-install
make deploy-odoo
make monitoring-install
make test
make dashboards
```

The first commit needs a Git author identity. Use your existing identity; otherwise
set `git config user.name` and `git config user.email` **in this repository only**.
No global Git identity or remote is changed by scripts.

Odoo: <http://127.0.0.1:8069>. [Odoo instance HA](odoo-ha.md) adds shared storage, replicated web/cron and measured failure tests. Credentials are in the private
`.state/credentials.json`; do not paste them into chat, issues, or logs.
Enter `admin` in the Email field and use the value beside `"odoo-admin"` as the
password. The text `odoo-admin` is a credential key, not the password.

## Daily workflow

Edit charts/values, run `make test-static`, commit, and run `make gitops-publish`.
Synchronize through Argo CD or the documented core-mode CLI. Sync is manual and
pruning is disabled by default. `make deploy-odoo` explicitly syncs PostgreSQL
before Odoo; `make monitoring-install` bootstraps platform RBAC/CRDs before sync.

| Command | Behaviour |
|---|---|
| `make doctor` | Read-only host, tools, routes, ports, registry, and resource assessment |
| `make install-tools` | Verify and install locked CLI artifacts into `.tools/bin` |
| `make cluster-up` | Create only `odoo-dev`, preserving the user's kubeconfig |
| `make network-install` | Install/upgrade Cilium and Hubble through Helm |
| `make argocd-install` | Bootstrap secrets, local Git, and non-HA Argo CD |
| `make gitops-publish` | Publish committed HEAD to the private read-only Git mirror |
| `make deploy-odoo` | Cache images and explicitly sync PostgreSQL, then Odoo |
| `make monitoring-install` | Bootstrap platform permissions and sync metrics/monitoring |
| `make status` / `make logs` | Inspect resources/metrics or recent Odoo logs |
| `make test-static` | Lint, render, schema validation, contract tests, secret scan |
| `make test-unit` | Unit and Helm contract tests |
| `make test-public` | Verify the publication file set and scan source for secrets |
| `make test-ci-integration` | Disposable Kind test: login, CRM, attachments and persistent pod recovery |
| `make source-archive` | Scanned public source archive without private state or Git history |
| `make package` | Validate and package application Helm charts, public source and checksums |
| `make test` | All live gates, including pod recovery and isolated backup restore |
| `make test-network` | Policy/connectivity matrix and official Cilium suite |
| `make test-network-core` | Four official internal policy cases; verifies cases executed; separate from the full suite |
| `make test-application` | SQL, HTTP, login, CRM, and filestore checks |
| `make test-resilience` | Delete application/DB pods and validate a rollout |
| `make dashboards` / `make dashboards-stop` | Start/stop owned loopback forwards |
| `make headlamp` / `make k9s` | Start desktop/terminal viewers with an expiring read-only identity |
| `make kubeconfig` | Show the dedicated kubeconfig location; never print its credentials |
| `make backup` | Quiesce Odoo and back up matching database/filestore |
| `make restore BACKUP=/absolute/path` | Replace dedicated dev data with a verified backup |
| `make restore-test` | Back up and restore into temporary isolated resources |
| `make addons-build ADDONS_REVISION=<commit>` | Build an immutable custom-addon image |
| `make module-install MODULES=<names>` | Back up, stop Odoo, install explicit modules, resume |
| `make module-upgrade MODULES=<names>` | Back up, stop Odoo, upgrade explicit modules, resume |
| `make restart` | Restart Odoo with a brief single-replica outage |
| `make recreate` | Back up, rebuild the dedicated cluster, and restore application data |
| `make cluster-down` | Back up and delete only the dedicated cluster |
| `make ha-install` | Bootstrap Crunchy/Patroni HA in the existing cluster |
| `make ha-test` | PostgreSQL instance failover and strict replication checks |
| `make load-test ENABLE_LOAD=true K6_IMAGE=<digest>` | Two users for 30 seconds; dashboards must be running |

## Documentation

- [Architecture and security](architecture.md)
- [Version compatibility and upstream sources](versions.md)
- [Installation and dashboards](installation.md)
- [Operations, backups, and addons](operations.md)
- [Validation and expected results](testing.md)
- [Troubleshooting](troubleshooting.md)
- [Database HA, PITR and migration](database-ha.md)
- [Production migration](production-migration.md)
- [GitHub publication, CI and releases](github.md)

State, credentials, addon source, backup data, and local evidence are ignored by
Git. Vendor charts and artifact checksums are committed. Stage/prod directories
are intentionally incomplete and must not be deployed as production environments.

PostgreSQL HA bootstrap, measured acceptance gates, migration and safe reverse transfer are documented in [docs/database-ha.md](database-ha.md).

| Command | Purpose |
|---|---|
| `make ha-status` | Verify placement, replication and strict synchronous settings |
| `make ha-backup` | Wait for a completed pgBackRest Full backup |
| `make ha-restore-test` | Verify PITR markers in an isolated clone |
| `make ha-migrate` | Validate and migrate Odoo to HA within the measured budget |
| `make ha-rollback` | Fresh consistent backup and reverse migration after commit |
