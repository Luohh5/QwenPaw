# Shared dependency visibility implementation plan

> **For agentic workers:** Use separate production and test authors. Follow TDD and independently review the final diff.

**Goal:** Fix user-site dependency visibility and host-consumer audit false failures while retaining isolated, lazy plugin execution.

**Architecture:** Keep the same interpreter and `-I`. Resolve trusted active installation roots once in the common execution layer, and use them from normal Channel and Python-plugin preparation. Host import pins and complete installed inventories have different purposes; validate each consumer in its own environment without copying host libraries into a runtime worker.

**Spec:** The user-approved proposal in this conversation: unified trusted paths, separate environment audits, lazy imports, bounded runtime metadata, simulation and memory verification.

## Global constraints

- Efficient, concise code; production and test code have different authors.
- Preserve explicit raw `PluginLaunchSpec` isolation: arbitrary parent paths and plugin roots are never inherited automatically.
- Keep `-I`, lifecycle, cancellation, custody, conflict/origin checks, writer locks and repair markers.
- Additional paths do not execute `.pth`; standard library and trusted support priority are preserved.
- Readable shared roots do not become writable installation targets. Frozen deployments use their validated manifest, never the desktop parent's user-site.
- No dependency installation, live bot connections, changes to real workspace configurations, backend restart, commit or push.
- Preserve existing editable-resource fix and unrelated user edits.

## Review focus

- Disabled user-site and virtual environments must not gain unrelated host roots.
- Duplicate package origins and changed loaded versions remain detected.
- Unloaded packages are not promoted to imported host consumers.
- Withdrawn declarations retain valid host import pins and no stale inventory bypass.
- Additional metadata never causes unrelated SDK/model imports or new resident services.

## Task 1: Common trusted paths

**Files:** `src/qwenpaw/plugins/execution/environment.py` (new); `execution/worker.py`; `plugins/channel_bootstrap.py`; `plugins/loader.py`.

**Interface:** `shared_dependency_paths() -> tuple[Path, ...]`: current host's trusted active installation directories in import order, normalized and deduplicated. Caller-owned managed paths remain explicit. Frozen branches retain their manifest paths.

- [x] Independent tests reproduce missing enabled user-site, disabled-site exclusion, no arbitrary parent/candidate paths, no `.pth` execution and standard-library priority.
- [x] Record RED before production edits.
- [x] Implement the common resolver and connect Channel and ordinary Python preparation. Keep read roots separate from install-target selection.
- [x] Verify actual isolated workers can find a user-site fixture without importing unused fixture modules.

## Task 2: Consumer-specific audit evidence

**Files:** `plugins/dependency_gate.py`; `plugins/execution/dependencies.py`; only necessary changes to `dependency_usage.py`/`python_backend.py`.

**Interface:** Existing public Gate APIs remain compatible. Host requirements stay rooted in observed imports; a fresh metadata-only inventory from their attested roots completes transitive dependency checks. Never infer the host's inventory solely from the candidate child's inventory. Any reusable inventory is scoped to one decision, not a permanent cache or a full inventory per plugin.

- [x] Tests reproduce a loaded host dependency needing an unimported user-site package, even when the candidate has no such requirement.
- [x] Record RED and test missing/changed/ambiguous origins, marker context, withdrawn host pins and shared-write protection.
- [x] Implement the smallest consumer-context correction, preserving existing imported evidence and fail-closed behavior.
- [x] Run generic Gate, usage and host-observation regression suites.

## Task 3: Bounded runtime metadata and acceptance

**Files:** `plugins/execution/worker.py`; relevant new tests and isolated fixture suites.

**Interface:** Runtime handshakes retain only the required dependency closure's version entries; complete audits remain available to the host and short-lived inspection. A worker must not import unrelated packages to establish metadata evidence.

- [x] Test required versions remain correct and unrelated packages are absent from runtime version state and imported modules.
- [x] Implement bounded retained state without weakening startup evidence, protocol or origin validation.
- [x] Compare paired simulated workers' steady memory, import sets and process count. Report observed values; do not claim absolute zero overhead.
- [x] Verify C4/C5, Feishu and DingTalk simulated workflows and affected generic/plugin tests; identify any pre-existing failures separately.
- [x] Independent final review, fresh checks and concise user-facing report. Leave changes uncommitted.
