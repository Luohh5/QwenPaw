# Supervisor dependency maintenance implementation plan

> **For agentic workers:** Use superpowers:subagent-driven-development. Production code and tests have different owners.

**Goal:** Prevent dependency upgrades from bypassing or competing with Supervisor.

**Architecture:** Standalone launches retain automatic upgrades. Supervisor launches use the existing retained offline worker and explicit service-manager stop/start. Keep all dependency transaction and admission safeguards.

**Tech Stack:** Python, Click, pytest, Supervisor INI templates.

**Spec:** User-approved minimal proposal in the current conversation: detect restart ownership; managed launches retain a waiting_restart operation without automatic shutdown/worker/restart; provide a usable official Docker maintenance path; preserve standalone behavior.

## Global Constraints

- Work in the existing `codex/channel-pluginization` checkout and preserve all existing dirty changes. No commit, push, real package upgrades, Docker installation, or real bot calls.
- Keep production code efficient and concise. Functional and test code must be written by separate subagents. Tests must not weaken assertions to pass.
- Run only affected tests. Use the current repository `src`, not the venv editable installation pointing at a sibling checkout.
- Do not disable Supervisor autorestart or reinterpret crash exit codes. A container marker alone does not establish restart ownership.
- Never mutate dependency files while readers are live. Preserve approval, retained plans, rollback, repair markers, and missing-package protection.

## Review Focus

- Supervisor process metadata must suppress standalone restart even if stale launcher state exists.
- Reload and unmanaged ASGI paths must remain manual; standalone CLI remains automatic.
- Managed apply must persist instructions and plan without scheduling a signal or detached worker.
- Manual worker success, safe failure, and repair-required failure must not start a backend.
- Official and Creator templates must provide the same local-only control socket and usable explicit stop/worker/start instructions.

### Task 1: Regression evidence, independent test owner

**Files:** New focused CLI/plugin tests or a small isolated suite/wrapper using existing test conventions. Do not edit production files or existing dirty tests.

**Interfaces:** Exercise actual `app_cmd.app` launch configuration, `schedule_maintenance(manager, directory, operation)`, and `dependency_worker.run(directory)`; substitute only server execution, OS shutdown/Popen, and dangerous package transaction boundaries. Assert persisted state and operation outcomes.

- [x] Add focused tests for Supervisor-owned launch and repeated invocation clearing stale restart state; preserve standalone registration, reload behavior, and container-only standalone behavior.
- [x] Reproduce managed scheduling safety using the real launch-selected restart configuration, saved `restart=None`, `waiting_restart`, retained instructions, and no signal/worker spawn. Cover automatic standalone scheduling.
- [x] Cover the existing worker's manual success, safe rollback failure, repair-required failure and standalone restart behavior using real operation/transaction paths where practical, without actual environment modifications.
- [x] Check usable local Supervisor socket/RPC/control configuration for both templates with an INI parser. Prefer behavior to source-string assertions; report absence of real Supervisor/Docker verification honestly.
- [x] Run RED against unchanged production code; report exact expected failures and baseline passes. Freeze assertions, notify controller, then wait for implementation.
- [x] Run GREEN and only affected CLI/dependency upgrade tests after source changes. Report command, counts, limitations, and test/source hashes.

### Task 2: Managed restart ownership and Docker maintenance, production owner

**Files:** `src/qwenpaw/cli/app_cmd.py`, `src/qwenpaw/plugins/dependency_upgrade.py`, `deploy/config/supervisord.conf.template`, `plugins/apps/qwenpaw-creator/config/supervisord.conf.template`; concise maintenance runbook if needed. Touch worker code only if needed for a real uncovered safety gap.

**Interfaces:** Existing `_dependency_restart` is a launcher-owned optional command/cwd dictionary. `_app.py` only registers shutdown/restart if this is non-None. Existing worker skips backend spawn when the persisted operation has `restart=None`.

- [x] Read the focused tests and RED report before writing production changes.
- [x] Reset launch restart state on every invocation; grant standalone restart only for non-reload launches not owned by Supervisor (use native Supervisor metadata, no new dependency).
- [x] Reuse existing manual maintenance branch for managed launch. Keep the operation, approval, staged source, and dependency plan; no automatic worker or shutdown. Emit actionable instructions to stop through the service manager before running the retained absolute worker, then restart through the owner only once maintenance safely completes.
- [x] Add `/run/qwenpaw-supervisor.sock` local control socket with restrictive permissions, standard Supervisor RPC interface and matching `supervisorctl` serverurl to both templates. Keep existing process restart policy unchanged.
- [x] Document explicit official Docker `supervisorctl -c /etc/supervisor/conf.d/supervisord.conf stop app`, retained worker, and `start app` sequence. Warn to keep app stopped if repair is required and recover using retained worker, never by editing operation.json or running pip behind the transaction.
- [x] Verify only changed Python/config files and report succinctly; do not modify tests or commit.

### Task 3: Verification and independent review

- [x] Inspect final source/test diff against this task's baseline, including template parity and instructions.
- [x] Confirm new regressions and affected existing tests pass, with no weakened assertions or unrelated changes.
- [x] Obtain independent spec and quality review of this task's diff and evidence. Resolve actionable findings via the appropriate owner.
- [x] Report implemented behavior, focused test counts, and the offline Docker upgrade tradeoff. Do not claim full Docker or real upgrade end-to-end validation.

## Verification outcome

New regressions: 18 passed. Existing affected launcher/dependency-upgrade wrappers: 3 passed, 2 Windows-only skipped. `test_local_runtime_init.py` could not collect because the test environment lacks `questionary`. No full Docker/Supervisor or real package-upgrade validation was performed. Independent review evidence is retained with the plan-scoped SDD artifacts.
