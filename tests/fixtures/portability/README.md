# Pawport refactor baseline

This directory is the deterministic behavior baseline for the Pawport
refactor. The mini homes contain only synthetic data and must never depend on
a developer's real Codex, Qoder, or QwenPaw directories.

## Golden fixtures

- `codex-mini`: one root session, one Guardian session, one standalone Skill,
  one memory scope, and one content plugin that owns a Skill and MCP server.
- `qoder-mini`: one visible session, one internal tool-only trace, one
  standalone Skill, one project memory, one custom plugin, and two MCP
  definitions.
- `qoder-user-data-mini`: one active Qoder scheduled task.
- `golden/*-inventory.json`: the stable, user-visible normalized provider
  result. Absolute paths, timestamps, warnings, and random IDs are excluded.
- `golden/four-zone-summary.md`: the reviewed summary contract for all four
  compatibility zones and `stopped_limit`.

Golden files describe intended public behavior, not implementation details.
Update them only when a Pawport behavior change is deliberate and reviewed.

## Protection matrix

| Contract | Primary test |
| --- | --- |
| Codex/Qoder normalized inventory | `test_phase0_contracts.py` |
| Explicit Codex source is local-only | `test_migration_providers.py` |
| Guardian and automation sessions are excluded structurally | `test_codex_rollout_reader.py`, `test_migration_providers.py` |
| Qoder visible sessions and internal traces are separated | `test_qoder_sessions.py` |
| Four-zone transitions require a current passing native test | `test_compatibility.py` |
| Parallel Agent triage and Mission repair remain isolated | `test_adaptation_loop.py` |
| Generated plugin IDs do not follow display names | `test_phase0_contracts.py` |
| PluginApi calls and handler signatures match real registration | `test_adaptation_loop.py` |
| MCP credentials fail closed or enter encrypted bindings | `test_compatibility.py`, `test_importer.py` |
| Imported Cron jobs remain review-gated | `test_importer.py`, `test_doctor_scheduled_tasks.py` |
| Late import failure rolls back every asset writer | `test_importer.py` |
| Old Plan and Receipt fields remain loadable | `test_phase0_contracts.py` |
| Archive, source fingerprint, and SQLite sidecars remain bounded | `test_archive.py`, `test_planner.py`, `test_codex_schedules.py` |

## Baseline commands

Run the deterministic Pawport suite before and after each refactor step:

```bash
.venv/bin/pytest -q tests/unit/portability \
  tests/unit/harnesses/test_codex_rollout_reader.py \
  tests/unit/harnesses/test_codex_adapter.py \
  tests/unit/harnesses/test_codex_app_server.py \
  tests/unit/app/crons/test_manager.py \
  tests/unit/app/crons/test_executor.py \
  tests/unit/plugins/test_marketplace_registry.py \
  tests/unit/plugins/test_plugin_install_target_safety.py
```

Then run the repository checks:

```bash
.venv/bin/pre-commit run --from-ref main --to-ref HEAD
```

Agent prose, progress wording, random migration IDs, timestamps, and temporary
paths are intentionally not Golden contracts.
