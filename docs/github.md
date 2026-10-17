# GitHub CI, releases and publication

The destination is [Hatef-Rostamkhani/odoo-high-availability](https://github.com/Hatef-Rostamkhani/odoo-high-availability).
Original code is licensed under MIT. Upstream licenses and distribution limits
are listed in [third-party notices](../THIRD_PARTY_NOTICES.md).

## Publish a clean source snapshot

Use a fresh history. The existing local history and runtime state are private;
a source rename does not change old commits, running clusters or backups.
The packaging guard rejects retired branding, private paths, symlinks and the
supplier-restricted Crunchy packaged chart. The latter is fetched from its
locked upstream digest by `scripts/vendor-tools.py`, with archive SHA-256 verification.
Do not force-add that archive or copy an existing `.git` directory into the export.

```bash
make package
mkdir -p /path/to/public-workspace
tar -xzf dist/odoo-ha-source.tar.gz -C /path/to/public-workspace
cd /path/to/public-workspace/odoo-ha
git init -b main
git add .
git commit -m "Initial public Odoo HA lab"
git remote add origin https://github.com/Hatef-Rostamkhani/odoo-high-availability.git
git push -u origin main
```

The source archive includes current tracked files and non-ignored new source.
Git history, kubeconfigs, credentials, caches, tools, backups, local evidence,
custom addons and distribution output are excluded. Review the export and run
Gitleaks against its new initial commit before pushing it. The existing local
history has an unclassified secret-scan finding and must remain private.

## Continuous integration

[CI](../.github/workflows/ci.yml) runs on pushes, pull requests and manual dispatch.
Fork PRs need no secrets; workflow permissions are read-only and checkout does
not persist credentials. Actions are pinned to commit hashes, downloaded tools
are checksum-verified, and Dependabot checks Actions and pip weekly.

| Job | Checks |
|---|---|
| Static and security checks | Publication guards, Ruff formatting/lint, generated runtime drift, local documentation links, actionlint, ShellCheck, six Helm charts, native/custom schemas, contracts and source/history secret scans |
| Unit tests (Python 3.12 / 3.13) | Runtime credential, rotation, HA, migration failure cleanup, ownership, OCI cache and publication contracts |
| Odoo and PostgreSQL integration | Documented basic installation in a disposable Kind cluster; restricted DB role; login/CRM/attachments; pod recovery; coordinated database/filestore backup and isolated restore |

Integration uses a unique owned cluster and dedicated kubeconfig and credentials.
Cleanup runs on success, failure and interruption. The Cilium, Argo, HA, PITR and
rotation gates are separate; passing basic CI does not certify them. Only selected
sanitized validation logs/JSON are uploaded; private state and pod logs are excluded.

```bash
python3 scripts/tools.py --only helm kubectl kind shellcheck kubeconform gitleaks actionlint ruff
make test-static
make test-ci-integration
```

Docker and internet access are required. Matching local OCI archives can be reused;
their platform manifests are verified against raw upstream indexes in
`tests/ci/images`. Update those snapshots together with image pins.

[Security inventory](../.github/workflows/security.yml) runs on main pushes, PRs,
manual dispatch and weekly. It scans the Python dependency and basic Odoo/PostgreSQL
images and uploads JSON inventories and CycloneDX SBOMs. Inventory completion
and zero vulnerabilities are different outcomes. `make security-gate` fails on
HIGH/CRITICAL findings and is mandatory in the release workflow.

## Repository protections

The reviewed desired policy is in [.github/settings.json](../.github/settings.json)
and [.github/rulesets/main.json](../.github/rulesets/main.json). It disables merge
commits, enables branch cleanup, requests secret scanning/push protection and
private vulnerability reporting, and requires a pull request, resolved threads
and all four CI checks before merging to `main`. Zero required approvals supports
a solo maintainer; increase that count when another reviewer is available.

```bash
python3 scripts/github-settings.py --repository Hatef-Rostamkhani/odoo-high-availability
# With an administrator token available privately in GH_TOKEN or GITHUB_TOKEN:
python3 scripts/github-settings.py --repository Hatef-Rostamkhani/odoo-high-availability --apply
```

The first command previews the policy; the second applies it and verifies the
ruleset. Never send a token through chat or commit it. Policy files alone do not
activate GitHub settings. See [validation](validation.md) for their actual status.

## Delivery and releases

[Release packages](../.github/workflows/release.yml) repeats static, full-history
secret, real integration and severity checks before packaging three Helm charts,
a history-free source archive and `SHA256SUMS`. Manual dispatch builds artifacts;
an explicit semantic tag such as `v0.1.0` publishes a GitHub Release. Only the final
publication job has write access. There is no automatic cluster deployment.

Follow [maintenance](maintenance.md) for updates, compatibility and release review.
A blocked security gate must be remediated before tagging a release. Optional
HA/network/monitoring images are outside the basic scanner's current scope.
