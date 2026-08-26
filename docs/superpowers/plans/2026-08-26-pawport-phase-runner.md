# Pawport Phase Runner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Pawport’s duplicated Agent/Mission execution wrappers with one Phase Runner, one fail-closed access guard, and one completion result without changing prompts or four-zone transitions.

**Architecture:** `adaptation_phase.py` owns phase specifications, request-bound access, Agent streaming, heartbeat, cancellation, and rolling parallel batches. `adaptation_loop.py` keeps asset tools and high-level orchestration. `MissionMode.internal_mission()` wraps the existing MissionGate lifecycle so callers cannot forget cleanup.

**Tech Stack:** Python 3.11+, asyncio, AgentScope workspace streaming, Pydantic models, pytest/pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-08-26-pawport-phase-runner-design.md`

## Global Constraints

- Do not edit `adaptation_prompts.py`.
- Do not change `CompatibilityStore.classify()`, `AssetZone`, or `RunState`.
- Preserve request phase values `triage` and `mission_repair`.
- Preserve the existing tool allowlists, three-worker concurrency, heartbeat interval, dynamic ReAct budget, retry behavior, warnings, and Chinese progress messages.
- Preserve final `completed` / `stopped_limit` behavior and summary format.
- Do not generalize the runner to ordinary Goal, Loop, or user Mission workflows.

---

### Task 1: Freeze Phase and Access Contracts

**Files:**
- Create: `src/qwenpaw/portability/adaptation_phase.py`
- Create: `tests/unit/portability/test_adaptation_phase.py`

**Interfaces:**
- Produces: `PhaseSpec`, `PhaseOutcome`, `AdaptationAccessGuard`, `TRIAGE_PHASE`, `REPAIR_PHASE`.
- `AdaptationAccessGuard.bind(session_id, context, phase, asset_key)` is a synchronous context manager.
- `AdaptationAccessGuard.current()` returns the current request binding by QwenPaw session ID and otherwise raises `PermissionError`.

- [ ] **Step 1: Write failing phase-contract tests**

```python
def test_phase_specs_preserve_prompt_and_tool_contracts():
    assert TRIAGE_PHASE.name == "triage"
    assert TRIAGE_PHASE.mutable is False
    assert TRIAGE_PHASE.prompt is triage_prompt
    assert REPAIR_PHASE.name == "mission_repair"
    assert REPAIR_PHASE.mutable is True
    assert REPAIR_PHASE.prompt is repair_prompt

def test_access_guard_cleans_binding_after_error():
    guard = AdaptationAccessGuard()
    context = object()
    with scoped_session_id("session"):
        with pytest.raises(RuntimeError):
            with guard.bind("session", context, TRIAGE_PHASE, "skills:demo"):
                assert guard.current().asset_key == "skills:demo"
                raise RuntimeError("boom")
        with pytest.raises(PermissionError):
            guard.current()
```

- [ ] **Step 2: Run RED**

Run: `.venv/bin/pytest -q tests/unit/portability/test_adaptation_phase.py`

Expected: import failure because `adaptation_phase` does not exist.

- [ ] **Step 3: Implement immutable contracts and fail-closed guard**

The guard owns the session-to-binding mapping, rejects duplicate live session IDs, resolves the current QwenPaw session through `get_current_session_id()`, validates optional expected context, and always removes its exact binding in `finally`.

- [ ] **Step 4: Run GREEN and commit**

Run: `.venv/bin/pytest -q tests/unit/portability/test_adaptation_phase.py`

Commit: `refactor(pawport): centralize phase access contracts`

---

### Task 2: Move Agent Execution into PhaseRunner

**Files:**
- Modify: `src/qwenpaw/portability/adaptation_phase.py`
- Modify: `src/qwenpaw/portability/adaptation_loop.py`
- Modify: `tests/unit/portability/test_adaptation_phase.py`
- Modify: `tests/unit/portability/test_adaptation_loop.py`

**Interfaces:**
- Produces: `PhaseRunner(workspace, context, guard, progress)`.
- `run_batch(asset_keys, phase, worker)` limits concurrency to `MAX_SPAWN_BATCH_CONCURRENCY` and isolates per-asset failures through the supplied worker.
- `run_agent(asset, phase, session_id, label)` binds access, builds the existing ephemeral AgentRequest, drains `workspace.stream_query()`, reports heartbeat, and joins cancellation before unbinding.

- [ ] **Step 1: Write failing runner tests**

Create `test_phase_runner_uses_phase_request_contract` with a fake workspace
whose `stream_query` stores its request, then assert:

```python
assert workspace.max_active_queries == MAX_SPAWN_BATCH_CONCURRENCY
assert request.request_context["portability_phase"] == "triage"
assert request.request_context["subagent_allowed_tools"] == list(
    TRIAGE_PHASE.tools,
)
```

Create `test_phase_runner_cleans_access_after_stream_failure`; make the fake
stream raise `RuntimeError("stream failed")`, run it inside
`scoped_session_id(request.session_id)`, and assert `guard.current()` raises
`PermissionError` afterward and `context._activities` is empty.

- [ ] **Step 2: Run RED**

Run: `.venv/bin/pytest -q tests/unit/portability/test_adaptation_phase.py`

Expected: missing `PhaseRunner` behavior.

- [ ] **Step 3: Implement the minimal runner**

Move, without semantic edits:

- dynamic `_iteration_budget`
- AgentRequest construction
- heartbeat loop
- cancellation join
- session binding and activity cleanup
- rolling semaphore pool

Keep `_MAX_REACT_ITERATIONS=4000`, `_HEARTBEAT_SECONDS=12`, existing ephemeral metadata, approval level, channel, and empty subagent skills.

- [ ] **Step 4: Switch triage to PhaseRunner**

Retain triage’s same-session retry only when tool calls increased and budget remains. Return `PhaseOutcome` with the current tool-limit or unfinished-staging reason. Delete `_run_agent`, `_run_bound_agent`, `_bounded_parallel`, `_triage_asset`, and `_triage_assets` after all callers move.

- [ ] **Step 5: Run GREEN and commit**

Run:

```bash
.venv/bin/pytest -q tests/unit/portability/test_adaptation_phase.py \
  tests/unit/portability/test_adaptation_loop.py -k 'triage or phase or guard'
```

Commit: `refactor(pawport): run triage through phase runner`

---

### Task 3: Replace Mission’s Three-Step Internal API

**Files:**
- Modify: `src/qwenpaw/modes/mission/__init__.py`
- Modify: `tests/unit/loop/test_mode_lifecycle.py`

**Interfaces:**
- Removes: `start_internal_mission`, `check_internal_mission`, `finish_internal_mission`.
- Produces: `MissionMode.internal_mission(session_id, loop_dir)` yielding an object with `async check() -> bool`.

- [ ] **Step 1: Write failing context-lifecycle tests**

```python
with mode.internal_mission("migration-mission", tmp_path) as mission:
    assert not await mission.check()
    prd["userStories"][0]["passes"] = True
    write_prd_json(tmp_path, prd)
    assert await mission.check()

with pytest.raises(RuntimeError):
    with mode.internal_mission("failed", tmp_path):
        raise RuntimeError("boom")
assert mode._gate._state() is None
```

- [ ] **Step 2: Run RED**

Run: `.venv/bin/pytest -q tests/unit/loop/test_mode_lifecycle.py -k internal_mission`

Expected: `internal_mission` is missing.

- [ ] **Step 3: Implement one context-managed session**

Use the existing `MissionGate`, `scoped_session_id`, and `StopAction.TERMINATE`. Activation occurs on entry and `reset_session()` occurs in `finally`, including exception paths.

- [ ] **Step 4: Delete old methods, run GREEN, and commit**

Run: `.venv/bin/pytest -q tests/unit/loop/test_mode_lifecycle.py`

Commit: `refactor(mission): scope internal mission lifecycle`

---

### Task 4: Run Repair and Completion through PhaseRunner

**Files:**
- Modify: `src/qwenpaw/portability/adaptation_phase.py`
- Modify: `src/qwenpaw/portability/adaptation_loop.py`
- Modify: `tests/unit/portability/test_adaptation_phase.py`
- Modify: `tests/unit/portability/test_adaptation_loop.py`

**Interfaces:**
- `PhaseRunner.run_repair_round(asset_keys, warnings)` executes one parallel repair attempt per asset.
- High-level repair returns `PhaseOutcome`; MissionGate remains the completion authority for the mirrored PRD.

- [ ] **Step 1: Write failing repair/outcome tests**

Add `test_repair_phase_outcome_preserves_completion_contract` by adapting the
existing two-skill parallel repair fixture. Assert:

```python
assert result.status == "completed"
assert result.asset_zones == {
    "skills:first": "migrate",
    "skills:second": "migrate",
}
assert all(
    request.request_context["portability_phase"] in {
        "triage",
        "mission_repair",
    }
    for request in workspace.requests
)
```

Keep `test_static_failure_keeps_remote_plugin_in_repair` as the exhaustion
contract and add `assert result.counts["repair"] == 1`. The existing
cross-asset assertion remains mandatory and must continue raising
`PermissionError("worker may access only its assigned asset")`.

- [ ] **Step 2: Run RED**

Run:

```bash
.venv/bin/pytest -q tests/unit/portability/test_adaptation_phase.py \
  tests/unit/portability/test_adaptation_loop.py -k 'mission or repair or outcome'
```

- [ ] **Step 3: Migrate repair orchestration**

Use `with mode.internal_mission(...) as mission`, call PhaseRunner for each pending batch, sync the existing Mission files, and translate gate acceptance or retry exhaustion into `PhaseOutcome`. Delete `_repair_asset` and the string-returning `_repair_with_mission` implementation.

- [ ] **Step 4: Centralize context authorization**

Make `ActiveAdaptationContext` resolve one guard binding. Replace string-based mutation checks with `binding.phase.mutable`; derive the expected source zone from `PhaseSpec`. Preserve the public `context.phase` and `context.active_asset_key` properties.

- [ ] **Step 5: Simplify final orchestration**

`run_adaptation_loop()` consumes triage and repair outcomes, then leaves final truth to the existing `store.complete()`, `store.finish()`, and `write_summary()` calls. Do not edit prompts or compatibility state models.

- [ ] **Step 6: Run GREEN and commit**

Run:

```bash
.venv/bin/pytest -q tests/unit/portability/test_adaptation_phase.py \
  tests/unit/portability/test_adaptation_loop.py \
  tests/unit/loop/test_mode_lifecycle.py
```

Commit: `refactor(pawport): unify mission repair completion`

---

### Task 5: Verify Frozen Behavior

**Files:**
- No production changes unless a regression introduced by Tasks 1–4 is found.

**Interfaces:**
- Consumes the stage-0 protection matrix and stage-6 design requirements.

- [ ] **Step 1: Prove prompt and state files were not modified**

Run:

```bash
git diff 01c12a50..HEAD -- \
  src/qwenpaw/portability/adaptation_prompts.py \
  src/qwenpaw/portability/compatibility.py
```

Expected: empty output.

- [ ] **Step 2: Run the full Pawport protection matrix**

Run:

```bash
.venv/bin/pytest -q tests/unit/portability \
  tests/unit/harnesses/test_codex_rollout_reader.py \
  tests/unit/harnesses/test_codex_adapter.py \
  tests/unit/harnesses/test_codex_app_server.py \
  tests/unit/app/crons/test_manager.py \
  tests/unit/app/crons/test_executor.py \
  tests/unit/plugins/test_marketplace_registry.py \
  tests/unit/plugins/test_plugin_install_target_safety.py \
  tests/unit/loop/test_mode_lifecycle.py
```

Expected: all tests pass.

- [ ] **Step 3: Run phase-scoped pre-commit**

Run: `.venv/bin/pre-commit run --from-ref 01c12a50 --to-ref HEAD`

Expected: every hook passes.

- [ ] **Step 4: Inspect size and stale symbols**

Run:

```bash
rg -n 'start_internal_mission|check_internal_mission|finish_internal_mission|_run_bound_agent|_bounded_parallel' src tests
wc -l src/qwenpaw/portability/adaptation_loop.py \
  src/qwenpaw/portability/adaptation_phase.py
git status --short
```

Expected: no stale production references, `adaptation_loop.py` is smaller than its 925-line baseline, and the worktree is clean.
