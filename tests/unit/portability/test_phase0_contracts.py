# -*- coding: utf-8 -*-
"""Behavioral contracts that protect the Pawport refactor baseline."""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from qwenpaw.plugins.loader import PluginLoader
from qwenpaw.portability.codex_plugin_adapter import (
    stage_codex_content_plugin,
)
from qwenpaw.portability.models import (
    ImportReceipt,
    MigrationDoctorCheck,
    MigrationDoctorReport,
    SourcePlugin,
)
from qwenpaw.portability.providers import create_migration_provider
from qwenpaw.portability.providers.qoder import QoderMigrationProvider
from qwenpaw.portability.qoder_plugin_adapter import stage_qoder_skill_plugin

_FIXTURES = Path(__file__).parents[2] / "fixtures" / "portability"


class _UnexpectedHarnessRuntime:
    async def adapter(self, provider_id: str, settings: dict[str, Any]):
        del provider_id, settings
        raise AssertionError("explicit source-home must stay local-only")


def _workspace(tmp_path: Path) -> SimpleNamespace:
    root = tmp_path / "workspace"
    root.mkdir()
    return SimpleNamespace(
        workspace_dir=root,
        config=SimpleNamespace(backend="qwenpaw", backend_settings={}),
        harness_runtime=_UnexpectedHarnessRuntime(),
    )


def _copy_fixture(tmp_path: Path, name: str) -> Path:
    target = tmp_path / name
    shutil.copytree(_FIXTURES / name, target)
    return target


def _history(item: Any) -> dict[str, str]:
    return {
        "kind": item.kind.value,
        "text": item.text,
    }


def _inventory_contract(inventory: Any) -> dict[str, Any]:
    """Keep only stable, user-visible provider output."""
    return {
        "provider": inventory.provider_id,
        "sessions": sorted(
            (
                {
                    "id": item.source_id,
                    "title": item.title,
                    "cwd": item.cwd,
                    "history": [_history(event) for event in item.history],
                }
                for item in inventory.sessions
            ),
            key=lambda item: item["id"],
        ),
        "ignored_sessions": sorted(inventory.ignored_session_ids),
        "skills": sorted(item.name for item in inventory.skills),
        "mcp": sorted(
            (
                {
                    "id": item.source_id,
                    "name": item.name,
                    "transport": item.transport,
                    "command": item.command,
                    "args": item.args,
                    "url": item.url,
                    "plugin": str(item.metadata.get("source_plugin") or ""),
                }
                for item in inventory.mcp_servers
            ),
            key=lambda item: item["id"],
        ),
        "memory": sorted(
            (
                {
                    "id": item.source_id,
                    "cwd": item.cwd,
                    "files": sorted(
                        file.relative_path.as_posix() for file in item.files
                    ),
                }
                for item in inventory.memory_projects
            ),
            key=lambda item: item["id"],
        ),
        "marketplaces": sorted(
            (
                {
                    "id": item.source_id,
                    "name": item.name,
                    "type": item.source_type,
                }
                for item in inventory.marketplaces
            ),
            key=lambda item: item["id"],
        ),
        "plugins": sorted(
            (
                {
                    "id": item.source_id,
                    "name": item.name,
                    "version": item.version,
                    "adapter": str(item.metadata.get("adapter") or ""),
                }
                for item in inventory.plugins
            ),
            key=lambda item: item["id"],
        ),
        "scheduled_tasks": sorted(
            (
                {
                    "id": item.source_id,
                    "name": item.name,
                    "type": item.schedule_type,
                    "cron": item.cron,
                    "timezone": item.timezone,
                    "enabled_at_source": item.enabled,
                }
                for item in inventory.scheduled_tasks
            ),
            key=lambda item: item["id"],
        ),
    }


def _golden_json(name: str) -> dict[str, Any]:
    path = _FIXTURES / "golden" / name
    return json.loads(path.read_text(encoding="utf-8"))


def _content_plugins(
    tmp_path: Path,
    codex_name: str,
    qoder_name: str,
) -> tuple[SourcePlugin, SourcePlugin]:
    codex_home = _copy_fixture(tmp_path, "codex-mini")
    codex_source = codex_home / "plugins/cache/mini-market/expo/1.0.0"
    qoder_home = _copy_fixture(tmp_path, "qoder-mini")
    qoder_source = qoder_home / "plugins/custom/mini-plugin-0.1.0"
    return (
        SourcePlugin(
            source_id="expo@mini-market",
            name=codex_name,
            marketplace="mini-market",
            version="1.0.0",
            install_source=str(codex_source),
        ),
        SourcePlugin(
            source_id="mini-plugin@local-custom",
            name=qoder_name,
            marketplace="local-custom",
            version="0.1.0",
            install_source=str(qoder_source),
            metadata={"canonical_plugin_source": str(qoder_source.resolve())},
        ),
    )


@pytest.mark.asyncio
async def test_codex_mini_home_matches_golden_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "isolated-home"))
    codex_home = _copy_fixture(tmp_path, "codex-mini")

    inventory = await create_migration_provider(
        "codex",
        _workspace(tmp_path),
        source_home=codex_home,
    ).inventory(limit=20)

    assert _inventory_contract(inventory) == _golden_json(
        "codex-mini-inventory.json",
    )


@pytest.mark.asyncio
async def test_qoder_mini_home_matches_golden_inventory(
    tmp_path: Path,
) -> None:
    qoder_home = _copy_fixture(tmp_path, "qoder-mini")
    user_data = _copy_fixture(tmp_path, "qoder-user-data-mini")
    ledger = qoder_home / "plugins" / "installed_plugins_v2.json"
    ledger.write_text(
        ledger.read_text(encoding="utf-8").replace(
            "__QODER_HOME__",
            str(qoder_home),
        ),
        encoding="utf-8",
    )

    inventory = await QoderMigrationProvider(
        SimpleNamespace(workspace_dir=tmp_path / "workspace"),
        qoder_home=qoder_home,
        qoder_user_data=user_data,
    ).inventory(limit=20)

    assert _inventory_contract(inventory) == _golden_json(
        "qoder-mini-inventory.json",
    )


def test_import_receipt_matches_reviewed_contract() -> None:
    started = datetime(2026, 8, 20, tzinfo=timezone.utc)
    completed = datetime(2026, 8, 20, 0, 5, tzinfo=timezone.utc)
    receipt = ImportReceipt(
        migration_id="migration-fixture",
        plan_id="plan-fixture",
        source="qoder",
        source_locator="/fixture/qoder",
        agent_id="agent-fixture",
        started_at=started,
        completed_at=completed,
        imported_sessions=["session-1"],
        skipped_sessions=["session-existing"],
        ignored_source_sessions=["internal-1"],
        archived_internal_sessions=["internal-1"],
        imported_skills=["skill-1"],
        imported_mcp_servers=["mcp-1"],
        imported_memory_projects=["memory-1"],
        restored_marketplaces=["market-1"],
        prepared_plugins=["plugin-1@market-1"],
        installed_plugins=["plugin-1"],
        imported_scheduled_tasks=["cron-1"],
        adaptation_manifest="adaptation/manifest.json",
        adaptation_summary="adaptation/summary.md",
        warnings=["fixture warning"],
        doctor_report=MigrationDoctorReport(
            status="pass",
            summary_zh="迁移结果已通过检查。",
            checked_at=completed,
            checks=[
                MigrationDoctorCheck(
                    category="plugins",
                    status="pass",
                    title_zh="插件注册",
                    detail_zh="插件已完成原生注册。",
                ),
            ],
        ),
    )

    assert receipt.model_dump(mode="json") == _golden_json(
        "import-receipt.json",
    )


def test_generated_plugin_ids_do_not_depend_on_display_names(
    tmp_path: Path,
) -> None:
    codex, qoder = _content_plugins(
        tmp_path,
        "Renamed Expo Display",
        "Renamed Mini Plugin Display",
    )

    codex_staged = stage_codex_content_plugin(codex)
    qoder_staged = stage_qoder_skill_plugin(qoder)
    try:
        codex_manifest = json.loads(
            (codex_staged / "plugin.json").read_text(encoding="utf-8"),
        )
        qoder_manifest = json.loads(
            (qoder_staged / "plugin.json").read_text(encoding="utf-8"),
        )

        assert (codex_manifest["id"], codex_manifest["name"]) == (
            "expo",
            "Expo",
        )
        assert (qoder_manifest["id"], qoder_manifest["name"]) == (
            "mini-plugin",
            "Mini Plugin",
        )
    finally:
        shutil.rmtree(codex_staged.parent)
        shutil.rmtree(qoder_staged.parent)


@pytest.mark.asyncio
async def test_canonical_plugin_ids_block_duplicate_native_installs(
    tmp_path: Path,
) -> None:
    first_codex, first_qoder = _content_plugins(
        tmp_path,
        "First display name",
        "First Qoder display name",
    )
    second_codex = first_codex.model_copy(
        update={"name": "A different display name"},
    )
    second_qoder = first_qoder.model_copy(
        update={"name": "A different Qoder display name"},
    )
    staged = [
        stage_codex_content_plugin(first_codex),
        stage_codex_content_plugin(second_codex),
        stage_qoder_skill_plugin(first_qoder),
        stage_qoder_skill_plugin(second_qoder),
    ]
    install_root = tmp_path / "installed-plugins"
    install_root.mkdir()
    loader = PluginLoader([install_root])
    try:
        codex_record = await loader.load_plugin_from_path(
            staged[0],
            install_dir=install_root,
        )
        qoder_record = await loader.load_plugin_from_path(
            staged[2],
            install_dir=install_root,
        )
        assert (codex_record.manifest.id, qoder_record.manifest.id) == (
            "expo",
            "mini-plugin",
        )
        with pytest.raises(ValueError, match="already loaded"):
            await loader.load_plugin_from_path(
                staged[1],
                install_dir=install_root,
            )
        with pytest.raises(ValueError, match="already loaded"):
            await loader.load_plugin_from_path(
                staged[3],
                install_dir=install_root,
            )
        assert set(loader.get_all_loaded_plugins()) == {
            "expo",
            "mini-plugin",
        }
    finally:
        for plugin in staged:
            shutil.rmtree(plugin.parent)
