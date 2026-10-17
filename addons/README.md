Place custom addon directories here, each with an `__manifest__.py`.
Addon code is ignored by default to avoid accidentally publishing proprietary
modules. Manage it in an authorized separate repository, or deliberately adjust
the ignore rule for approved code. Never place secrets in this build context.

Build with `make addons-build ADDONS_REVISION=<immutable-addon-commit>`.
Use a fresh image tag for each build. Load it into Kind, update the dev image
value, commit and publish, back up, and run an explicit module upgrade.

Licensed Enterprise addons require an appropriate license and authorized source.
