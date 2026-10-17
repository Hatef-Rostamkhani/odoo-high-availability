# Validation and expected results

GitHub's portable checks and disposable application smoke test are described in
[GitHub CI and releases](github.md). `make test-unit` runs the contracts;
`make test-public` checks names, private paths and source secrets;
`make test-ci-integration` exercises the documented basic profile in an owned
disposable Kind cluster, including pod recovery and an isolated coordinated
database/filestore restore. `make basic-test` exercises the same recovery checks
on an existing basic environment.

Use `make test-static` before deployment and `make test` after all components are
installed. Tests that change state target only the dedicated development context.
Recovery tests delete pods; backup tests pause Odoo and cause a brief outage.

| Gate | Command | Expected evidence |
|---|---|---|
| Host | `make doctor` | Adequate budget, discovered routes/ports, registry HTTP results |
| Python readability | `make lint-python` | Ruff lint and format checks; canonical runtime generation checked for drift |
| Security inventory | `make security-scan` / `make security-gate` | Vulnerability JSON and image SBOMs; release gate fails on HIGH/CRITICAL |
| Basic recovery | `make test-ci-integration` | Login, CRM, attachments, pod replacements and isolated coordinated restore |
| Static | `make test-static` | actionlint, ShellCheck, Helm, built-in/CRD schemas, unit contracts, no leaked secrets |
| Foundation | `bash tests/foundation.sh` | API healthy; three Ready nodes; kube-proxy/Cilium/Hubble ready |
| Network | `make test-network` | Cross-worker Pod/Service/DNS/HTTPS works; deny fails; explicit allow works |
| Official network suite | Included in network gate | Cilium suite exits zero; JUnit and flow files recorded |
| Official internal policy subset | `make test-network-core` | Four named official cases execute without skips/failures; does not replace the full-suite result |
| Application | `make test-application` | Non-superuser DB role; HTTP/login/CRM/attachment roundtrip passes |
| GitOps | `bash tests/gitops.sh` | All active Applications Healthy/Synced at published HEAD |
| Metrics | `bash tests/monitoring.sh` | Metrics API works, enabled Prometheus targets healthy |
| Recovery | `make test-resilience` | Pod replacements and rollout retain IDs/file hashes; outage samples recorded |
| Backup/restore | `make restore-test` | Isolated restored database/filestore matches original fixtures |
| Desktop | `make headlamp`, `make k9s` | Manually inspect nodes, namespaces, workloads, PVCs, logs, events, metrics |
| Load | Explicit opt-in k6 target | Two users/30s; <1% failures and p95 <2 seconds |
| HA/PITR | `make ha-test`, `make ha-restore-test` | Measured instance failover, strict writes and sentinel-verified PITR |

Tests use pre-existing successful traffic to establish that a negative policy
result is caused by denial rather than a broken service. The official connectivity
suite is pinned through the installed CLI; its required images are cached before
execution. External connectivity failures are reported, not silently skipped.

`scripts/report.py` records PASS, FAIL, BLOCKED, or NOT RUN with UTC timestamps,
duration, command, exit status, Git revision, and sanitized logs. Credentials in
the local recovery file are redacted before saving output. Evidence is ignored
by Git; `docs/validation.md` records the reviewed completion status.

Static Kubernetes schema checks validate native objects strictly. Vendored custom
resource definitions supply OpenAPI schemas for Argo/Prometheus objects, which are
validated separately; missing custom schemas are errors. The CRD definition schema
is captured from the pinned Kubernetes 1.35.8 API OpenAPI. Contract tests cover
delayed binding, replica safeguards, restore initialization/cron, external database
policy selection, and existing filestore claims. Static passing does not prove
that an image can start or that networking works.

The rollout measurement counts samples without a Ready pod, rather than claiming
an exact user-visible outage duration. Base local-path volumes do not demonstrate
cross-node data availability. Headlamp/K9s terminal interaction must be recorded
as manual or NOT RUN if the agent cannot observe the desktop.
