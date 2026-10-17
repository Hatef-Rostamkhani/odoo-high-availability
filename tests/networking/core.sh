#!/usr/bin/env bash
source "$(dirname -- "$0")/../../scripts/common.sh"
assert_cluster
cilium --context "$CONTEXT" connectivity test --ip-families ipv4 \
  --test-namespace odoo-core-connectivity --test-concurrency 1 --timeout 5m \
  --test '(all-ingress-deny-knp|all-egress-deny-knp|client-egress-knp|echo-ingress-knp)/(pod-to-pod|pod-to-service)' \
  --junit-file "$ROOT/evidence/cilium-core-connectivity.xml" 2>&1 | tee "$ROOT/.state/cilium-core-output.log"
python3 - "$ROOT/evidence/cilium-core-connectivity.xml" "$ROOT/.state/cilium-core-output.log" <<'PY'
import re,sys,xml.etree.ElementTree as ET
from pathlib import Path
summary=re.search(r'All (\d+) tests \((\d+) actions\) successful',Path(sys.argv[2]).read_text())
assert summary and int(summary[1])>=4 and int(summary[2])>0, 'Official subset did not execute real actions'
root=ET.parse(sys.argv[1]).getroot()
cases={case.attrib['name']:case for case in root.iter('testcase')}
required=('all-ingress-deny-knp','all-egress-deny-knp','client-egress-knp','echo-ingress-knp')
for name in required:
    assert name in cases, 'Expected official test missing: '+name
    assert cases[name].find('skipped') is None, 'Expected official test skipped: '+name
    assert cases[name].find('failure') is None and cases[name].find('error') is None, 'Official test failed: '+name
print('PASS: four official policy cases executed internal Pod/Service scenarios successfully; full suite is a separate gate')
PY
