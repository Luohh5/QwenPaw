# MQTT relative TLS paths implementation plan

> **For agentic workers:** Use superpowers:subagent-driven-development. Separate agents own production and test code.

**Goal:** Preserve MQTT TLS certificate lookup against the backend working directory after channel subprocess migration.

**Architecture:** Add MQTT's three TLS path fields to the existing official execution-path mapping. Reuse detached `absolute_execution_paths` projection before subprocess launch; preserve original configuration, empty values, absolute paths, symlink semantics and platform TLS validation.

**Spec:** User-approved minimal proposal in this conversation: one mapping entry for `tls_ca_certs`, `tls_certfile`, `tls_keyfile`; targeted path tests and actual local TLS Broker/subprocess verification.

## Global Constraints

- Use current `codex/channel-pluginization` checkout. Preserve all existing dirty changes; no commit/push, package installation or external MQTT services.
- Production code and tests have separate subagent owners. Do not weaken assertions; only run affected tests.
- Import this checkout's `src` and `packages/qwenpawmail-mcp/src`. A dependency-complete existing sibling venv may execute tests; its editable code must not replace this checkout.
- Host remains SDK-free. Do not change MQTT platform logic, subprocess cwd, TLS checks, stored config, or copy certificate/private-key files into plugins.

## Review Focus

- Relative paths use original backend cwd, distinct from workspace, state root and plugin cwd.
- Each of the three TLS files must be projected, with original input unmodified.
- Absolute paths, `~`, `None`, and empty strings retain existing helper semantics.
- A real child MQTT client must establish mutually authenticated local TLS and send data; incorrect CA must still fail.

### Task 1: Independent tests and RED

**Files:** New focused isolated suite under `tests/fixtures/channel_plugins/` and wrapper under `tests/unit/plugins/channels/`, or an equally small existing clean test extension. Do not modify pre-existing dirty files.

- [x] Reuse existing `g2_lifecycle_suite.certificate_files`, `g2_mqtt_broker.Broker`, and official MQTT/runtime helpers. Inspect their contracts before using them.
- [x] Cover all three path fields with relative, absolute, home-expanded and empty values, distinct cwd/workspace/state roots, plus unchanged original input.
- [x] Add a real child-process/local TLS Broker regression using relative CA/client certificate/private-key paths. Assert projected launch paths, successful client certificate authentication and publication. Do not mock TLS or installed SDK/Host decisions.
- [x] Run RED against unchanged production; distinguish expected path/assertion failures from environment errors. Freeze assertions and report evidence before source edits.
- [x] After source fix, run GREEN and only affected path/MQTT TLS tests, including existing absolute-path success and wrong-CA rejection where available. Record commands, counts, hashes and simulation limits.

### Task 2: Separate production owner, one mapping entry

**File:** `src/qwenpaw/plugins/official_execution_context.py`.

- [x] Wait for independent RED.
- [x] Add `"mqtt": ("tls_ca_certs", "tls_certfile", "tls_keyfile"),` to `_PATH_FIELDS`; no new helper or other behavior change.
- [x] Run scoped syntax/format/lint/diff checks. Never modify tests. Record source hash and exact diff.

### Task 3: Verify and review

- [x] Confirm real TLS/child and path regression evidence on frozen tests and final source.
- [x] Independently review spec compliance and code/test quality; resolve actionable findings through the correct owner.
- [x] Report the narrow change, focused checks and local-only TLS scope. Leave changes in this checkout.

## Verification outcome

Independent frozen RED: 3 failed / 3 passed. Fixed direct affected selection: 13 passed (6 new cases, 2 existing TLS cases, 5 projection policy cases); isolated wrapper also passed. Actual client/worker TLS and local Broker client-certificate authentication/publication exercised; wrong CA still rejected. Syntax, flake8, new-test Black and scoped diff checks pass. Source Black only requests the same pre-existing docstring blank line as HEAD, intentionally outside the one-mapping change. Independent spec and quality review passed without findings. No external broker or full repository validation claimed.
