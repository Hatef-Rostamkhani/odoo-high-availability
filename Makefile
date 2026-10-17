SHELL := /bin/bash
.DEFAULT_GOAL := help
.PHONY: test-network-core
.PHONY: help doctor install-tools cluster-up network-install argocd-install gitops-publish deploy-odoo monitoring-install status logs test test-static test-network test-application test-resilience dashboards dashboards-stop backup restore restore-test addons-build module-install module-upgrade restart recreate cluster-down kubeconfig headlamp k9s ha-install ha-test load-test
help:
	@python3 scripts/help.py
doctor:
	@bash scripts/prerequisites.sh
install-tools:
	@bash scripts/install-tools.sh
basic-up:
	@python3 scripts/basic.py up
basic-test:
	@python3 scripts/basic.py test
basic-status:
	@python3 scripts/basic.py status
basic-forward:
	@python3 scripts/basic.py forward
basic-down:
	@python3 scripts/basic.py down
format:
	@.tools/bin/ruff format scripts tests helm/odoo/files infrastructure/secrets
lint-python:
	@.tools/bin/ruff check scripts tests helm/odoo/files infrastructure/secrets
	@.tools/bin/ruff format --check scripts tests helm/odoo/files infrastructure/secrets
render-runtime:
	@python3 scripts/render-runtime.py
check-generated:
	@python3 scripts/render-runtime.py --check
security-scan:
	@python3 scripts/security-scan.py
security-gate:
	@python3 scripts/security-scan.py --gate

.PHONY: security-scan security-gate

.PHONY: render-runtime check-generated

.PHONY: basic-up basic-test basic-status basic-forward basic-down format lint-python
cluster-up:
	@bash scripts/create-cluster.sh
network-install:
	@bash scripts/install-cilium.sh
argocd-install:
	@bash scripts/bootstrap-argocd.sh
gitops-publish:
	@bash scripts/gitops-publish.sh
deploy-odoo:
	@bash scripts/deploy.sh application
monitoring-install:
	@bash scripts/deploy.sh monitoring
status:
	@bash scripts/status.sh
logs:
	@bash scripts/logs.sh
test-static:
	@bash tests/static.sh
test-unit:
	@python3 -m unittest discover -s tests/unit -v
test-public:
	@python3 tests/public-source.py
	@python3 tests/secret-scan.py
test-ci-integration:
	@bash tests/ci/integration.sh
source-archive: test-public
	@python3 scripts/package-release.py --source-only
package: test-static
	@python3 scripts/package-release.py

.PHONY: test-unit test-public test-ci-integration source-archive package
test:
	@bash scripts/validate-cluster.sh
test-network:
	@bash tests/networking/run.sh
test-network-core:
	@bash tests/networking/core.sh
test-application:
	@bash tests/application/run.sh
test-resilience:
	@bash tests/resilience/run.sh
dashboards:
	@bash scripts/dashboards.sh start
dashboards-stop:
	@bash scripts/dashboards.sh stop
backup:
	@bash scripts/backup.sh
restore:
	@bash scripts/restore.sh
restore-test:
	@bash tests/database/restore.sh
addons-build:
	@bash scripts/addons-build.sh
module-upgrade:
	@bash scripts/module-upgrade.sh
module-install:
	@MODULE_ACTION=install bash scripts/module-upgrade.sh
restart:
	@bash scripts/restart.sh
recreate:
	@bash scripts/recreate.sh
cluster-down:
	@bash scripts/destroy-cluster.sh
kubeconfig:
	@bash scripts/kubeconfig.sh
headlamp:
	@bash scripts/headlamp.sh
k9s:
	@bash scripts/k9s.sh
ha-install:
	@bash scripts/ha.sh install
ha-test:
	@bash scripts/ha.sh test
load-test:
	@bash tests/load/run.sh

.PHONY: ha-status ha-backup ha-restore-test ha-migrate ha-rollback
ha-status:
	@bash scripts/ha.sh status
ha-backup:
	@bash scripts/ha.sh backup
ha-restore-test:
	@bash scripts/ha.sh restore-test
ha-migrate:
	@bash scripts/ha.sh migrate
ha-rollback:
	@bash scripts/ha.sh rollback

.PHONY: odoo-ha-install odoo-ha-status odoo-ha-migrate odoo-ha-test odoo-ha-rollback
odoo-ha-install:
	@bash scripts/odoo-ha.sh install
odoo-ha-status:
	@bash scripts/odoo-ha.sh status
odoo-ha-migrate:
	@bash scripts/odoo-ha.sh migrate
odoo-ha-test:
	@bash scripts/odoo-ha.sh test
odoo-ha-rollback:
	@bash scripts/odoo-ha.sh rollback

.PHONY: openbao-install openbao-status openbao-backup openbao-restore-test secrets-migrate secrets-status secrets-rotate secrets-revoke secrets-rollback secrets-test secrets-observe
openbao-install:
	@bash scripts/openbao.sh install
openbao-status:
	@bash scripts/openbao.sh status
openbao-backup:
	@bash scripts/openbao.sh backup
openbao-restore-test:
	@bash scripts/openbao.sh restore-test
secrets-migrate:
	@bash scripts/secrets.sh migrate
secrets-status:
	@bash scripts/secrets.sh status
secrets-rotate:
	@bash scripts/secrets.sh rotate
secrets-revoke:
	@bash scripts/secrets.sh revoke
secrets-rollback:
	@bash scripts/secrets.sh rollback
secrets-test:
	@bash scripts/secrets.sh test
secrets-observe:
	@bash scripts/secrets.sh observe
