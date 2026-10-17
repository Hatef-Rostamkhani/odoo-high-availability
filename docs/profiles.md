# Deployment profiles

## Basic

`make basic-up` creates one Kind node, the pinned official PostgreSQL/Odoo images,
a local RWO filestore and private credentials. It renders the fresh standalone
values in `gitops/environments/dev/odoo.yaml` and `postgresql.yaml`. It needs no
pre-existing HA cluster, TLS secret, shared PVC, OpenBao or credential controller.
State is under `.state/odoo-basic`. Reconciliation preserves generated credentials.
Deletion verifies the local ownership marker and Kind container label.
`ODOO_BASIC_CLUSTER=odoo-basic-example` selects another owned basic environment.

`make basic-test` checks the generic company name, least-privilege database role,
login, CRM and attachment round trips. It replaces PostgreSQL and Odoo pods and
compares fixture IDs and attachment hashes. It then stops Odoo, captures its
database plus matching filestore, resumes the source in cleanup and restores both
into the owned `odoo-restore-test` namespace. An occupied restore namespace is
rejected. The test causes a brief outage; `basic-down` deletes this lab's data.

## Three-node GitOps lab

Follow [installation](installation.md), then:

```bash
make doctor
make test-static
make cluster-up
make network-install
make test-network
make argocd-install
make deploy-odoo
make monitoring-install
make test
```

Commit reviewed changes before publishing to the private read-only Git mirror.
Defaults start with one Odoo process, static credentials, initialization enabled
and standalone PostgreSQL. Full network and monitoring gates need more resources
than the basic test. They are not claimed as GitHub-hosted acceptance results.

## Database HA and application HA

Follow [database HA](database-ha.md) for operator installation, strict replication,
failover, backup/PITR, migration rehearsal and rollback gates. Operator installation
alone neither migrates Odoo nor proves failover. The source remains authoritative
until the measured cutover commits.

After database HA passes, [Odoo HA](odoo-ha.md) adds shared filestore/session
storage, web and cron replicas and a loopback entry point. Static-credential target
values are in `odoo-ha.yaml`; do not apply them to an unprepared cluster. Host NFS
setup is an explicit privileged operation.

Migration commands publish selected active values to local GitOps. A migrated
lab may have different values from a fresh public checkout. Do not reset active
values while that lab is serving a different backend.

## Rotating credentials

After database and application HA pass, use [secrets migration](secrets.md) to
introduce OpenBao/ESO and rotating roles. `odoo-ha-rotating.yaml` documents the
final credential-mode overlay. The overlay does not replace bootstrap or tests.

## Limits

Kind nodes, host NFS and local backup storage share one machine. Host disaster
recovery, production capacity, independent backup storage and production operation
remain unverified. Stage/prod directories require their own configuration and gates.
