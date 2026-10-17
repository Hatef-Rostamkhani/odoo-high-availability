# Observed implementation status

This document separates current public-source checks from historical local HA
observations. Passing a local gate does not establish a GitHub-hosted run or
production readiness. The existing local deployment was preserved.

## Public preparation — 2026-10-17

| Check | Result |
|---|---|
| Source publication guards | PASS: retired branding absent; history, private state, credentials and supplier-restricted packaged chart excluded |
| Readability and generated runtime | PASS: Ruff formatting/lint and canonical Python-to-ConfigMap drift checks |
| Unit tests | PASS: 49 tests, including failure cleanup, rollback, interruption, ownership, secret redaction and corrupted image-cache rejection |
| Static validation | PASS: actionlint, ShellCheck, six Helm charts, 260 native resources and 74 custom resources, documentation links and source secret scan |
| Basic profile on preceding image | PASS: real basic installation, login/CRM/attachments, least privileges, pod replacement and isolated coordinated database/filestore restore; 408.21 seconds |
| Basic profile on PostgreSQL 17.11 | PASS: fresh disposable installation, restricted role, login/CRM/attachments, pod recovery and isolated coordinated restore; 393.97 seconds; owned cluster cleaned up |
| Exported checkout | PASS: static validation passed in an isolated source checkout; the omitted Crunchy chart was fetched upstream and checksum-verified |
| Demo image | PASS: actual running basic-profile CRM screenshot with synthetic data |
| Evidence privacy | PASS: generated basic credentials and their base64 forms redacted from report logs |
| Security inventory | PASS: reports/SBOMs generated; Python and Odoo have zero HIGH/CRITICAL findings in this scan |
| Security release gate | FAIL: PostgreSQL 17.11 has 95 HIGH and 4 CRITICAL package findings; see [inventory](security-inventory.md) |
| Clean source packages/history | PASS: source archive and three Helm packages; verified SHA256SUMS and MIT notices; clean initial Git commit passed Gitleaks |
| GitHub publication and Actions | IN PROGRESS: repository access and initial MIT license creation verified after a transient connector transport failure; full source publication and hosted checks are pending |
| GitHub protections | NOT APPLIED: reviewed policy files and administration script are prepared; remote settings have not been activated |

The unit matrix targets Python 3.12/3.13; only the local runtime has been executed
here. Scanner counts refer to the locked images and refreshed database used on
this date. They are not a continuing zero-vulnerability assurance. No tagged
release is permitted while the security gate fails.

Current machine-readable evidence and sanitized logs are kept in ignored
`evidence/public-*.json`, `evidence/public-*.log`, and `evidence/security`.
The PostgreSQL update changes the basic image pin only; it does not upgrade the
existing local HA database or replay historical HA acceptance tests.

## Earlier platform observations

Reviewed on 2026-10-11. The platform is installed and Odoo is accessible, but final
acceptance remains incomplete because the full connectivity suite failed and
several maintenance/desktop checks have not been completed.

| Component/check | Observed result |
|---|---|
| Kind/Kubernetes | PASS: dedicated cluster, exactly three Ready nodes, API readyz and kube-proxy healthy |
| Cilium/Hubble | PASS: three agents, one operator, Relay and UI healthy |
| Explicit network policy matrix | PASS: cross-worker Pod/Service traffic, DNS, external GitHub HTTPS, default-deny, explicit allow, recorded Hubble drops |
| Full official Cilium suite | FAIL: 20-minute deadline after external Cloudflare HTTP/TLS failures; unfiltered host-network control also timed out. The cause remains unresolved. See networking.log and cilium-connectivity.xml. |
| Official internal policy subset | PASS: four named cases, restricted to internal Pod/Service scenarios, executed 42 actions; 131 other tests and one scenario skipped. This does not replace the full-suite failure. |
| Argo CD/GitOps | PASS: four applications Healthy/Synced at the published revision; managed annotation drift detected and reconciled |
| PostgreSQL/Odoo | PASS: PostgreSQL 17, non-superuser application role, bound 10 GiB/5 GiB PVCs; Odoo 19 login, CRM fixture, attachment upload/download and DB-aware HTTP health |
| Pod persistence | PASS: Odoo and PostgreSQL pods replaced; recovered fixtures and attachment hash match the pre-test snapshot |
| Full resilience run | FAIL: deliberately interrupted to restore interactive Odoo access; rollout phase not reached |
| Dashboard reconnection | PASS: Odoo forward recovered in 1.43 seconds after its forwarding child exited; exact browser endpoint and administrator login verified |
| Monitoring | PASS: Kubernetes metrics collected; Prometheus and Grafana ready; enabled targets healthy; Cilium/Hubble scraped; Odoo availability metric equals 1; bound 5 GiB Prometheus PVC |
| K9s | PASS: interactive read-only context displayed application pods; RBAC permits pod inspection and denies Secrets |
| Headlamp | NOT RUN: package installed and startup observed; graphical resource inspection not verified |
| Backup/isolated restore | NOT RUN: scripts implemented; live maintenance test deferred while restoring interactive access |
| Deployment rollout/outage measurement | NOT RUN: implemented Recreate test not reached in the interrupted run |
| HA/PITR/load/addon experiments | NOT RUN: opt-in implementations prepared; no production HA or load claim |
| Local static checks | PASS: ShellCheck, Python compilation, Helm lint/rendering, native/CRD schemas, contract tests and source secret scan |
| GitLab CI pipeline | NOT RUN: pipeline configuration exists; no remote GitLab repository is connected |

Current sanitized machine-readable and Markdown evidence is kept in the ignored
`evidence/` directory. Each gate records its command, UTC time, duration, exit
status and Git revision. Failed full-suite and interrupted recovery evidence is
retained; a successful subset does not replace either result.

## PostgreSQL HA acceptance — 2026-10-12

Odoo now runs on `odoo-ha` in the existing Kind cluster, with three database
instances on three nodes, strict synchronous replication and verified TLS. The
original database is declaratively scaled to zero; its PVC remains bound.

| HA acceptance gate | Observed result |
|---|---|
| Placement, replication, TLS and role privileges | PASS: one primary, two streaming replicas, one synchronous standby; PostgreSQL 17.10; Odoo has no superuser/create-role/create-db privileges |
| Instance failover | PASS: 3.712 seconds; all 100 client-acknowledged commits preserved; all instances/operator recovered |
| Strict mode | PASS: no acknowledged write with both standby instances stopped; writes recovered afterwards |
| Signal cleanup | PASS: SIGTERM during primary failure restored the operator and all three instances |
| Full backup and database PITR | PASS: Full completed; isolated PITR clone retained the before marker and excluded the after marker |
| Pre-commit rollback rehearsal | PASS: actual private Odoo validation, switchover and deliberate rollback completed in 89.451 seconds |
| Odoo migration | PASS: 98.365 seconds of measured cutover; full public-table signatures matched before private validation; HTTP, login, CRM IDs and attachment checks passed |
| Complete ERP recovery | PASS: actual HA dump and matching filestore restored into a disposable Crunchy cluster; Odoo authenticated and attachment create/read/delete passed |
| Fresh reverse-transfer compatibility | PASS: fresh HA ERP backup restored into isolated PostgreSQL using the original standalone image; only dependency-free, data-free operator pgaudit instrumentation omitted |
| Prometheus/Grafana | PASS: three Crunchy exporters scraped; role, replication, connections and Full backup age panels configured |
| Cluster recreation / live post-commit reverse migration | NOT RUN: the cluster must remain intact and the requested final backend is HA; source/bootstrap paths were validated statically and reverse data transfer was exercised in isolation |

Evidence and sanitized logs are in `evidence/ha/` and `evidence/ha-*.json` / `.log`.
Earlier failed attempts remain archived under `evidence/ha/attempts/`. These HA
results do not replace the historical full Cilium suite failure below. All Kind
nodes share one computer, and local repositories are not host disaster recovery.

## Access

Run `make dashboards` after a host reboot. Supervisors bind only `127.0.0.1` and
reconnect after pod replacement. Odoo uses `http://127.0.0.1:8069/web/login`.
Enter `admin` in Email and copy the actual value beside `"odoo-admin"` from the
private `.state/credentials.json`, without quotation marks. Credential keys are
labels, not passwords. The browser endpoint was authenticated with that value.

Argo CD: `https://127.0.0.1:8443`; Hubble: `http://127.0.0.1:12000`;
Grafana: `http://127.0.0.1:3000`; Prometheus: `http://127.0.0.1:9090`.
See installation.md for their private credential keys and desktop viewers.

## Outstanding acceptance work

The complete ERP restore and HA recovery gates above are verified. The historical
full resilience/rollout suite remains a separate outstanding gate. Use `make headlamp`
and its read-only kubeconfig to inspect nodes, workloads, PVCs, logs and events.
Investigate the external Cilium failures and retain a full-suite successful result
before declaring all requested network validation complete.

The desktop rebooted during implementation. Git metadata was recovered while
preserving its original copy under private `.state/git-recovery/`; Git fsck passed.
No unrelated cluster or global kubeconfig was removed. Kind node recovery shares
one physical host and does not prove production high availability.
