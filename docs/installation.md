# Installation and local access

For the one-node basic profile, follow the [README quickstart](../README.md).
The commands below install the separate three-node lab described in
[profiles](profiles.md). They use a different kubeconfig and require more resources.

## Host prerequisites

Supported installer target: Ubuntu 24.04 amd64. Read `scripts/install-host.sh`.
It installs signed Docker packages at locked versions, preserves existing Docker,
proxy, and firewall configuration, rejects conflicting packages, and grants the
invoking desktop user Docker group membership. Docker itself manages bridge and
firewall rules; an organizational firewall policy must account for this.

Run `bash scripts/install-tools.sh`, then `sudo bash scripts/install-host.sh`.
From a root shell, use `bash scripts/install-host.sh <desktop-user>` instead. The
Headlamp package is downloaded and checksum-verified by the tools installer.
Start a fresh login session for the new group, or use `sg docker -c 'make doctor'`.
Project scripts automatically re-enter this already-granted group when necessary.

Never send a sudo password through chat. An interrupted apt installation must be
resolved in your terminal before continuing. No cluster script invokes sudo.

The existing proxy is a host loopback endpoint. Tool/image downloads can use it.
Kind nodes do not inherit that endpoint. Images are fetched on the host with
Skopeo, imported into each node, assigned canonical digest aliases, and checked
through CRI before deployment. Downloads retry and have bounded timeouts.
`IMAGE_PROXY_MODE=auto` tries the inherited host transport and then a direct
per-process fallback. Set it to `host` or `direct` to choose one explicitly.
Completed OCI layers persist under `.cache/oci` for retry after interruption;
neither fallback nor cache handling rewrites proxy settings.

## Deployment order

```bash
make doctor
make test-static
# Commit the reviewed repository if it has no HEAD.
make cluster-up
make network-install
make test-network
make argocd-install
make deploy-odoo
make monitoring-install
make test
make dashboards
```

Nodes are expected to be NotReady between cluster creation and Cilium installation.
The API should already answer `/readyz`. `network-install` gates on all nodes,
Cilium, Hubble Relay, and Hubble UI becoming ready.

Scripts always select `.state/kubeconfig` and `kind-odoo-dev`; they do not
merge into or overwrite `~/.kube/config`. For manual administration:

```bash
export PATH="$PWD/.tools/bin:$PATH"
export KUBECONFIG="$PWD/.state/kubeconfig"
kubectl --context kind-odoo-dev get nodes
kind get kubeconfig --name odoo-dev > /tmp/odoo-export.kubeconfig
chmod 600 /tmp/odoo-export.kubeconfig
```

The exported kubeconfig is an administrator credential; remove it after use.

## Dashboards and credentials

`make dashboards` starts loopback forwarding supervisors, which reconnect when
their selected pods are replaced. Run it again after a host reboot. Management
commands are serialized and stop only this project's recorded forward processes.

| Dashboard | URL/access | Login |
|---|---|---|
| Odoo | `http://127.0.0.1:8069` | `admin`; private `odoo-admin` value |
| Argo CD | `https://127.0.0.1:8443` | `admin`; private `argocd-admin` value |
| Hubble | `http://127.0.0.1:12000` | Loopback forward; no separate local login |
| Grafana | `http://127.0.0.1:3000` | `admin`; private `grafana-admin` value |
| Prometheus | `http://127.0.0.1:9090` | Loopback forward; no separate local login |
| Headlamp | `make headlamp` | Dedicated read-only kubeconfig |
| K9s | `make k9s` | Dedicated read-only kubeconfig |

Open `.state/credentials.json` locally using your editor. Do not commit, print into
captured CI logs, or share this file. Change the initial Argo administrator password
through the UI, record the replacement securely, and delete
`argocd/argocd-initial-admin-secret` after verifying the new login. Its generated
initial password remains stored locally until you remove/update that entry.

Forward supervisors reconnect after their selected pods disappear. `make dashboards`
restarts missing owned supervisors and reports occupied ports instead of killing
unrelated processes. `make dashboards-stop` stops only processes whose command
lines match this project's forwards, including their active forwarding children.

Headlamp should use `.state/viewer.kubeconfig`. If the desktop app does not inherit
the environment, import that file in its cluster settings. Refresh expired tokens
with `bash scripts/kubeconfig.sh viewer`; don't disable Electron's sandbox.

## GitOps and GitLab

For local changes: edit → static checks → commit → `make gitops-publish` → sync.
Uncommitted changes are never published. A local commit needs an explicit author
identity; scripts do not change global Git configuration.

```bash
export PATH="$PWD/.tools/bin:$PATH" KUBECONFIG="$PWD/.state/kubeconfig"
argocd --core --kube-context kind-odoo-dev app diff odoo
argocd --core --kube-context kind-odoo-dev app sync odoo
argocd --core --kube-context kind-odoo-dev app wait odoo --health --sync --timeout 600
```

Core mode uses your local Kubernetes administrator credential without storing an
Argo API token. Default namespace in the dedicated admin context is `argocd`.

To connect GitLab, create the private repository, review the secret scan, add the
remote, and push using your normal authenticated Git workflow. Replace `repoURL`
in Applications and `sourceRepos` in AppProjects with the exact GitLab URL.
Use a read-only GitLab deploy key/token. Create its Argo repository Secret from a
private file or stdin; never put credentials in a URL, Helm values, or shell argv.
For SSH, verify the server fingerprint before configuring Argo's known-hosts map.
Apply reviewed project/application source changes, then verify every resolved
revision and application health before retiring local Git.

Auto-sync is opt-in: add `syncPolicy.automated` with `prune: false`,
`selfHeal: true`, and `allowEmpty: false` to the chosen dev Application and publish
that reviewed change. PVCs remain protected. Bootstrap does not manage itself.
