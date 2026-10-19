# Test Suite Trimming Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Track steps with checkboxes.

**Goal:** Remove audited redundant tests and retire tests of the unused channel lifecycle prototype while preserving current plugin/channel regressions.

**Architecture:** Keep production behavior unchanged. First apply the reviewed deletion manifest, then move retained endpoint/retry coverage onto current shared-channel interfaces before removing the old controller harness.

**Tech Stack:** Python/pytest, TypeScript/Vitest.

**Spec:** User-approved audit: `/tmp/qwenpaw-test-trim-audit-20261019/summary.json` and `historical-legacy-reachability.json` in the same directory.

## Global Constraints

- Work in the existing `codex/channel-pluginization` checkout; preserve unrelated changes. No commit, push, branch or index operations.
- Exclude every PR #7565 protected path in the audit inventory; retain their exact initial bytes.
- Test authors edit only tests. Any required production fix goes to a separate author; do not weaken retained assertions, add skips, enlarge timeouts or hide collection errors.
- Retain installation, enable/disable, native receive/send, worker isolation, routing/ACL, stop failures, dependency conflicts and rollback coverage.
- Prefer deletion and small existing fixtures over new test infrastructure. Do not add development-only tests to validate deletion.
- Mock platform interactions only; no real bot credentials, network dependency installs or native Desktop/Docker installation.

## Review Focus

- Missing payload dispatch: compare default collected IDs and explicitly run affected isolated wrappers.
- Lost current endpoint/retry behavior: retain the registry, identity, readiness and retry assertions on current interfaces.
- Residual references: check deleted payloads/helpers against tests, scripts and CI.
- Broken source isolation: retain SDK import guards and run the real Feishu/DingTalk worker suites.
- Upgrade/stop regressions: run existing installation, lifecycle, dependency and recovery tests without changing protected files.

### Task 1: Apply confirmed test deletions

**Files:** Exact whole-file/range list in `summary.json.manifest` (including optional baseline microtests); no other test families.

**Interfaces:** Consume the frozen audit snapshot `501c01e33`; produce a smaller suite with the same retained behavioral assertions and canonical payload dispatch.

- [x] Capture initial status, protected hashes and default collection before editing.
- [x] Delete the audited whole files/ranges; remove imports/helpers made unused by those deletions. Keep unique platform behavior and canonical wrappers.
- [x] Run AST/format checks and the changed small test groups; record exact diff and evidence.
- [x] Independent task review checks every manifest entry, protected paths, remaining dispatch and retained assertions.

### Task 2: Retire unused lifecycle prototype tests

**Files:** `tests/unit/channel_isolation/test_ch_0_004_{lifecycle,outbound,core_lifecycle}.py`, `test_ch_0_005_reliability.py`; shared helpers only if no remaining users.

**Interfaces:** Consume `historical-legacy-reachability.json`; preserve `CoreEndpointRegistry` behavior through current `SharedEndpointAuthority`, plus current `RetryPolicy/events_for_retry` and DTO checks.

- [x] Read the current shared endpoint service and map every retained registry/retry assertion before editing.
- [x] Remove the two unused controller-only suites and audited old client/controller ranges.
- [x] Adapt retained registry setup to the current process observation/authority model; remove obsolete local setup. Retain meaningful identity, routing, degraded/recovery and stale-generation checks.
- [x] Separate the mixed all-invalid batch case from the unused Inbox simulation, retaining its applicable DTO/ACK rejection behavior.
- [x] Align the existing registered-channel enable test setup with production `defer_process_channels=True`; preserve all assertions and cleanup (independent diagnosis in workspace).
- [x] Run all remaining channel-isolation tests and review the task diff independently.

### Task 3: Verify the final combination

**Files:** Existing tests only; any CLI invocation/CI paths changed solely to follow removed duplicate entries.

**Interfaces:** Consume both reviewed changes; produce measured deletion totals and fresh regression evidence.

- [x] Default collection succeeds; every removed collected ID is explained by the approved deletions or mapped retained cases.
- [x] Run complete Feishu/DingTalk worker wrappers serially; run affected QQ/Matrix/WeCom/Voice/Telegram canonical entries.
- [x] Run relevant public plugin lifecycle, dependency recovery and release-installation checks; do not replay unrelated suites without cause.
- [x] Run changed frontend tests; if retained mocks use obsolete QR/store callback contracts, update only fixture setup to current public contracts and preserve assertions. Run scoped lint/diff checks; verify protected hashes and unchanged production sources.
- [x] Independent final review; report exact net reduction, verification and any concrete limitations.

Verification outcome: both trimming tasks and independent reviews complete. C4 shared49 pass; complete framework wrapper has one retained DingTalk abnormal-stop retry cleanup failure, also reproduced outside sandbox. See .superpowers/sdd/2026-10-19-test-suite-trimming/final-validation.md. No production patch, skip, relaxed assertion, commit or push.
