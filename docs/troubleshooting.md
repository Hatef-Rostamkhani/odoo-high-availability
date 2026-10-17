# Troubleshooting

Always select `.state/kubeconfig` and `kind-odoo-dev`. Start with `make doctor`
and `make status`; use `kubectl -n <namespace> describe pod <name>` and events.

| Symptom | Check and action |
|---|---|
| Docker command missing | Run the reviewed host installer; verify `docker --version` in the same environment |
| Docker socket permission denied | New login or `sg docker -c 'make status'`; never chmod the socket world-writable |
| apt/registry TLS errors | Retry bounded downloads and inspect host proxy reachability; don't disable TLS validation |
| Registry works directly but proxy returns EOF | Use `IMAGE_PROXY_MODE=direct make <installation-target>`; this changes only that process's transport |
| Nodes NotReady before Cilium | Expected during bootstrap; inspect Cilium install output, kernel/cgroup requirements, agent logs |
| Cached image alias missing | Inspect node `ctr -n k8s.io images ls`; canonical digest aliases must be present in CRI |
| Pods ImagePullBackOff | Re-run the image-cache/deployment step; host loopback proxy is unavailable inside nodes |
| Cilium connectivity failures | Save JUnit, policy verdicts, agent status, flows, DNS and cross-worker tests before changing settings |
| PVC Pending | Local-path uses WaitForFirstConsumer; inspect consuming pod placement and provisioner events |
| Odoo init Job failed | Read Job logs; check DB role/Secret alignment; don't start the Deployment with default credentials |
| Odoo says Wrong login/password | Enter `admin` in Email and copy the value beside `"odoo-admin"` in the private `.state/credentials.json`, without quotes. `odoo-admin` is a key, not a password. Run make test-application to verify the stored credential; do not edit the file as a password reset. |
| PostgreSQL credentials changed but auth fails | Existing PGDATA ignores init scripts; rotate the database role password explicitly before changing Secrets |
| Argo repository unreachable | Check local Git service, bare repository HEAD/info/refs, committed publication, and repo-server policy |
| Argo RBAC errors | Review namespace-scoped roles and bootstrap cluster RBAC; never fix with a blanket cluster-admin binding |
| Monitoring API/CRDs absent | Re-run monitoring-install; cluster CRDs/RBAC are bootstrap-owned |
| metrics-server TLS errors | Kind-only exception is configured; use valid serving certificates in production |
| Dashboard connection reset | The owned forwarding supervisor reconnects after pod replacement. If forwards were stopped or the host rebooted, run make dashboards again; inspect .state/forward-<name>.log if reconnection fails. |
| Port already occupied | Stop the identified conflicting process yourself; forwards don't kill unrelated listeners |
| Viewer authorization expires | Refresh the one-hour viewer kubeconfig; re-import it in Headlamp if necessary |
| Desktop app won't launch as root | Run as the desktop user in the graphical login session; keep Electron's sandbox enabled |
| OOMKilled/high desktop CPU | Check host totals, requests/limits, Prometheus retention, and concurrent tests; don't sum Kind node capacity |
| Backup/upgrade interrupted | Inspect maintenance state and failed helper/Job; restore from verified backup before resuming writes |

Do not remove existing clusters, replace host proxy configuration, flush firewall
rules, or run Docker system prune as a troubleshooting shortcut.
