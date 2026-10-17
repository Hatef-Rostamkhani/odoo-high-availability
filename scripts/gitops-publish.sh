#!/usr/bin/env bash
source "$(dirname -- "$0")/common.sh"
need git
git -C "$ROOT" rev-parse --verify HEAD >/dev/null 2>&1 || die 'Commit the reviewed project first: git add . && git commit -m "Build Odoo development platform"'
repository="$ROOT/.state/git/odoo-platform.git"
mkdir -p "$ROOT/.state/git"
chmod 755 "$ROOT/.state/git"
if [[ ! -d $repository ]]; then git init --bare --initial-branch=main "$repository"; fi
exec 9> "$ROOT/.state/publish.lock"
flock -x 9
git -C "$ROOT" push "$repository" HEAD:refs/heads/main
git --git-dir="$repository" update-server-info
chmod -R a+rX "$repository"
echo "Published $(git -C "$ROOT" rev-parse HEAD) to read-only local Git"
if [[ -n $(git -C "$ROOT" status --porcelain) ]]; then echo 'Uncommitted changes were not published.'; fi
