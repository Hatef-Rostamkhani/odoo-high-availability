# Operations, addons, backup, and recovery

## Backup consistency

`make backup` takes a maintenance lock, stops Odoo HTTP and cron, waits for its
server pods to terminate, runs PostgreSQL's custom-format `pg_dump`, and archives
the matching filestore from a non-root helper pod. It writes checksums, versions,
and the Git revision into a private timestamped directory, then restarts Odoo.
It fails if there is not exactly one active replica.

Sessions are intentionally not restored; users log in again after restoration.
No database credentials are stored in backup metadata. Database dumps may contain
sensitive business records and password hashes. Backups are owner-only and ignored
by Git, but share this desktop's failure domain. Encrypt and copy important
backups to independent storage. The default local procedure has no automatic
retention deletion; review available disk space before repeated large backups.

```bash
make backup
make restore-test
# Explicitly replace the dedicated dev database and filestore:
make restore BACKUP=/path/to/odoo-ha/backups/<timestamp>
```

Restore validates checksums and database compatibility, stops Odoo, recreates the
dev database, restores the dump, replaces the filestore, and resumes Odoo only
after success. If interrupted, Odoo remains stopped; fix the fault and repeat
the verified restore. Do not restore an untrusted archive. Importing company
backups and cross-major database migration are outside the synthetic initial setup.

`make restore-test` restores to a temporary isolated namespace with fresh PVCs
and disabled cron. It verifies login, CRM IDs, and attachment hashes and deletes
only its own labeled test namespace. Test PVCs use local-path Delete reclamation.

## Addon installation and upgrades

Keep authorized addon source in `addons/`. Its source is ignored by default;
track approved modules explicitly or maintain their separate repository. Build
with an immutable revision tag and load that image into the dedicated cluster.
Edit only dev values, render/test, commit, publish, and synchronize.

```bash
make addons-build ADDONS_REVISION=<addon-commit>
PATH="$PWD/.tools/bin:$PATH" kind load docker-image --name odoo-dev odoo/odoo-addons:19.0-<addon-commit>
make module-install MODULES=my_module
make module-upgrade MODULES=my_module,another_module
```

Each module operation backs up first, stops the HTTP/cron deployment, and runs a
single dedicated Job with `--stop-after-init --no-http --max-cron-threads=0`.
Failures leave Odoo stopped and retain the failed Job for diagnosis. Restore its
consistent backup before allowing writes after an incompatible upgrade.
The normal pod startup never installs/upgrades modules on an existing initialized DB.

Initialization marks successful credential/fixture creation in the database.
An interrupted initial Job reruns initialization rather than exposing a database
with the upstream default administrator password. Initializer and PVC share a
sync wave so delayed local-path binding cannot deadlock bootstrap.

## Lifecycle

`make restart` restarts Odoo; it does not promise zero downtime.
`make recreate` captures a consistent backup, deletes/rebuilds only the dedicated
cluster, reboots its bootstrap components, deploys, restores application data,
and reinstalls monitoring. Grafana dashboards are provisioned from configuration;
ephemeral UI changes and Prometheus history do not survive recreation.

`make cluster-down` inventories resources and backs up before deleting
`odoo-dev`. Other clusters and Docker settings are preserved. A broken
application that cannot be backed up blocks deletion. Only if you deliberately
accept loss of this development data may you use:

```bash
ALLOW_DATA_LOSS=odoo-dev make cluster-down
```

Host backups, credentials, and Git mirror are retained. Purge them manually only
after reviewing their exact paths; there is no broad Docker prune or unattended
host-data deletion command.
