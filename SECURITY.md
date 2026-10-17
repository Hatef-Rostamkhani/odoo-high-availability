# Security policy

## Reporting a vulnerability

Use this repository's **Security → Advisories → Report a vulnerability** route:
[private report](https://github.com/Hatef-Rostamkhani/odoo-high-availability/security/advisories/new).
Do not open a public issue with credentials, a kubeconfig, exploit details or
private database content. If private reporting is unavailable, contact the
maintainer through the public GitHub profile to arrange a private channel before
sending sensitive details: [maintainer](https://github.com/Hatef-Rostamkhani).

Include the affected revision/profile, prerequisites, impact and a minimal
reproduction using synthetic data. Replace secret values in logs with placeholders.
Do not attach backups, `.state`, environment dumps or unreviewed dashboard exports.

## Supported versions and scope

During the initial 0.x development series, security fixes target the latest
released version and `main`. Older snapshots have no maintenance commitment.
The supported host is Ubuntu 24.04 amd64. The basic profile is a local development
environment. The three-node HA lab shares one physical host; it is not a tested
production deployment or protection against loss of that host.

No response-time SLA is promised. Reports are assessed by the maintainer and
disclosed with a fix or mitigation when practical.

## Repository controls

Gitleaks scans public source and Git history. Workflows use pinned Actions,
read-only build permissions and checksum-verified CLIs. Security inventory scans
track Python dependencies and the principal application images. Inventory results
are reports; release gates are described in [maintenance](docs/maintenance.md).
Repository settings, including private reporting and push protection, must be
enabled on GitHub; a policy file does not activate those settings by itself.
