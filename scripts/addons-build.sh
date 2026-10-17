#!/usr/bin/env bash
source "$(dirname -- "$0")/common.sh"
need docker
[[ -n ${ADDONS_REVISION:-} ]] || die 'Set ADDONS_REVISION to an immutable addon commit/tag; never use latest'
[[ $ADDONS_REVISION =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]+$ ]] || die 'Invalid ADDONS_REVISION'
image="odoo/odoo-addons:19.0-$ADDONS_REVISION"
docker build --build-arg "ODOO_BASE=$(version odoo_image)" --tag "$image" -f "$ROOT/addons/Dockerfile" "$ROOT/addons"
echo "Built $image. Load it with kind load docker-image --name odoo-dev $image."
echo 'Update dev Odoo image values, commit/publish, back up, then explicitly upgrade the relevant modules.'
