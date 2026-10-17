# Contributing

Use an issue for a reproducible bug or a focused feature proposal. Send security
reports through the private route in [SECURITY.md](SECURITY.md).

## Development

Ubuntu 24.04 on amd64 is the supported development host. Use Python 3.12 or 3.13.
Install the pinned tools and run the same checks as CI:

```bash
python3 scripts/tools.py --only helm kubectl kind shellcheck kubeconform gitleaks actionlint ruff
make format
make render-runtime
make test-static
```

For application changes, also run `make test-ci-integration`. It creates its own
Kind cluster, exercises login/CRM/files, replaces pods and restores a coordinated
database/filestore backup into an isolated namespace. It deletes only its owned
test resources. Live HA tests need the larger lab described in [profiles](docs/profiles.md).

## Code and generated files

Keep CLI entry points small. Shared Python code belongs in `scripts/odoo_platform`;
live acceptance tests belong in `tests`. Keep command names compatible or document
the migration. Use explicit ownership checks before changing external resources.

Run `make format` for Python formatting and `make lint-python` for checks. Edit the canonical runtime
files under `infrastructure/secrets`, then run `make render-runtime`; generated
ConfigMaps are checked for drift in CI. Do not hand-edit their embedded Python.
Use names that explain a value's purpose and separate validation, side effects
and cleanup. Add tests for actual failure behavior rather than repeating the code.

## Pull requests

Describe the problem, resulting behavior and validation. Include relevant resource
costs or downtime for operational changes. Keep unrelated changes in separate PRs.
Do not commit credentials, `.state`, kubeconfigs, private evidence, backups or
unapproved addon source. Use synthetic data in examples and screenshots.

Dependencies are pinned in the lock files. Follow [maintenance](docs/maintenance.md)
when updating them. Contributions to the project's original code use the MIT
license; third-party licenses remain with their respective components.
