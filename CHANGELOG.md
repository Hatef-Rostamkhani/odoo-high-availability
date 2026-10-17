# Changelog

## Unreleased

- Pin standalone PostgreSQL 17.11 and record the remaining image security findings;
  block tagged releases on HIGH/CRITICAL findings.
- Add a fresh-install basic profile with its own Kind cluster and private state.
- Make GitOps defaults start with standalone PostgreSQL and synthetic CRM data;
  retain explicit HA and rotating-credential profiles.
- Validate pod recovery and coordinated database/filestore restore in isolation.
- Add formatting checks, shared operations, generated-runtime drift checks and
  failure tests for migration rollback, interruption, backup errors and redaction.
- Add GitHub CI/release workflows, community documentation and MIT licensing for
  original project code. Keep third-party terms separate and fetch the Crunchy
  packaged chart from its upstream registry instead of redistributing it.

No tagged public release has been verified yet. Version 0.1.0 is the first planned
release; publishing a tag is a separate action after release gates pass.
