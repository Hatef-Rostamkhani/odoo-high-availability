# Maintenance and releases

## Support

The project is experimental during 0.x. Security fixes target the latest release
and `main`; older snapshots are unsupported. Use semantic versions and document
incompatible command/configuration changes with migration and rollback steps.
Keep changes in `CHANGELOG.md` under Unreleased until a tag is actually published.

## Dependency updates

| Dependency | Source of truth | Review |
|---|---|---|
| CLI tools | `downloads.lock.json` | Version, origin URL, archive and extracted binary SHA-256 |
| Images | `versions.lock.json` and component image locks | Immutable upstream digest and runtime compatibility |
| Basic image indexes | `tests/ci/images` | Raw index SHA-256 must match image pins |
| Charts/schemas | Vendor lock and component chart metadata | SHA-256, notices, dependency and CRD changes |
| Python CI dependency | `requirements.txt` | Pinned version and supported Python matrix |
| GitHub Actions | Workflow commit hashes | Upstream release, runtime requirements and permissions |

Dependabot proposes Action and pip updates. It does not maintain the custom
image/CLI/vendor lock formats. Update those in a focused PR, verify the supplier's
digests, and update related values, snapshots and notices together. Normal installs
must not resolve latest tags.

Run `make format`, `make render-runtime`, `make test-static` and
`make test-ci-integration`. Repeat affected HA/failover/PITR or secrets gates for
their operator, runtime or state-handling changes. Test backup compatibility before
updating PostgreSQL major versions.

## Security inventory

`make security-scan` records dependency and principal image vulnerability reports
and image SBOMs under ignored `evidence/security`. CI and a weekly workflow publish
sanitized inventories. A successful functional test does not certify zero CVEs.
Scanner/network failures are failures, not successful empty reports.

`make security-gate` blocks on HIGH/CRITICAL findings and is required before a
tagged release. Fix or upgrade affected dependencies; do not silently suppress
findings. The inventory covers the Python dependency and basic Odoo/PostgreSQL
images. Optional HA/network/monitoring components need their own expanded review.

The [recorded inventory](security-inventory.md) lists the current reviewed findings
and whether a tagged release is permitted.

## Release

1. Pass all applicable checks and review security findings and remaining limits.
2. Write reviewed release notes, upgrade steps and rollback in the changelog.
3. Run `make package` and verify `dist/SHA256SUMS`.
4. Manually dispatch Release packages to build reviewable artifacts, or push an
   explicit semantic tag such as `v0.1.0` when ready to publish.
5. The tag workflow repeats validation and security gates before creating a
   release. Only publication has write access.

Attach charts, a history-free source archive and checksums. Exclude credentials,
backups, addon source and private evidence. Fetch Crunchy packaged artifacts from
their supplier instead of redistributing them.
