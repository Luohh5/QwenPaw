# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from qwenpaw.portability.import_planning import ImportPlanningMixin
from qwenpaw.portability.models import (
    ImportAssetResult,
    ImportAssetState,
    ImportSelection,
    MigrationPlan,
    ProviderInventory,
    SourceMarketplace,
    SourceMCPServer,
    SourceMemoryFile,
    SourceMemoryProject,
    SourcePlugin,
    SourceScheduledTask,
    SourceSession,
    SourceSkill,
)
from qwenpaw.portability.planner import (
    build_migration_plan,
    tool_asset_fingerprints,
)
from qwenpaw.portability.selection import select_inventory


def _inventory(tmp_path: Path) -> ProviderInventory:
    return ProviderInventory(
        provider_id="codex",
        provider_name="Codex",
        detected=True,
        sessions=[SourceSession(source_id="thread-1", title="Thread")],
        ignored_session_ids=["internal-1"],
        memory_projects=[
            SourceMemoryProject(source_id="memory-1", project_key="project"),
        ],
        skills=[
            SourceSkill(
                source_id="skill-1",
                name="Skill",
                directory=tmp_path / "skill",
            ),
        ],
        marketplaces=[
            SourceMarketplace(source_id="market-1", name="market"),
        ],
        plugins=[
            SourcePlugin(
                source_id="plugin-1",
                name="Plugin",
                marketplace="market-1",
            ),
        ],
        mcp_servers=[
            SourceMCPServer(source_id="mcp-1", name="Standalone MCP"),
            SourceMCPServer(
                source_id="plugin-mcp",
                name="Plugin MCP",
                metadata={"source_plugin": "plugin-1"},
            ),
        ],
        scheduled_tasks=[
            SourceScheduledTask(source_id="cron-1", name="Daily"),
            SourceScheduledTask(
                source_id="heartbeat-1",
                name="Heartbeat",
                metadata={
                    "source_kind": "heartbeat",
                    "target_thread_id": "thread-1",
                },
            ),
        ],
    )


def test_select_plugin_includes_bound_mcp_and_marketplace(
    tmp_path: Path,
) -> None:
    source = _inventory(tmp_path)
    source.mcp_servers[1].metadata["source_plugin_relative_cwd"] = "."

    selected = select_inventory(
        source,
        ImportSelection(sessions=False, plugins=["plugin-1"]),
    )

    assert [item.source_id for item in selected.plugins] == ["plugin-1"]
    assert [item.source_id for item in selected.mcp_servers] == ["plugin-mcp"]
    assert [item.source_id for item in selected.marketplaces] == ["market-1"]
    assert selected.sessions == []
    assert selected.ignored_session_ids == []
    assert len(source.mcp_servers) == 2


def test_tool_fingerprints_do_not_deep_copy_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory = _inventory(tmp_path)
    original = ProviderInventory.model_copy

    def model_copy(self, *args, **kwargs):
        assert not kwargs.get("deep")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(ProviderInventory, "model_copy", model_copy)

    assert tool_asset_fingerprints(inventory)


def test_select_mcp_with_plugin_provenance_stays_independent(
    tmp_path: Path,
) -> None:
    selected = select_inventory(
        _inventory(tmp_path),
        ImportSelection(sessions=False, mcp=["plugin-mcp"]),
    )

    assert selected.plugins == []
    assert [item.source_id for item in selected.mcp_servers] == ["plugin-mcp"]


def test_plugin_relative_mcp_requires_selected_plugin(tmp_path: Path) -> None:
    inventory = _inventory(tmp_path)
    inventory.mcp_servers[1].metadata["source_plugin_relative_cwd"] = "."

    with pytest.raises(ValueError, match="plugin-mcp.*plugin-1"):
        select_inventory(
            inventory,
            ImportSelection(sessions=False, mcp=["plugin-mcp"]),
        )

    selected = select_inventory(
        inventory,
        ImportSelection(sessions=False, plugins=["plugin-1"]),
    )
    assert [item.source_id for item in selected.mcp_servers] == ["plugin-mcp"]


@pytest.mark.asyncio
async def test_plan_hides_bound_mcp_and_fingerprints_it_with_plugin(
    tmp_path: Path,
) -> None:
    inventory = _inventory(tmp_path)
    bound_mcp = inventory.mcp_servers[1]
    bound_mcp.metadata["source_plugin_relative_cwd"] = "."
    workspace = SimpleNamespace(workspace_dir=tmp_path, agent_id="agent-1")

    plan = await build_migration_plan(workspace, inventory)
    action_ids = {action.source_id for action in plan.actions}
    fingerprint = plan.asset_fingerprints["plugins:plugin-1"]

    assert "mcp-1" in action_ids
    assert "plugin-mcp" not in action_ids
    bound_mcp.args = ["changed"]
    assert (
        tool_asset_fingerprints(inventory)["plugins:plugin-1"] != fingerprint
    )


@pytest.mark.asyncio
async def test_retry_rejects_a_bound_mcp_without_plugin(
    tmp_path: Path,
) -> None:
    inventory = _inventory(tmp_path)
    inventory.mcp_servers[1].metadata["source_plugin_relative_cwd"] = "."
    service = _PlanningService(tmp_path, inventory)
    service.plan.state = "applied"
    service.plan.migration_id = "migration-1"

    with pytest.raises(ValueError, match="plugin-mcp.*plugin-1"):
        await service.retry_selection(
            service.plan.plan_id,
            ImportSelection(sessions=False, mcp=["plugin-mcp"]),
        )

    assert service.executed is None


def test_select_individual_asset_groups(tmp_path: Path) -> None:
    selected = select_inventory(
        _inventory(tmp_path),
        ImportSelection(
            memory=["memory-1"],
            skills=["skill-1"],
            mcp=["mcp-1"],
            cron=["cron-1"],
        ),
    )

    assert len(selected.sessions) == 1
    assert [item.source_id for item in selected.memory_projects] == [
        "memory-1",
    ]
    assert [item.source_id for item in selected.skills] == ["skill-1"]
    assert [item.source_id for item in selected.mcp_servers] == ["mcp-1"]
    assert [item.source_id for item in selected.scheduled_tasks] == ["cron-1"]
    assert selected.plugins == []
    assert selected.marketplaces == []


def test_selection_rejects_unknown_or_duplicate_ids(tmp_path: Path) -> None:
    inventory = _inventory(tmp_path)

    with pytest.raises(ValueError, match="unknown skills"):
        select_inventory(inventory, ImportSelection(skills=["missing"]))
    with pytest.raises(ValueError, match="duplicate plugins"):
        select_inventory(
            inventory,
            ImportSelection(plugins=["plugin-1", "plugin-1"]),
        )


def test_heartbeat_can_use_a_previously_imported_conversation(
    tmp_path: Path,
) -> None:
    selected = select_inventory(
        _inventory(tmp_path),
        ImportSelection(sessions=False, cron=["heartbeat-1"]),
    )

    assert [item.source_id for item in selected.scheduled_tasks] == [
        "heartbeat-1",
    ]


@pytest.mark.parametrize(
    "state",
    list(ImportAssetState),
)
def test_asset_result_accepts_all_public_states(
    state: ImportAssetState,
) -> None:
    result = ImportAssetResult(
        asset_type="plugin",
        source_id="plugin-1",
        name="Plugin",
        state=state,
        enabled=False,
    )

    assert result.state is state
    assert {item.value for item in ImportAssetState} == {
        "pending",
        "repairing",
        "ready",
        "failed",
        "succeeded",
        "existing",
    }


class _PlanningService(ImportPlanningMixin):
    def __init__(self, tmp_path: Path, inventory: ProviderInventory) -> None:
        self._workspace = SimpleNamespace(
            workspace_dir=tmp_path,
            agent_id="agent-1",
        )
        self.inventory = inventory
        self.inventory_options: dict | None = None
        self.executed: ProviderInventory | None = None
        self.plan = MigrationPlan(
            plan_id="plan-" + "a" * 32,
            source="codex",
            agent_id="agent-1",
            created_at=datetime.now(timezone.utc),
            asset_fingerprints=tool_asset_fingerprints(
                inventory,
                include_sessions=True,
            ),
        )

    async def _read_plan(self, _plan_id: str) -> MigrationPlan:
        return self.plan

    async def _write_plan(self, _plan: MigrationPlan) -> None:
        return None

    async def _inventory(self, *_args, **_kwargs) -> ProviderInventory:
        self.inventory_options = _kwargs
        return self.inventory

    async def _execute_plan(self, _plan, inventory, **_kwargs):
        self.executed = inventory
        return SimpleNamespace(migration_id="migration-1")


@pytest.mark.asyncio
async def test_apply_selection_filters_before_execution(
    tmp_path: Path,
) -> None:
    service = _PlanningService(tmp_path, _inventory(tmp_path))

    await service.apply_selection(
        service.plan.plan_id,
        ImportSelection(sessions=False, skills=["skill-1"]),
    )

    assert service.executed is not None
    assert [item.source_id for item in service.executed.skills] == ["skill-1"]
    assert service.executed.plugins == []
    assert service.inventory_options == {
        "source_home": None,
        "progress": None,
        "include_sessions": False,
        "session_ids": None,
    }


@pytest.mark.asyncio
async def test_apply_selection_rejects_changed_source(
    tmp_path: Path,
) -> None:
    source = _inventory(tmp_path)
    memory = tmp_path / "memory.md"
    memory.write_text("before", encoding="utf-8")
    source.memory_projects[0].files = [
        SourceMemoryFile(source_path=memory, relative_path=Path("memory.md")),
    ]
    service = _PlanningService(tmp_path, source)
    memory.write_text("after", encoding="utf-8")

    with pytest.raises(ValueError, match="来源数据在预演后发生了变化"):
        await service.apply_selection(
            service.plan.plan_id,
            ImportSelection(sessions=False, memory=["memory-1"]),
        )

    assert service.executed is None


@pytest.mark.asyncio
async def test_apply_selection_rejects_changed_conversations(
    tmp_path: Path,
) -> None:
    source = _inventory(tmp_path)
    service = _PlanningService(tmp_path, source)
    source.sessions[0].title = "Updated while the plan was open"

    with pytest.raises(ValueError, match="来源数据在预演后发生了变化"):
        await service.apply_selection(
            service.plan.plan_id,
            ImportSelection(sessions=True, skills=["skill-1"]),
        )

    assert service.executed is None


@pytest.mark.asyncio
async def test_apply_selection_rejects_added_or_removed_conversations(
    tmp_path: Path,
) -> None:
    source = _inventory(tmp_path)
    service = _PlanningService(tmp_path, source)
    source.sessions.append(SourceSession(source_id="thread-2", title="Later"))

    with pytest.raises(ValueError, match="来源数据在预演后发生了变化"):
        await service.apply_selection(
            service.plan.plan_id,
            ImportSelection(sessions=True),
        )

    source = _inventory(tmp_path)
    service = _PlanningService(tmp_path, source)
    source.sessions.clear()

    with pytest.raises(ValueError, match="来源数据在预演后发生了变化"):
        await service.apply_selection(
            service.plan.plan_id,
            ImportSelection(sessions=True),
        )


@pytest.mark.asyncio
async def test_apply_selection_ignores_unselected_tool_changes(
    tmp_path: Path,
) -> None:
    source = _inventory(tmp_path)
    memory = tmp_path / "memory.md"
    skill = source.skills[0].directory / "SKILL.md"
    memory.write_text("memory", encoding="utf-8")
    skill.parent.mkdir()
    skill.write_text("before", encoding="utf-8")
    source.memory_projects[0].files = [
        SourceMemoryFile(source_path=memory, relative_path=Path("memory.md")),
    ]
    service = _PlanningService(tmp_path, source)
    skill.write_text("after", encoding="utf-8")
    source.sessions[0].title = "Changed but not selected"

    await service.apply_selection(
        service.plan.plan_id,
        ImportSelection(sessions=False, memory=["memory-1"]),
    )

    assert service.executed is not None
