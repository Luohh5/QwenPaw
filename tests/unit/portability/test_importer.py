# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from qwenpaw.app.chats.manager import ChatManager
from qwenpaw.app.chats.repo import JsonChatRepository
from qwenpaw.app.chats.session import SafeJSONSession
from qwenpaw.app.crons.manager import CronManager
from qwenpaw.app.driver_config_service import DriverConfigService
from qwenpaw.harnesses.events import HarnessHistoryItem, HarnessHistoryKind
from qwenpaw.portability.importer import ProviderImportService
from qwenpaw.portability.adaptation_loop import AdaptationResult
from qwenpaw.portability.compatibility import AssetZone, CompatibilityStore
from qwenpaw.portability.models import (
    ProviderInventory,
    SourceMarketplace,
    SourceMCPServer,
    SourceMemoryFile,
    SourceMemoryProject,
    SourcePlugin,
    SourceSession,
    SourceSkill,
    SourceScheduledTask,
)
from qwenpaw.portability.planner import inventory_fingerprint
from qwenpaw.portability.scheduled_tasks import build_imported_job


def _mock_adaptation(
    monkeypatch,
    workspace,
    inventory: ProviderInventory,
    *,
    zone: str = "repair",
    status: str = "completed",
) -> None:
    keys = {
        **{f"skills:{item.source_id}": zone for item in inventory.skills},
        **{f"mcp:{item.source_id}": zone for item in inventory.mcp_servers},
        **{f"plugins:{item.source_id}": zone for item in inventory.plugins},
        **{
            f"scheduled_tasks:{item.source_id}": zone
            for item in inventory.scheduled_tasks
        },
    }

    async def result(
        _workspace,
        _inventory,
        migration_id,
        _progress=None,
        **_kwargs,
    ):
        manifest_path = (
            workspace.workspace_dir
            / ".qwenpaw/imports"
            / migration_id
            / "test-adaptation-manifest.json"
        )
        store = CompatibilityStore(manifest_path)
        store.prepare(
            migration_id=migration_id,
            source=inventory.provider_id,
            skills=inventory.skills,
            mcp_servers=inventory.mcp_servers,
            plugins=inventory.plugins,
            scheduled_tasks=inventory.scheduled_tasks,
        )
        for key in keys:
            store.record_inspection(key)
            store.classify(
                key,
                AssetZone.REPAIR,
                "test fixture",
                plugin_disposition=(
                    "fully_usable" if key.startswith("plugins:") else ""
                ),
            )
        if zone == "migrate":
            for key in keys:
                store.record_test(key, passed=True, summary="test fixture")
                store.classify(key, AssetZone.MIGRATE, "test fixture")
            store.finish()
        else:
            store.finish(stopped=True, reason="test fixture")
        return AdaptationResult(
            manifest_path=manifest_path,
            summary_path=workspace.workspace_dir / "missing-summary.md",
            status=status,
            counts={zone: len(keys)},
            asset_zones=keys,
        )

    monkeypatch.setattr(
        "qwenpaw.portability.importer.run_adaptation_loop",
        result,
    )


def _workspace(tmp_path: Path):
    root = tmp_path / "workspace"
    root.mkdir()
    return SimpleNamespace(
        workspace_dir=root,
        agent_id="agent-1",
        session=SafeJSONSession(str(root / "sessions")),
        chat_manager=ChatManager(
            repo=JsonChatRepository(root / "chats.json"),
        ),
    )


class _CronManager:
    def __init__(self) -> None:
        self.jobs = {}

    async def list_jobs(self):
        return list(self.jobs.values())

    async def create_or_replace_job(self, spec):
        self.jobs[spec.id] = spec

    def validate_job_spec(self, spec):
        if spec.schedule.cron == "99 99 * * *":
            raise ValueError("invalid persisted cron")

    async def delete_job(self, job_id):
        return self.jobs.pop(job_id, None) is not None

    requires_portability_review = staticmethod(
        CronManager.requires_portability_review,
    )
    canonicalize_imported_job_for_review = staticmethod(
        CronManager.canonicalize_imported_job_for_review,
    )

    async def restore_imported_job_snapshot(self, spec):
        self.jobs[spec.id] = spec


@pytest.mark.asyncio
async def test_provider_imports_scheduled_tasks_disabled_and_idempotent(
    bind_import_inventory,
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = _workspace(tmp_path)
    workspace.cron_manager = _CronManager()
    inventory = ProviderInventory(
        provider_id="codex",
        provider_name="Codex",
        detected=True,
        scheduled_tasks=[
            SourceScheduledTask(
                source_id="automation-1",
                name="Daily report",
                schedule_type="cron",
                cron="30 9 * * *",
                timezone="Asia/Shanghai",
                prompt="Summarize yesterday's work",
                enabled=True,
            ),
        ],
        discovered_scheduled_task_count=1,
    )
    bind_import_inventory(inventory)
    _mock_adaptation(monkeypatch, workspace, inventory)

    first = await ProviderImportService(workspace).import_from("codex")
    second = await ProviderImportService(workspace).import_from("codex")

    assert first.imported_scheduled_tasks == ["automation-1"]
    assert second.imported_scheduled_tasks == []
    assert second.skipped_scheduled_tasks == ["automation-1"]
    assert len(workspace.cron_manager.jobs) == 1
    job = next(iter(workspace.cron_manager.jobs.values()))
    assert job.enabled is False
    assert job.runtime.tool_safety is True
    assert job.runtime.share_session is False
    assert job.meta["portability"]["source_enabled"] is True
    assert job.meta["portability"]["requires_review"] is True
    assert first.doctor_report is not None
    schedule_check = next(
        item
        for item in first.doctor_report.checks
        if item.category == "scheduled_tasks"
    )
    assert schedule_check.status == "pass", schedule_check


@pytest.mark.asyncio
async def test_unfinished_adaptation_materializes_repair_items_disabled(
    bind_import_inventory,
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = _workspace(tmp_path)
    workspace.cron_manager = _CronManager()
    skill_source = tmp_path / "bound-skill"
    skill_source.mkdir()
    (skill_source / "SKILL.md").write_text(
        "---\nname: bound-skill\ndescription: Bound test\n---\n\n"
        "Run codex exec for this task.\n",
        encoding="utf-8",
    )
    inventory = ProviderInventory(
        provider_id="codex",
        provider_name="Codex",
        detected=True,
        skills=[
            SourceSkill(
                source_id="bound-skill",
                name="bound-skill",
                directory=skill_source,
            ),
        ],
        scheduled_tasks=[
            SourceScheduledTask(
                source_id="bound-automation",
                name="Bound automation",
                schedule_type="cron",
                cron="30 9 * * *",
                timezone="Asia/Shanghai",
                prompt="Run codex exec for the daily report.",
                enabled=True,
            ),
        ],
    )
    bind_import_inventory(inventory)
    _mock_adaptation(monkeypatch, workspace, inventory, status="stopped_limit")

    receipt = await ProviderImportService(workspace).import_from("codex")

    assert receipt.imported_skills == ["bound-skill"]
    assert receipt.skipped_skills == []
    assert (workspace.workspace_dir / "skills/bound-skill").exists()
    skill_manifest = json.loads(
        (workspace.workspace_dir / "skill.json").read_text(encoding="utf-8"),
    )
    assert skill_manifest["skills"]["bound-skill"]["enabled"] is False
    assert receipt.imported_scheduled_tasks == ["bound-automation"]
    assert receipt.skipped_scheduled_tasks == []
    job = next(iter(workspace.cron_manager.jobs.values()))
    assert job.enabled is False
    assert job.meta["portability"]["requires_review"] is True
    assert receipt.adaptation_counts["repair"] == 2


@pytest.mark.asyncio
async def test_migrate_zone_materializes_and_enables_assets(
    bind_import_inventory,
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = _workspace(tmp_path)
    workspace.cron_manager = _CronManager()
    source = tmp_path / "portable-skill"
    source.mkdir()
    (source / "SKILL.md").write_text(
        "---\nname: portable-skill\ndescription: Portable\n---\n\n"
        "Use QwenPaw tools.\n",
        encoding="utf-8",
    )
    inventory = ProviderInventory(
        provider_id="codex",
        provider_name="Codex",
        detected=True,
        skills=[
            SourceSkill(
                source_id="portable-skill",
                name="portable-skill",
                directory=source,
            ),
        ],
        mcp_servers=[
            SourceMCPServer(
                source_id="portable-mcp",
                name="portable-mcp",
                command=sys.executable,
            ),
        ],
        scheduled_tasks=[
            SourceScheduledTask(
                source_id="portable-task",
                name="Portable task",
                schedule_type="cron",
                cron="30 9 * * *",
                prompt="Create the daily report",
            ),
        ],
    )
    bind_import_inventory(inventory)
    _mock_adaptation(monkeypatch, workspace, inventory)

    async def _approved(*_args, **_kwargs):
        summary = workspace.workspace_dir / "compatibility-summary.md"
        summary.write_text("approved", encoding="utf-8")
        return AdaptationResult(
            manifest_path=workspace.workspace_dir / "missing-manifest.json",
            summary_path=summary,
            status="completed",
            counts={"migrate": 3, "repair": 0, "discard": 0, "staging": 0},
            asset_zones={
                "skills:portable-skill": "migrate",
                "mcp:portable-mcp": "migrate",
                "scheduled_tasks:portable-task": "migrate",
            },
        )

    monkeypatch.setattr(
        "qwenpaw.portability.importer.run_adaptation_loop",
        _approved,
    )

    receipt = await ProviderImportService(workspace).import_from("codex")

    skill_manifest = json.loads(
        (workspace.workspace_dir / "skill.json").read_text(encoding="utf-8"),
    )
    assert skill_manifest["skills"]["portable-skill"]["enabled"] is True
    cards = await DriverConfigService(workspace).list_cards()
    assert len(cards) == 1 and cards[0].enabled is True
    job = next(iter(workspace.cron_manager.jobs.values()))
    assert job.enabled is True
    assert job.meta["portability"]["requires_review"] is False
    assert receipt.adaptation_summary == "compatibility-summary.md"


@pytest.mark.asyncio
async def test_import_repairs_unreviewed_invalid_legacy_scheduled_task(
    bind_import_inventory,
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = _workspace(tmp_path)
    workspace.cron_manager = _CronManager()
    task = SourceScheduledTask(
        source_id="legacy-ghost",
        name="Legacy ghost",
        schedule_type="cron",
        cron="30 9 * * *",
        prompt="Create a safe report",
    )
    inventory = ProviderInventory(
        provider_id="codex",
        provider_name="Codex",
        detected=True,
        scheduled_tasks=[task],
        discovered_scheduled_task_count=1,
    )
    ghost = build_imported_job("codex", task)
    ghost.schedule = ghost.schedule.model_copy(
        update={"cron": "99 99 * * *"},
    )
    workspace.cron_manager.jobs[ghost.id] = ghost
    bind_import_inventory(inventory)
    _mock_adaptation(monkeypatch, workspace, inventory)

    receipt = await ProviderImportService(workspace).import_from("codex")

    assert receipt.imported_scheduled_tasks == ["legacy-ghost"]
    repaired = workspace.cron_manager.jobs[ghost.id]
    assert repaired.schedule.cron == "30 9 * * *"
    assert repaired.enabled is False
    assert repaired.meta["portability"]["requires_review"] is True
    assert any("旧版留下" in warning for warning in receipt.warnings)


@pytest.mark.asyncio
async def test_import_repairs_missing_review_gate_on_valid_legacy_task(
    bind_import_inventory,
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = _workspace(tmp_path)
    workspace.cron_manager = _CronManager()
    task = SourceScheduledTask(
        source_id="legacy-valid",
        name="Legacy valid",
        schedule_type="cron",
        cron="30 9 * * *",
        prompt="Create a safe report",
    )
    inventory = ProviderInventory(
        provider_id="codex",
        provider_name="Codex",
        detected=True,
        scheduled_tasks=[task],
        discovered_scheduled_task_count=1,
    )
    legacy = build_imported_job("codex", task)
    provenance = {"source": "codex", "source_id": task.source_id}
    legacy = legacy.model_copy(
        update={
            "enabled": True,
            "meta": {"portability": provenance},
            "dispatch": legacy.dispatch.model_copy(update={"meta": {}}),
            "request": legacy.request.model_copy(
                update={"request_context": {}},
            ),
        },
    )
    workspace.cron_manager.jobs[legacy.id] = legacy
    bind_import_inventory(inventory)
    _mock_adaptation(monkeypatch, workspace, inventory)

    receipt = await ProviderImportService(workspace).import_from("codex")

    repaired = workspace.cron_manager.jobs[legacy.id]
    assert receipt.skipped_scheduled_tasks == [task.source_id]
    assert repaired.enabled is False
    assert repaired.meta["portability"]["requires_review"] is True
    assert any("审核门禁已补齐" in warning for warning in receipt.warnings)


@pytest.mark.asyncio
async def test_failed_import_restores_invalid_legacy_scheduled_task(
    bind_import_inventory,
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = _workspace(tmp_path)
    workspace.cron_manager = _CronManager()
    task = SourceScheduledTask(
        source_id="rollback-ghost",
        name="Rollback ghost",
        schedule_type="cron",
        cron="30 9 * * *",
        prompt="Create a safe report",
    )
    inventory = ProviderInventory(
        provider_id="codex",
        provider_name="Codex",
        detected=True,
        scheduled_tasks=[task],
        discovered_scheduled_task_count=1,
    )
    ghost = build_imported_job("codex", task)
    ghost.schedule = ghost.schedule.model_copy(
        update={"cron": "99 99 * * *"},
    )
    workspace.cron_manager.jobs[ghost.id] = ghost
    bind_import_inventory(inventory)

    async def _fail_receipt(*_args, **_kwargs):
        raise OSError("receipt unavailable")

    monkeypatch.setattr(
        "qwenpaw.portability.importer.write_json_atomic_async",
        _fail_receipt,
    )

    with pytest.raises(OSError, match="receipt unavailable"):
        await ProviderImportService(workspace).import_from("codex")

    restored = workspace.cron_manager.jobs[ghost.id]
    assert restored.schedule.cron == "99 99 * * *"
    assert restored.enabled is False


def test_inventory_fingerprint_ignores_new_non_root_source_sessions() -> None:
    inventory = ProviderInventory(
        provider_id="codex",
        provider_name="Codex",
        detected=True,
        ignored_session_ids=["guardian-1"],
    )
    before = inventory_fingerprint(inventory)

    inventory.ignored_session_ids.append("guardian-2")

    assert inventory_fingerprint(inventory) == before


@pytest.mark.asyncio
async def test_provider_import_is_additive_and_idempotent(
    bind_import_inventory,
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    inventory = ProviderInventory(
        provider_id="codex",
        provider_name="Codex",
        detected=True,
        locator="/usr/local/bin/codex",
        sessions=[
            SourceSession(
                source_id="thread-1",
                title="Imported thread",
                history=[
                    HarnessHistoryItem(
                        kind=HarnessHistoryKind.USER,
                        text="Fix the test",
                        item_id="user-1",
                    ),
                    HarnessHistoryItem(
                        kind=HarnessHistoryKind.MESSAGE,
                        text="Done",
                        item_id="assistant-1",
                    ),
                ],
            ),
        ],
    )
    bind_import_inventory(inventory)

    first = await ProviderImportService(workspace).import_from("codex")
    second = await ProviderImportService(workspace).import_from("codex")

    assert first.imported_sessions == ["thread-1"]
    assert second.imported_sessions == []
    assert second.skipped_sessions == ["thread-1"]
    chats = await workspace.chat_manager.list_chats(archived=None)
    assert len(chats) == 1
    portability = chats[0].meta["portability"]
    assert portability["source_id"] == "thread-1"
    assert portability["import_mode"] == "historical_archive"
    assert portability["read_only_enforced"] is False
    assert portability["continuation_fidelity"] == "not_guaranteed"
    state = await workspace.session.get_session_state_dict(
        chats[0].session_id,
        chats[0].user_id,
        chats[0].channel,
    )
    context = state["agent"]["state"]["context"]
    assert [message["role"] for message in context] == ["user", "assistant"]
    receipts = list(
        (workspace.workspace_dir / ".qwenpaw/imports").glob("*.json"),
    )
    assert len(receipts) == 2


@pytest.mark.asyncio
async def test_dry_run_plan_can_be_revalidated_and_applied(
    bind_import_inventory,
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    inventory = ProviderInventory(
        provider_id="codex",
        provider_name="Codex",
        detected=True,
        sessions=[
            SourceSession(
                source_id="planned-thread",
                title="Planned migration",
                history=[
                    HarnessHistoryItem(
                        kind=HarnessHistoryKind.USER,
                        text="Continue the planned task",
                    ),
                ],
            ),
        ],
    )
    bind_import_inventory(inventory)
    service = ProviderImportService(workspace)

    plan = await service.plan_from("codex")

    assert plan.state == "ready"
    assert plan.inventory_counts["sessions"] == 1
    assert plan.actions[0].action == "import_history"
    assert await workspace.chat_manager.list_chats(archived=None) == []
    receipt_root = workspace.workspace_dir / ".qwenpaw/imports"
    assert not list(
        receipt_root.glob("migration-*.json"),
    )

    receipt = await service.apply_plan(plan.plan_id)

    assert receipt.plan_id == plan.plan_id
    assert receipt.imported_sessions == ["planned-thread"]
    assert receipt.doctor_report is not None
    assert receipt.doctor_report.status == "pass"
    expected_summary = "迁移完成，已检查的项目全部通过。"
    assert receipt.doctor_report.summary_zh == expected_summary
    persisted = json.loads(
        (
            workspace.workspace_dir
            / ".qwenpaw/imports/plans"
            / f"{plan.plan_id}.json"
        ).read_text(encoding="utf-8"),
    )
    assert persisted["state"] == "applied"
    assert persisted["migration_id"] == receipt.migration_id


@pytest.mark.asyncio
async def test_apply_plan_refuses_changed_source_files(
    bind_import_inventory,
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    memory = tmp_path / "source-memory" / "fact.md"
    memory.parent.mkdir()
    memory.write_text("version one", encoding="utf-8")
    inventory = ProviderInventory(
        provider_id="codex",
        provider_name="Codex",
        detected=True,
        memory_projects=[
            SourceMemoryProject(
                source_id="memory-scope",
                project_key="project",
                files=[
                    SourceMemoryFile(
                        source_path=memory,
                        relative_path=Path("fact.md"),
                    ),
                ],
            ),
        ],
    )
    bind_import_inventory(inventory)
    service = ProviderImportService(workspace)
    plan = await service.plan_from("codex")
    memory.write_text("version two", encoding="utf-8")

    with pytest.raises(ValueError, match="来源数据.*发生了变化"):
        await service.apply_plan(plan.plan_id)

    assert await workspace.chat_manager.list_chats(archived=None) == []
    persisted = json.loads(
        (
            workspace.workspace_dir
            / ".qwenpaw/imports/plans"
            / f"{plan.plan_id}.json"
        ).read_text(encoding="utf-8"),
    )
    assert persisted["state"] == "ready"


@pytest.mark.asyncio
async def test_qoder_reimport_archives_internal_traces_from_old_import(
    bind_import_inventory,
    tmp_path: Path,
) -> None:
    """A rerun cleans up tool-only Qoder workers imported by older code."""
    workspace = _workspace(tmp_path)
    inventory = ProviderInventory(
        provider_id="qoder",
        provider_name="Qoder",
        detected=True,
        sessions=[
            SourceSession(
                source_id="worker-1",
                title="Qoder worker-1",
                history=[
                    HarnessHistoryItem(
                        kind=HarnessHistoryKind.TOOL_CALL,
                        tool_name="Bash",
                    ),
                ],
            ),
        ],
    )
    bind_import_inventory(inventory)

    await ProviderImportService(workspace).import_from("qoder")
    inventory.sessions = []
    inventory.ignored_session_ids = ["worker-1"]
    receipt = await ProviderImportService(workspace).import_from("qoder")

    assert receipt.archived_internal_sessions == ["worker-1"]
    assert await workspace.chat_manager.list_chats(archived=False) == []
    archived = await workspace.chat_manager.list_chats(archived=True)
    assert len(archived) == 1
    assert archived[0].meta["portability"]["source_id"] == "worker-1"


@pytest.mark.asyncio
async def test_codex_reimport_archives_previously_imported_guardian_chat(
    bind_import_inventory,
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    guardian_id = "guardian-review-1"
    inventory = ProviderInventory(
        provider_id="codex",
        provider_name="Codex",
        detected=True,
        sessions=[
            SourceSession(
                source_id=guardian_id,
                title=(
                    "The following is the Codex agent history whose request "
                    "action you are assessing"
                ),
                history=[
                    HarnessHistoryItem(
                        kind=HarnessHistoryKind.USER,
                        text="Internal approval transcript",
                    ),
                ],
            ),
        ],
    )
    bind_import_inventory(inventory)

    await ProviderImportService(workspace).import_from("codex")
    inventory.sessions = []
    inventory.ignored_session_ids = [guardian_id]
    receipt = await ProviderImportService(workspace).import_from("codex")

    assert receipt.ignored_source_sessions == [guardian_id]
    assert receipt.archived_internal_sessions == [guardian_id]
    assert await workspace.chat_manager.list_chats(archived=False) == []
    archived = await workspace.chat_manager.list_chats(archived=True)
    assert len(archived) == 1
    assert archived[0].meta["portability"]["source_id"] == guardian_id
    assert any(
        "Codex non-root/internal" in warning for warning in receipt.warnings
    )


@pytest.mark.asyncio
async def test_provider_import_reports_progress(
    bind_import_inventory,
    tmp_path,
):
    workspace = _workspace(tmp_path)
    inventory = ProviderInventory(
        provider_id="codex",
        provider_name="Codex",
        detected=True,
        sessions=[
            SourceSession(
                source_id="thread-progress",
                history=[
                    HarnessHistoryItem(
                        kind=HarnessHistoryKind.USER,
                        text="Show progress",
                    ),
                ],
            ),
        ],
    )
    bind_import_inventory(inventory)
    updates: list[str] = []

    async def _progress(message: str) -> None:
        updates.append(message)

    await ProviderImportService(workspace).import_from(
        "codex",
        progress=_progress,
    )

    assert "provider inventory" in updates
    assert any("正在写入会话：1/1" in item for item in updates)
    assert updates[-1] == "迁移事务已安全提交。"


@pytest.mark.asyncio
async def test_provider_not_detected_does_not_write(
    bind_import_inventory,
    tmp_path,
):
    workspace = _workspace(tmp_path)
    inventory = ProviderInventory(
        provider_id="codex",
        provider_name="Codex",
        detected=False,
        warnings=["not installed"],
    )
    bind_import_inventory(inventory)

    with pytest.raises(ValueError, match="not found"):
        await ProviderImportService(workspace).import_from("codex")

    assert await workspace.chat_manager.list_chats(archived=None) == []
    assert not (workspace.workspace_dir / ".qwenpaw/imports").exists()


@pytest.mark.asyncio
async def test_concurrent_imports_are_serialized_and_do_not_duplicate(
    bind_import_inventory,
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    inventory = ProviderInventory(
        provider_id="codex",
        provider_name="Codex",
        detected=True,
        sessions=[
            SourceSession(
                source_id="same-thread",
                history=[
                    HarnessHistoryItem(
                        kind=HarnessHistoryKind.USER,
                        text="One copy only",
                    ),
                ],
            ),
        ],
    )
    bind_import_inventory(inventory)

    first, second = await asyncio.gather(
        ProviderImportService(workspace).import_from("codex"),
        ProviderImportService(workspace).import_from("codex"),
    )

    assert sum(bool(item.imported_sessions) for item in (first, second)) == 1
    assert len(await workspace.chat_manager.list_chats(archived=None)) == 1


@pytest.mark.asyncio
async def test_provider_skill_symbolic_link_is_skipped(
    bind_import_inventory,
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    target = tmp_path / "provider-skill"
    target.mkdir()
    (target / "SKILL.md").write_text(
        "---\nname: linked\n---\n",
        encoding="utf-8",
    )
    linked = tmp_path / "linked-skill"
    try:
        linked.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symbolic links are unavailable on this platform")
    inventory = ProviderInventory(
        provider_id="codex",
        provider_name="Codex",
        detected=True,
        skills=[
            SourceSkill(
                source_id="linked",
                name="linked",
                directory=linked,
            ),
        ],
    )
    bind_import_inventory(inventory)

    receipt = await ProviderImportService(workspace).import_from("codex")

    assert receipt.imported_skills == []
    assert receipt.skipped_skills == ["linked"]
    assert any("symbolic link" in warning for warning in receipt.warnings)


@pytest.mark.asyncio
async def test_provider_skill_uses_existing_scanner_and_stays_disabled(
    bind_import_inventory,
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = _workspace(tmp_path)
    source = tmp_path / "provider-demo"
    source.mkdir()
    (source / "SKILL.md").write_text(
        "---\n"
        "name: provider-demo\n"
        "description: Imported provider skill\n"
        "---\n\n"
        "# Provider demo\n\nUse only when explicitly requested.\n",
        encoding="utf-8",
    )
    inventory = ProviderInventory(
        provider_id="codex",
        provider_name="Codex",
        detected=True,
        skills=[
            SourceSkill(
                source_id="provider-demo",
                name="provider-demo",
                directory=source,
            ),
        ],
    )
    bind_import_inventory(inventory)
    _mock_adaptation(monkeypatch, workspace, inventory)

    receipt = await ProviderImportService(workspace).import_from("codex")

    assert receipt.imported_skills == ["provider-demo"]
    skill_path = workspace.workspace_dir / "skills/provider-demo/SKILL.md"
    assert skill_path.is_file()
    manifest = json.loads(
        (workspace.workspace_dir / "skill.json").read_text(encoding="utf-8"),
    )
    assert manifest["skills"]["provider-demo"]["enabled"] is False


@pytest.mark.asyncio
async def test_provider_import_persists_disabled_mcp_with_encrypted_secret(
    bind_import_inventory,
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = _workspace(tmp_path)
    inventory = ProviderInventory(
        provider_id="codex",
        provider_name="Codex",
        detected=True,
        discovered_mcp_count=1,
        mcp_servers=[
            SourceMCPServer(
                source_id="filesystem",
                name="filesystem",
                transport="stdio",
                enabled=True,
                command="npx",
                args=["server-filesystem"],
                env={"API_TOKEN": "test-token"},
            ),
        ],
    )
    bind_import_inventory(inventory)
    _mock_adaptation(monkeypatch, workspace, inventory)

    first = await ProviderImportService(workspace).import_from("codex")
    second = await ProviderImportService(workspace).import_from("codex")

    assert first.imported_mcp_servers == ["filesystem"]
    assert second.skipped_mcp_servers == ["filesystem"]
    card_path = workspace.workspace_dir / "drivers/mcp/filesystem.yaml"
    assert card_path.is_file()
    card_text = card_path.read_text(encoding="utf-8")
    assert "enabled: false" in card_text
    assert "test-token" not in card_text
    credential_text = (workspace.workspace_dir / "credentials.yaml").read_text(
        encoding="utf-8",
    )
    assert "test-token" not in credential_text


@pytest.mark.asyncio
async def test_provider_import_encrypts_even_public_named_mcp_bindings(
    bind_import_inventory,
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = _workspace(tmp_path)
    env_secret = "sk-debug-field-secret-123456789"
    header_secret = "sk-user-agent-field-secret-123456789"
    inventory = ProviderInventory(
        provider_id="codex",
        provider_name="Codex",
        detected=True,
        discovered_mcp_count=2,
        mcp_servers=[
            SourceMCPServer(
                source_id="stdio-public-name",
                name="stdio-public-name",
                transport="stdio",
                command="npx",
                args=["server-package"],
                env={"DEBUG": env_secret},
            ),
            SourceMCPServer(
                source_id="http-public-name",
                name="http-public-name",
                transport="streamable_http",
                url="https://example.test/mcp",
                headers={"User-Agent": header_secret},
            ),
        ],
    )
    bind_import_inventory(inventory)
    _mock_adaptation(monkeypatch, workspace, inventory)

    receipt = await ProviderImportService(workspace).import_from("codex")

    assert receipt.imported_mcp_servers == [
        "stdio-public-name",
        "http-public-name",
    ]
    persisted = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in workspace.workspace_dir.rglob("*")
        if path.is_file()
    )
    assert env_secret not in persisted
    assert header_secret not in persisted
    assert "source: credential" in persisted


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("server", "secret"),
    [
        pytest.param(
            SourceMCPServer(
                source_id="unsafe-inline",
                name="unsafe-inline",
                transport="stdio",
                command="npx",
                args=["server", "--api-key", "sk-inline-secret-123456789"],
            ),
            "sk-inline-secret-123456789",
            id="api-key-flag",
        ),
        pytest.param(
            SourceMCPServer(
                source_id="unsafe-password",
                name="unsafe-password",
                transport="stdio",
                command="mysql",
                args=["-phunter2"],
            ),
            "hunter2",
            id="attached-password-flag",
        ),
        pytest.param(
            SourceMCPServer(
                source_id="unsafe-jdbc",
                name="unsafe-jdbc",
                transport="stdio",
                command="java",
                args=["jdbc:postgresql://alice:hunter2@example.test/prod"],
            ),
            "hunter2",
            id="jdbc-credentials",
        ),
        pytest.param(
            SourceMCPServer(
                source_id="unsafe-webhook",
                name="unsafe-webhook",
                transport="streamable_http",
                url=(
                    "https://hooks.slack.com/services/T000/B000/"
                    "correct-horse-battery-staple"
                ),
            ),
            "correct-horse-battery-staple",
            id="webhook-path-secret",
        ),
    ],
)
async def test_provider_import_rejects_inline_mcp_argument_secret(
    bind_import_inventory,
    tmp_path: Path,
    server: SourceMCPServer,
    secret: str,
) -> None:
    workspace = _workspace(tmp_path)
    inventory = ProviderInventory(
        provider_id="codex",
        provider_name="Codex",
        detected=True,
        discovered_mcp_count=1,
        mcp_servers=[server],
    )
    bind_import_inventory(inventory)

    receipt = await ProviderImportService(workspace).import_from("codex")

    assert receipt.imported_mcp_servers == []
    assert receipt.skipped_mcp_servers == [server.name]
    assert not (
        workspace.workspace_dir / f"drivers/mcp/{server.name}.yaml"
    ).exists()
    persisted = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in workspace.workspace_dir.rglob("*")
        if path.is_file()
    )
    assert secret not in persisted


@pytest.mark.asyncio
async def test_dry_run_routes_inline_mcp_secret_to_agent_workflow(
    bind_import_inventory,
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    inventory = ProviderInventory(
        provider_id="codex",
        provider_name="Codex",
        detected=True,
        mcp_servers=[
            SourceMCPServer(
                source_id="signed-url",
                name="signed-url",
                transport="streamable_http",
                url="https://example.test/mcp?sig=opaque-signature",
            ),
        ],
    )
    bind_import_inventory(inventory)

    plan = await ProviderImportService(workspace).plan_from("codex")

    action = next(item for item in plan.actions if item.asset_type == "mcp")
    assert action.action == "agent_test_and_adapt"
    assert action.fidelity == "agent_decision"
    assert "Mission" in action.reason_zh


@pytest.mark.asyncio
async def test_provider_import_sets_and_repairs_source_project_directory(
    bind_import_inventory,
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    project = tmp_path / "source-project"
    project.mkdir()
    session = SourceSession(
        source_id="thread-project",
        history=[
            HarnessHistoryItem(
                kind=HarnessHistoryKind.USER,
                text="Continue here",
            ),
        ],
    )
    inventory = ProviderInventory(
        provider_id="codex",
        provider_name="Codex",
        detected=True,
        sessions=[session],
    )
    bind_import_inventory(inventory)

    await ProviderImportService(workspace).import_from("codex")
    session.cwd = str(project)
    repaired = await ProviderImportService(workspace).import_from("codex")

    assert repaired.skipped_sessions == ["thread-project"]
    chats = await workspace.chat_manager.list_chats(archived=None)
    assert chats[0].meta["runtime_context"]["project_dir"] == str(
        project.resolve(),
    )


@pytest.mark.asyncio
async def test_provider_memory_is_scoped_exact_and_idempotent(
    bind_import_inventory,
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    source = tmp_path / "source-memory/MEMORY.md"
    source.parent.mkdir()
    source.write_text("# Source memory\n\nExact bytes.\n", encoding="utf-8")
    project = SourceMemoryProject(
        source_id="project-a",
        project_key="Project A",
        cwd="/source/project-a",
        files=[
            SourceMemoryFile(
                source_path=source,
                relative_path=Path("MEMORY.md"),
            ),
        ],
    )
    inventory = ProviderInventory(
        provider_id="codex",
        provider_name="Codex",
        detected=True,
        memory_projects=[project],
    )
    bind_import_inventory(inventory)

    first = await ProviderImportService(workspace).import_from("codex")
    second = await ProviderImportService(workspace).import_from("codex")

    assert first.imported_memory_projects == ["project-a"]
    assert second.skipped_memory_projects == ["project-a"]
    imported = list(
        (workspace.workspace_dir / "memory/imports/codex").glob(
            "*/MEMORY.md",
        ),
    )
    assert len(imported) == 1
    assert imported[0].read_bytes() == source.read_bytes()
    scope = json.loads((imported[0].parent / "_scope.json").read_text())
    assert scope["cwd"] == "/source/project-a"
    assert scope["trust"] == "source_material_not_instructions"
    assert not (workspace.workspace_dir / "MEMORY.md").exists()


@pytest.mark.asyncio
async def test_provider_plugin_restores_marketplace_then_native_installs(
    bind_import_inventory,
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = _workspace(tmp_path)
    workspace.marketplace_registry_path = tmp_path / "marketplaces.json"
    plugin_source = tmp_path / "marketplace/plugins/demo"
    plugin_source.mkdir(parents=True)
    (plugin_source / "plugin.json").write_text(
        json.dumps({"id": "qwen-demo", "entry": {}}),
        encoding="utf-8",
    )
    calls = []

    async def _install(source, *, app, force, reload_agents):
        calls.append((source, app, force, reload_agents))
        return SimpleNamespace(
            manifest=SimpleNamespace(id="qwen-demo"),
        )

    app = SimpleNamespace(state=SimpleNamespace(plugin_loader=object()))
    monkeypatch.setattr(
        "qwenpaw.plugins.registry.PluginRegistry.get_plugin_http_app",
        lambda _self: app,
    )
    monkeypatch.setattr(
        "qwenpaw.app.routers.plugins.install_plugin_source",
        _install,
    )
    inventory = ProviderInventory(
        provider_id="codex",
        provider_name="Codex",
        detected=True,
        marketplaces=[
            SourceMarketplace(
                source_id="codex:local",
                name="local",
                source=str(plugin_source.parent.parent),
                source_type="directory",
            ),
        ],
        plugins=[
            SourcePlugin(
                source_id="demo@local",
                name="demo",
                marketplace="local",
                install_source=str(plugin_source),
            ),
        ],
    )
    bind_import_inventory(inventory)
    _mock_adaptation(monkeypatch, workspace, inventory)

    receipt = await ProviderImportService(workspace).import_from("codex")

    assert receipt.restored_marketplaces == ["local"]
    assert receipt.prepared_plugins == ["demo@local"]
    assert receipt.installed_plugins == []
    assert not calls
    assert receipt.doctor_report is not None
    pending_check = next(
        item
        for item in receipt.doctor_report.checks
        if item.category == "plugins_prepared"
    )
    assert pending_check.status == "warning"
    registry = json.loads(workspace.marketplace_registry_path.read_text())
    assert registry["sources"]["codex:codex:local"]["source"] == str(
        plugin_source.parent.parent,
    )


@pytest.mark.asyncio
async def test_provider_plugin_never_falls_back_to_installed_cache(
    bind_import_inventory,
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    workspace.marketplace_registry_path = tmp_path / "marketplaces.json"
    inventory = ProviderInventory(
        provider_id="qoder",
        provider_name="Qoder",
        detected=True,
        marketplaces=[
            SourceMarketplace(
                source_id="qoder:qoder-bundler",
                name="qoder-bundler",
                source_type="builtin",
            ),
        ],
        plugins=[
            SourcePlugin(
                source_id="demo@qoder-bundler",
                name="demo",
                marketplace="qoder-bundler",
                install_source="",
                metadata={"install_path": "/provider/cache/demo"},
            ),
        ],
    )
    bind_import_inventory(inventory)

    receipt = await ProviderImportService(workspace).import_from("qoder")

    assert receipt.installed_plugins == []
    assert receipt.skipped_plugins == ["demo@qoder-bundler"]
    assert not (workspace.workspace_dir / "plugins").exists()


@pytest.mark.asyncio
async def test_qoder_custom_skill_plugin_uses_native_adapter(
    bind_import_inventory,
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A registered custom Skill plugin uses a reviewed native wrapper."""
    workspace = _workspace(tmp_path)
    workspace.marketplace_registry_path = tmp_path / "marketplaces.json"
    custom_root = tmp_path / ".qoder/plugins/custom"
    source = custom_root / "test-report-0.1.0"
    skill = source / "skills/test-report"
    manifest_dir = source / ".qoder-plugin"
    skill.mkdir(parents=True)
    manifest_dir.mkdir()
    (skill / "SKILL.md").write_text(
        "Read ~/.qoder/mcp.json",
        encoding="utf-8",
    )
    (manifest_dir / "plugin.json").write_text(
        json.dumps(
            {
                "name": "test-report",
                "displayName": "Test Report",
                "version": "0.1.0",
                "author": {"name": "User"},
                "skills": "./skills/",
            },
        ),
        encoding="utf-8",
    )
    captured = {}
    installed_plugins_root = tmp_path / "installed-plugins"
    installed_plugins_root.mkdir()

    async def _install(source_path, *, app, force, reload_agents):
        del app, force, reload_agents
        staged = Path(source_path)
        captured["manifest"] = json.loads(
            (staged / "plugin.json").read_text(encoding="utf-8"),
        )
        captured["backend"] = (staged / "plugin.py").read_text(
            encoding="utf-8",
        )
        captured["skill"] = (staged / "skills/test-report/SKILL.md").read_text(
            encoding="utf-8",
        )
        installed = installed_plugins_root / "test-report"
        installed.mkdir()
        (installed / "plugin.json").write_text(
            json.dumps({"id": "test-report"}),
            encoding="utf-8",
        )
        return SimpleNamespace(
            manifest=SimpleNamespace(id="test-report"),
        )

    app = SimpleNamespace(state=SimpleNamespace(plugin_loader=object()))
    monkeypatch.setattr(
        "qwenpaw.plugins.registry.PluginRegistry.get_plugin_http_app",
        lambda _self: app,
    )
    monkeypatch.setattr(
        "qwenpaw.app.routers.plugins.install_plugin_source",
        _install,
    )
    monkeypatch.setattr(
        "qwenpaw.portability.doctor.get_plugins_dir",
        lambda: installed_plugins_root,
    )
    inventory = ProviderInventory(
        provider_id="qoder",
        provider_name="Qoder",
        detected=True,
        marketplaces=[
            SourceMarketplace(
                source_id="qoder:local-custom",
                name="local-custom",
                source=str(custom_root),
                source_type="local_custom",
            ),
        ],
        plugins=[
            SourcePlugin(
                source_id="test-report-0.1.0@local-custom",
                name="test-report-0.1.0",
                marketplace="local-custom",
                version="0.1.0",
                install_source=str(source),
                metadata={
                    "adapter": "qoder_skill_only_v1",
                    "canonical_custom_root": str(custom_root.resolve()),
                    "skills_relative_path": "skills",
                    "harness_bound": True,
                    "skills_enabled_by_default": False,
                },
            ),
        ],
    )
    bind_import_inventory(inventory)
    _mock_adaptation(monkeypatch, workspace, inventory)

    receipt = await ProviderImportService(workspace).import_from("qoder")

    assert receipt.installed_plugins == ["test-report"]
    assert captured["manifest"]["id"] == "test-report"
    assert captured["manifest"]["meta"]["migration"]["harness_bound"] is True
    assert "enabled_by_default=False" in captured["backend"]
    assert captured["skill"] == "Read ~/.qoder/mcp.json"
    assert any("disabled" in warning for warning in receipt.warnings)
    assert receipt.doctor_report is not None
    plugin_check = next(
        item
        for item in receipt.doctor_report.checks
        if item.category == "plugins"
    )
    assert plugin_check.status == "warning"


@pytest.mark.asyncio
async def test_codex_content_plugin_registers_skills_and_owned_mcp(
    bind_import_inventory,
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = _workspace(tmp_path)
    source = tmp_path / "creative-production"
    manifest = source / ".codex-plugin/plugin.json"
    skill = source / "skills/produce/SKILL.md"
    server = source / "mcp/server.mjs"
    manifest.parent.mkdir(parents=True)
    skill.parent.mkdir(parents=True)
    server.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps(
            {
                "name": "creative-production",
                "version": "1.0.0",
                "skills": "./skills/",
                "mcpServers": "./.mcp.json",
            },
        ),
        encoding="utf-8",
    )
    skill.write_text(
        "---\nname: produce\ndescription: Produce visuals\n---\n",
        encoding="utf-8",
    )
    server.write_text("// bundled MCP", encoding="utf-8")
    (source / ".mcp.json").write_text(
        '{"mcpServers":{}}',
        encoding="utf-8",
    )
    installed_root = tmp_path / "installed-plugins"
    installed_root.mkdir()

    async def _install(source_path, *, app, force, reload_agents):
        del app, force, reload_agents
        staged = Path(source_path)
        installed = installed_root / "creative-production"
        shutil.copytree(staged, installed)
        return SimpleNamespace(
            manifest=SimpleNamespace(id="creative-production"),
            source_path=installed,
        )

    app = SimpleNamespace(state=SimpleNamespace(plugin_loader=object()))
    monkeypatch.setattr(
        "qwenpaw.plugins.registry.PluginRegistry.get_plugin_http_app",
        lambda _self: app,
    )
    monkeypatch.setattr(
        "qwenpaw.app.routers.plugins.install_plugin_source",
        _install,
    )
    monkeypatch.setattr(
        "qwenpaw.portability.doctor.get_plugins_dir",
        lambda: installed_root,
    )
    plugin_id = "creative-production@openai-curated-remote"
    inventory = ProviderInventory(
        provider_id="codex",
        provider_name="Codex",
        detected=True,
        plugins=[
            SourcePlugin(
                source_id=plugin_id,
                name="Creative Production",
                marketplace="openai-curated-remote",
                install_source=str(source),
                metadata={"adapter": "codex_content_bundle_v1"},
            ),
        ],
        discovered_mcp_count=1,
        mcp_servers=[
            SourceMCPServer(
                source_id=f"codex:plugin-mcp:{plugin_id}:creative",
                name="creative",
                command="node",
                args=["./mcp/server.mjs"],
                metadata={
                    "source_plugin": plugin_id,
                    "source_plugin_relative_cwd": ".",
                },
            ),
        ],
    )
    bind_import_inventory(inventory)
    _mock_adaptation(monkeypatch, workspace, inventory, zone="migrate")

    receipt = await ProviderImportService(workspace).import_from("codex")

    assert receipt.installed_plugins == ["creative-production"]
    assert receipt.imported_mcp_servers == ["creative"]
    backend = (installed_root / "creative-production/plugin.py").read_text()
    assert "register_skill_provider" in backend
    card = (workspace.workspace_dir / "drivers/mcp/creative.yaml").read_text()
    assert str(installed_root / "creative-production") in card


@pytest.mark.asyncio
async def test_failed_receipt_rolls_back_memory_and_native_plugin(
    bind_import_inventory,
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = _workspace(tmp_path)
    workspace.marketplace_registry_path = tmp_path / "marketplaces.json"
    original_registry = b'{"schema_version":"1","sources":{"keep":{}}}'
    workspace.marketplace_registry_path.write_bytes(original_registry)
    memory_source = tmp_path / "source-memory/topic.md"
    memory_source.parent.mkdir()
    memory_source.write_text("temporary memory", encoding="utf-8")
    custom_root = tmp_path / ".qoder/plugins/custom"
    plugin_source = custom_root / "demo-0.1.0"
    skill_dir = plugin_source / "skills/demo"
    manifest_dir = plugin_source / ".qoder-plugin"
    skill_dir.mkdir(parents=True)
    manifest_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo\n---\nSafe imported Skill.\n",
        encoding="utf-8",
    )
    (manifest_dir / "plugin.json").write_text(
        json.dumps(
            {
                "name": "qwen-demo",
                "version": "0.1.0",
                "skills": "./skills/",
            },
        ),
        encoding="utf-8",
    )
    app = SimpleNamespace(state=SimpleNamespace(plugin_loader=object()))
    uninstalled = []

    async def _install(_source, *, app, force, reload_agents):
        del app, force, reload_agents
        return SimpleNamespace(manifest=SimpleNamespace(id="qwen-demo"))

    async def _uninstall(plugin_id, *, app, reload_agents):
        del app, reload_agents
        uninstalled.append(plugin_id)

    async def _fail_receipt(*_args, **_kwargs):
        raise OSError("receipt storage unavailable")

    monkeypatch.setattr(
        "qwenpaw.plugins.registry.PluginRegistry.get_plugin_http_app",
        lambda _self: app,
    )
    monkeypatch.setattr(
        "qwenpaw.app.routers.plugins.install_plugin_source",
        _install,
    )
    monkeypatch.setattr(
        "qwenpaw.app.routers.plugins.uninstall_plugin_source",
        _uninstall,
    )
    monkeypatch.setattr(
        "qwenpaw.portability.importer.write_json_atomic_async",
        _fail_receipt,
    )
    inventory = ProviderInventory(
        provider_id="qoder",
        provider_name="Qoder",
        detected=True,
        memory_projects=[
            SourceMemoryProject(
                source_id="project-a",
                project_key="project-a",
                files=[
                    SourceMemoryFile(
                        source_path=memory_source,
                        relative_path=Path("topic.md"),
                    ),
                ],
            ),
        ],
        marketplaces=[
            SourceMarketplace(
                source_id="qoder:local-custom",
                name="local-custom",
                source=str(custom_root),
                source_type="local_custom",
            ),
        ],
        plugins=[
            SourcePlugin(
                source_id="demo-0.1.0@local-custom",
                name="demo-0.1.0",
                marketplace="local-custom",
                install_source=str(plugin_source),
                metadata={
                    "adapter": "qoder_skill_only_v1",
                    "canonical_custom_root": str(custom_root.resolve()),
                    "skills_relative_path": "skills",
                    "harness_bound": False,
                    "skills_enabled_by_default": True,
                },
            ),
        ],
    )
    bind_import_inventory(inventory)
    _mock_adaptation(monkeypatch, workspace, inventory, zone="migrate")

    with pytest.raises(OSError, match="receipt storage unavailable"):
        await ProviderImportService(workspace).import_from("qoder")

    assert uninstalled == ["qwen-demo"]
    assert not list(
        (workspace.workspace_dir / "memory/imports/qoder").glob("*/topic.md"),
    )
    assert (
        workspace.marketplace_registry_path.read_bytes() == original_registry
    )


@pytest.mark.asyncio
async def test_failed_receipt_rolls_back_all_core_asset_writers(
    bind_import_inventory,
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A late failure must leave no conversation, Skill, MCP, or Cron."""
    workspace = _workspace(tmp_path)
    workspace.cron_manager = _CronManager()
    skill_source = tmp_path / "rollback-skill"
    skill_source.mkdir()
    (skill_source / "SKILL.md").write_text(
        "---\n"
        "name: rollback-skill\n"
        "description: Rollback fixture\n"
        "---\n\n"
        "Use ordinary QwenPaw tools.\n",
        encoding="utf-8",
    )
    inventory = ProviderInventory(
        provider_id="codex",
        provider_name="Codex",
        detected=True,
        sessions=[
            SourceSession(
                source_id="rollback-session",
                title="Rollback session",
                history=[
                    HarnessHistoryItem(
                        kind=HarnessHistoryKind.USER,
                        text="Do rollback work",
                        item_id="rollback-message",
                    ),
                ],
            ),
        ],
        skills=[
            SourceSkill(
                source_id="rollback-skill",
                name="rollback-skill",
                directory=skill_source,
            ),
        ],
        mcp_servers=[
            SourceMCPServer(
                source_id="rollback-mcp",
                name="rollback-mcp",
                command=sys.executable,
            ),
        ],
        scheduled_tasks=[
            SourceScheduledTask(
                source_id="rollback-task",
                name="Rollback task",
                schedule_type="cron",
                cron="30 9 * * *",
                prompt="Review rollback state",
            ),
        ],
    )
    bind_import_inventory(inventory)
    _mock_adaptation(monkeypatch, workspace, inventory, zone="migrate")

    async def _fail_receipt(*_args, **_kwargs):
        raise OSError("receipt storage unavailable")

    monkeypatch.setattr(
        "qwenpaw.portability.importer.write_json_atomic_async",
        _fail_receipt,
    )

    with pytest.raises(OSError, match="receipt storage unavailable"):
        await ProviderImportService(workspace).import_from("codex")

    assert await workspace.chat_manager.list_chats(archived=None) == []
    assert not (workspace.workspace_dir / "skills/rollback-skill").exists()
    skill_manifest = workspace.workspace_dir / "skill.json"
    if skill_manifest.exists():
        assert "rollback-skill" not in skill_manifest.read_text(
            encoding="utf-8",
        )
    assert not (
        workspace.workspace_dir / "drivers/mcp/rollback-mcp.yaml"
    ).exists()
    assert workspace.cron_manager.jobs == {}
    imports = workspace.workspace_dir / ".qwenpaw/imports"
    assert not list(imports.glob("migration-*.json"))
