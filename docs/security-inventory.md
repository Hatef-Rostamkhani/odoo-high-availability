# Security inventory and release status

## Local observation — 2026-10-17

The locked Trivy 0.75.0 scanner completed dependency and image inventories with
its vulnerability database refreshed during the run. These are scanner findings,
not a determination that every issue is exploitable in this deployment.

| Scanned dependency | HIGH | CRITICAL |
|---|---:|---:|
| PyYAML 6.0.3 (`requirements.txt`) | 0 | 0 |
| Odoo 19 image pinned in `versions.lock.json` | 0 | 0 |
| PostgreSQL 17.11 Bookworm image pinned in `versions.lock.json` | 95 | 4 |

The PostgreSQL findings cover 49 distinct vulnerability identifiers and 99 package
findings; one vulnerability can affect several packages. The preceding 17.9 image
had 147 HIGH/CRITICAL package findings. Updating to the official 17.11 image reduced
the count but did not clear the release gate. Twenty-five remaining package
findings had fixed-version information in this scan.

The CRITICAL package findings were `libsqlite3-0` (CVE-2025-7458), `libxml2`
(CVE-2026-6653), `zlib1g` (CVE-2023-45853) and the Go standard library embedded in
an image utility (CVE-2025-68121). The first three had no fixed version listed by
the scanner for this image; the Go finding had fixed versions listed. Review
vendor advisories and affected functionality before any exposure or exception.
No vulnerability exclusions or severity reductions were added.

**Release gate: FAIL.** A tagged release must wait for remediation and a fresh
successful gate. The source remains an experimental local lab. Counts change
as vulnerability databases and image pins change; rerun the inventory rather
than treating this document as a current security certification.

## Reproduce and extend

```bash
python3 scripts/tools.py --only trivy
make security-scan
make security-gate
```

Reports and CycloneDX image SBOMs are generated in ignored `evidence/security`.
The inventory command records findings without using them as its exit condition;
the gate command fails on HIGH/CRITICAL findings. Scanner or transport failure
also fails the command. GitHub's security workflow uploads the reports when run.

This scope covers the basic profile's Python dependency and two main images.
The optional HA, network, monitoring and secrets components require an expanded
inventory before release or deployment. See [maintenance](maintenance.md) and
[security reporting](../SECURITY.md).
