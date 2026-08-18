# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from qwenpaw.app.chats.manager import ChatManager
from qwenpaw.app.chats.repo import JsonChatRepository
from qwenpaw.app.chats.session import SafeJSONSession
from qwenpaw.harnesses.events import HarnessHistoryItem, HarnessHistoryKind
from qwenpaw.portability.importer import ProviderImportService
from qwenpaw.portability.models import (
    ProviderInventory,
    SourceMarketplace,
    SourceMCPServer,
    SourceMemoryFile,
    SourceMemoryProject,
    SourcePlugin,
    SourceSession,
    SourceSkill,
)


class _Provider:
    provider_id = "codex"

    def __init__(self, inventory: ProviderInventory) -> None:
        self._inventory = inventory

    async def inventory(
        self,
        *,
        limit: int,
        progress=None,
    ) -> ProviderInventory:
        assert limit >= 1
        if progress is not None:
            await progress("provider inventory")
        return self._inventory


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


@pytest.mark.asyncio
async def test_provider_import_is_additive_and_idempotent(
    tmp_path: Path,
    monkeypatch,
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
    monkeypatch.setattr(
        "qwenpaw.portability.importer.create_migration_provider",
        lambda _source, _workspace: _Provider(inventory),
    )

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
    tmp_path: Path,
    monkeypatch,
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
    monkeypatch.setattr(
        "qwenpaw.portability.importer.create_migration_provider",
        lambda _source, _workspace: _Provider(inventory),
    )
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
    assert receipt.doctor_report.summary_zh == "迁移完成，已检查的项目全部通过。"
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
    tmp_path: Path,
    monkeypatch,
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
    monkeypatch.setattr(
        "qwenpaw.portability.importer.create_migration_provider",
        lambda _source, _workspace: _Provider(inventory),
    )
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
    tmp_path: Path,
    monkeypatch,
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
    monkeypatch.setattr(
        "qwenpaw.portability.importer.create_migration_provider",
        lambda _source, _workspace: _Provider(inventory),
    )

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
async def test_provider_import_reports_progress(tmp_path, monkeypatch):
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
    monkeypatch.setattr(
        "qwenpaw.portability.importer.create_migration_provider",
        lambda _source, _workspace: _Provider(inventory),
    )
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
async def test_provider_not_detected_does_not_write(tmp_path, monkeypatch):
    workspace = _workspace(tmp_path)
    inventory = ProviderInventory(
        provider_id="codex",
        provider_name="Codex",
        detected=False,
        warnings=["not installed"],
    )
    monkeypatch.setattr(
        "qwenpaw.portability.importer.create_migration_provider",
        lambda _source, _workspace: _Provider(inventory),
    )

    with pytest.raises(ValueError, match="not found"):
        await ProviderImportService(workspace).import_from("codex")

    assert await workspace.chat_manager.list_chats(archived=None) == []
    assert not (workspace.workspace_dir / ".qwenpaw/imports").exists()


@pytest.mark.asyncio
async def test_concurrent_imports_are_serialized_and_do_not_duplicate(
    tmp_path: Path,
    monkeypatch,
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
    monkeypatch.setattr(
        "qwenpaw.portability.importer.create_migration_provider",
        lambda _source, _workspace: _Provider(inventory),
    )

    first, second = await asyncio.gather(
        ProviderImportService(workspace).import_from("codex"),
        ProviderImportService(workspace).import_from("codex"),
    )

    assert sum(bool(item.imported_sessions) for item in (first, second)) == 1
    assert len(await workspace.chat_manager.list_chats(archived=None)) == 1


@pytest.mark.asyncio
async def test_provider_skill_symbolic_link_is_skipped(
    tmp_path: Path,
    monkeypatch,
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
    monkeypatch.setattr(
        "qwenpaw.portability.importer.create_migration_provider",
        lambda _source, _workspace: _Provider(inventory),
    )

    receipt = await ProviderImportService(workspace).import_from("codex")

    assert receipt.imported_skills == []
    assert receipt.skipped_skills == ["linked"]
    assert any("symbolic link" in warning for warning in receipt.warnings)


@pytest.mark.asyncio
async def test_provider_skill_uses_existing_scanner_and_stays_disabled(
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
    monkeypatch.setattr(
        "qwenpaw.portability.importer.create_migration_provider",
        lambda _source, _workspace: _Provider(inventory),
    )

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
    monkeypatch.setattr(
        "qwenpaw.portability.importer.create_migration_provider",
        lambda _source, _workspace: _Provider(inventory),
    )

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
async def test_provider_import_sets_and_repairs_source_project_directory(
    tmp_path: Path,
    monkeypatch,
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
    monkeypatch.setattr(
        "qwenpaw.portability.importer.create_migration_provider",
        lambda _source, _workspace: _Provider(inventory),
    )

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
    tmp_path: Path,
    monkeypatch,
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
    monkeypatch.setattr(
        "qwenpaw.portability.importer.create_migration_provider",
        lambda _source, _workspace: _Provider(inventory),
    )

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
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = _workspace(tmp_path)
    workspace.marketplace_registry_path = tmp_path / "marketplaces.json"
    plugin_source = tmp_path / "marketplace/plugins/demo"
    plugin_source.mkdir(parents=True)
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
    monkeypatch.setattr(
        "qwenpaw.portability.importer.create_migration_provider",
        lambda _source, _workspace: _Provider(inventory),
    )

    receipt = await ProviderImportService(workspace).import_from("codex")

    assert receipt.restored_marketplaces == ["local"]
    assert receipt.installed_plugins == ["qwen-demo"]
    assert calls == [(str(plugin_source), app, False, False)]
    registry = json.loads(workspace.marketplace_registry_path.read_text())
    assert registry["sources"]["codex:codex:local"]["source"] == str(
        plugin_source.parent.parent,
    )


@pytest.mark.asyncio
async def test_provider_plugin_never_falls_back_to_installed_cache(
    tmp_path: Path,
    monkeypatch,
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
    monkeypatch.setattr(
        "qwenpaw.portability.importer.create_migration_provider",
        lambda _source, _workspace: _Provider(inventory),
    )

    receipt = await ProviderImportService(workspace).import_from("qoder")

    assert receipt.installed_plugins == []
    assert receipt.skipped_plugins == ["demo@qoder-bundler"]
    assert not (workspace.workspace_dir / "plugins").exists()


@pytest.mark.asyncio
async def test_qoder_custom_skill_plugin_uses_native_adapter(
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
    monkeypatch.setattr(
        "qwenpaw.portability.importer.create_migration_provider",
        lambda _source, _workspace: _Provider(inventory),
    )

    receipt = await ProviderImportService(workspace).import_from("qoder")

    assert receipt.installed_plugins == ["test-report"]
    assert captured["manifest"]["id"] == "test-report"
    assert captured["manifest"]["meta"]["migration"]["harness_bound"] is True
    assert "enabled_by_default=False" in captured["backend"]
    assert captured["skill"] == "Read ~/.qoder/mcp.json"
    assert any("disabled" in warning for warning in receipt.warnings)


@pytest.mark.asyncio
async def test_failed_receipt_rolls_back_memory_and_native_plugin(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = _workspace(tmp_path)
    workspace.marketplace_registry_path = tmp_path / "marketplaces.json"
    memory_source = tmp_path / "source-memory/topic.md"
    memory_source.parent.mkdir()
    memory_source.write_text("temporary memory", encoding="utf-8")
    plugin_source = tmp_path / "marketplace/plugins/demo"
    plugin_source.mkdir(parents=True)
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
        provider_id="codex",
        provider_name="Codex",
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
        plugins=[
            SourcePlugin(
                source_id="demo@local",
                name="demo",
                marketplace="local",
                install_source=str(plugin_source),
            ),
        ],
    )
    monkeypatch.setattr(
        "qwenpaw.portability.importer.create_migration_provider",
        lambda _source, _workspace: _Provider(inventory),
    )

    with pytest.raises(OSError, match="receipt storage unavailable"):
        await ProviderImportService(workspace).import_from("codex")

    assert uninstalled == ["qwen-demo"]
    assert not list(
        (workspace.workspace_dir / "memory/imports/codex").glob("*/topic.md"),
    )
