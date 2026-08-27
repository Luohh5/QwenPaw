# Portability Import UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a five-state, internationalized Console workflow that detects Codex/Qoder, lets users select conversations and portable assets, and runs the existing migration core with reconnectable progress.

**Architecture:** Mount a `portability_import_router` at `/api/agents/{agentId}/portability/imports`; the name and prefix deliberately avoid the existing agent-scoped router terminology. A small persisted job layer owns scans, selections, snapshots and SSE events, while Providers, plans, adaptation Mission, Importer and Doctor remain the single migration implementation.

**Tech Stack:** Python 3.10+, FastAPI, Pydantic v2, asyncio/SSE, React 18, TypeScript, Ant Design, Zustand, i18next, pytest, Vitest.

**Spec:** Approved design in the Codex task on 2026-08-27.

## Global Constraints

- The only user-visible asset states are `pending`, `repairing`, `not_needed`, `failed`, and `succeeded`.
- A succeeded but disabled asset remains `succeeded`; `enabled=false` and its tooltip explain the review requirement.
- Codex and Qoder scans may run concurrently, but writes are serialized by the existing workspace migration lock.
- The browser never receives conversation content, MCP secrets, environment values, headers, plugin source files, or Agent prompts.
- The router and task are pinned to the explicit URL `agentId`; changing the selected Agent does not retarget a running job.
- Existing Provider, adaptation Mission, Importer rollback, Cron review, MCP credential and Doctor behavior must not be duplicated or weakened.
- All seven Console locale files (`en`, `zh`, `ja`, `ru`, `id`, `vi`, `pt-BR`) receive the same key set.
- Every task follows red-green-refactor and ends with its focused tests and a separate commit.

---

### Task 1: Selection Contract and Dependency Filtering

**Files:**
- Create: `src/qwenpaw/portability/selection.py`
- Modify: `src/qwenpaw/portability/models.py`
- Modify: `src/qwenpaw/portability/import_planning.py`
- Test: `tests/unit/portability/test_selection.py`

**Interfaces:**
- Produces: `ImportSelection`, `ImportAssetResult`, `select_inventory(inventory, selection) -> ProviderInventory`, and `ProviderImportService.apply_selection(plan_id, selection, ...)`.
- Preserves: the full inventory fingerprint is checked before selection; only the selected copy reaches `ProviderImportService._apply`.

- [ ] **Step 1: Write failing tests** proving session all-or-none selection, individual Memory/Cron/Skill/MCP/Plugin selection, hidden Marketplace inclusion, plugin-owned MCP parent inclusion, heartbeat/session dependency validation, unknown IDs rejection, and no mutation of the original inventory.
- [ ] **Step 2: Verify RED** with `.venv/bin/pytest -q tests/unit/portability/test_selection.py`; failures must be missing models/functions.
- [ ] **Step 3: Implement the minimal pure filter** using a table from UI asset type to `ProviderInventory` field and one dependency pass; do not create per-provider selectors. Add `apply_selection()` beside `apply_plan()` so it rereads the full source, verifies the stored fingerprint, filters a deep copy, then calls `_execute_plan()`.
- [ ] **Step 4: Verify GREEN** with `.venv/bin/pytest -q tests/unit/portability/test_selection.py tests/unit/portability/test_planner.py`.
- [ ] **Step 5: Commit** as `feat(pawport): add selective import contract`.

### Task 2: Read-Only Five-State Projection

**Files:**
- Create: `src/qwenpaw/portability/import_status.py`
- Test: `tests/unit/portability/test_import_status.py`

**Interfaces:**
- Produces: `project_asset_results(plan, selection, manifest=None, receipt=None) -> list[ImportAssetResult]`.
- Reuses: the existing plan, compatibility manifest and receipt as state sources; it does not alter Provider, Mission, Importer or Receipt behavior.

- [ ] **Step 1: Write failing tests** for exactly five public states, disabled-success tooltip data, semantic discard reasons, already-present actions, unresolved repair items, native-install failure and successful materialization.
- [ ] **Step 2: Verify RED** with `.venv/bin/pytest -q tests/unit/portability/test_import_status.py`.
- [ ] **Step 3: Implement one table-driven projector**; do not parse warning text when a structured plan action, zone or receipt list is available.
- [ ] **Step 4: Verify GREEN** with the focused test plus existing planner, compatibility and importer tests.
- [ ] **Step 5: Commit** as `feat(pawport): project import asset status`.

### Task 3: Persisted Portability Import Jobs

**Files:**
- Create: `src/qwenpaw/portability/import_jobs.py`
- Test: `tests/unit/portability/test_import_jobs.py`

**Interfaces:**
- Produces: `PortabilityImportJobManager` with `create`, `snapshot`, `start`, and `subscribe`.
- Persists: UI-safe snapshots under `<workspace>/.qwenpaw/imports/jobs/<job-id>.json`; plans and receipts remain in their existing locations.

- [ ] **Step 1: Write failing tests** for concurrent Codex/Qoder scan, `awaiting_selection`, one active run per Agent, explicit Agent pinning, atomic owner-only snapshots, monotonic event sequence, reconnect replay, partial provider failure, and interrupted-state recovery.
- [ ] **Step 2: Verify RED** with `.venv/bin/pytest -q tests/unit/portability/test_import_jobs.py`.
- [ ] **Step 3: Implement the minimal manager** around `ProviderImportService.plan_from` and `apply_selection`, an asyncio task, one bounded event buffer, and atomic JSON snapshots.
- [ ] **Step 4: Verify GREEN** with the focused test plus `tests/unit/portability`.
- [ ] **Step 5: Commit** as `feat(pawport): add persisted import jobs`.

### Task 4: Portability Import HTTP Router

**Files:**
- Create: `src/qwenpaw/app/routers/portability_imports.py`
- Modify: `src/qwenpaw/app/routers/agent_scoped.py`
- Test: `tests/unit/app/routers/test_portability_imports_router.py`

**Interfaces:**
- Produces:
  - `GET /portability/imports/sources`
  - `POST /portability/imports/jobs`
  - `GET /portability/imports/jobs/{job_id}`
  - `GET /portability/imports/jobs/{job_id}/events`
  - `POST /portability/imports/jobs/{job_id}/start`
- Consumes: the current workspace from `get_agent_for_request` and the Task 3 manager.

- [ ] **Step 1: Write failing route tests** for supported-source probing, sanitized plan summaries, selection submission, SSE headers/events, missing/wrong Agent job rejection, unknown assets, source change requiring rescan, and loopback-only access.
- [ ] **Step 2: Verify RED** with the focused router test.
- [ ] **Step 3: Implement the router** named `portability_import_router`; mount it only through `create_agent_scoped_router`, never the global root router.
- [ ] **Step 4: Verify GREEN** with `.venv/bin/pytest -q tests/unit/app/routers/test_portability_imports_router.py tests/unit/app/routers/test_agents_router.py tests/unit/portability`.
- [ ] **Step 5: Commit** as `feat(pawport): expose import job api`.

### Task 5: Console API, Store and Five-State Projection

**Files:**
- Create: `console/src/api/types/import.ts`
- Create: `console/src/api/modules/import.ts`
- Modify: `console/src/api/types/index.ts`
- Modify: `console/src/api/index.ts`
- Create: `console/src/pages/Import/useImportJob.ts`
- Test: `console/src/api/modules/import.test.ts`
- Test: `console/src/pages/Import/useImportJob.test.ts`

**Interfaces:**
- Produces: `portabilityImportApi` and `useImportJob` pinned to the Agent captured when the job starts.
- Maps backend states to exactly five UI states; `enabled=false` changes tooltip metadata only.

- [ ] **Step 1: Write failing Vitest tests** for explicit Agent URLs, source/job/start requests, SSE reconnect sequence, default-all selection, five-state projection, disabled-success tooltip, stale event rejection, and Agent-switch isolation.
- [ ] **Step 2: Verify RED** with `npm --prefix console run test:run -- src/api/modules/import.test.ts src/pages/Import/useImportJob.test.ts`.
- [ ] **Step 3: Implement the smallest API and hook**; keep the server snapshot authoritative and SSE as delta-only enhancement.
- [ ] **Step 4: Verify GREEN** with the same command and `npm --prefix console exec tsc -b --noEmit`.
- [ ] **Step 5: Commit** as `feat(console): add portability import client`.

### Task 6: Import Page, Navigation and Internationalization

**Files:**
- Create: `console/src/pages/Import/index.tsx`
- Create: `console/src/pages/Import/index.module.less`
- Create: `console/src/pages/Import/index.test.tsx`
- Modify: `console/src/layouts/registry/builtinMenu.ts`
- Modify: `console/src/layouts/registry/builtinRoutes.tsx`
- Modify: `console/src/layouts/registry/capabilities.ts`
- Modify: `console/src/locales/{en,zh,ja,ru,id,vi,pt-BR}.json`
- Modify: `console/src/locales/thirdPartyAgentLocales.test.ts`

**Interfaces:**
- Produces: `/imports` page below Apps and above Control.
- Displays: source selection, conversation row, grouped Memory/Cron/Skill/MCP/Plugin selection, overall progress, five statuses, tooltips, log disclosure and completion actions.

- [ ] **Step 1: Write failing UI tests** for source detection, multi-select, default-all inventory, group select, dependency lock, start button, five statuses, disabled-success tooltip, not-needed reason, failure hint, progress, reconnect and completion navigation.
- [ ] **Step 2: Add a failing locale parity assertion** requiring `nav.import` and the complete `portabilityImport` key tree in all seven files.
- [ ] **Step 3: Verify RED** with the page and locale tests.
- [ ] **Step 4: Implement one responsive page** using existing Ant Design components; avoid a custom component framework and keep status rendering table-driven.
- [ ] **Step 5: Add accurate translations** in all seven locale files; no English fallback is accepted for the new page.
- [ ] **Step 6: Verify GREEN** with focused Vitest, `npm --prefix console exec tsc -b --noEmit`, and `npm --prefix console exec prettier --check` for changed Console files.
- [ ] **Step 7: Commit** as `feat(console): add import workflow`.

### Task 7: Mini-Source End-to-End Verification

**Files:**
- Modify only if a real defect is found: implementation files from Tasks 1-6.
- Test fixtures: `tests/fixtures/portability/codex-mini`, `tests/fixtures/portability/qoder-mini`, `tests/fixtures/portability/qoder-user-data-mini`.

**Interfaces:**
- Verifies the complete API workflow against real provider readers without touching the user's actual Codex/Qoder homes.

- [ ] **Step 1: Run a temporary Agent workspace through source probe, scan, selection and import** using explicit `source_home`; point Qoder user data through `QODER_USER_DATA_HOME`.
- [ ] **Step 2: Assert conversations, selected assets, plugin/MCP dependencies, receipts, five terminal states and Doctor results** from persisted files and public API snapshots.
- [ ] **Step 3: Run backend regression**: `.venv/bin/pytest -q tests/unit/portability tests/unit/app/routers/test_portability_imports_router.py`.
- [ ] **Step 4: Run frontend regression**: focused Import tests, locale tests, TypeScript and Prettier.
- [ ] **Step 5: Run pre-commit on every changed file** and fix only failures caused by this feature.
- [ ] **Step 6: Record final diff metrics** and confirm the implementation reuses the existing migration core rather than duplicating Provider/Importer/Mission logic.
- [ ] **Step 7: Commit** as `test(pawport): verify import ui with mini sources`.
