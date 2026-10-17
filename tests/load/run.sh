#!/usr/bin/env bash
source "$(dirname -- "$0")/../../scripts/common.sh"
assert_cluster
[[ ${ENABLE_LOAD:-} == true ]] || die 'Optional: make load-test ENABLE_LOAD=true after starting dashboards'
[[ ${K6_IMAGE:-} == *@sha256:* ]] || die 'Set K6_IMAGE to a reviewed grafana/k6 image digest'
docker run --rm --network host --user "$(id -u):$(id -g)" --cap-drop ALL --security-opt no-new-privileges \
  -v "$ROOT/tests/load/smoke.js:/scripts/smoke.js:ro" "$K6_IMAGE" run /scripts/smoke.js
