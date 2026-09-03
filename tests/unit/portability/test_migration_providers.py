# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from qwenpaw.harnesses.events import HarnessHistoryItem, HarnessHistoryKind
from qwenpaw.harnesses.codex.rollout_reader import CodexRolloutReader
from qwenpaw.portability.providers import (
    create_migration_provider,
    provider_names,
)
from qwenpaw.portability.providers.codex import CodexMigrationProvider
from qwenpaw.portability.providers.qoder import QoderMigrationProvider


class _CodexAdapter:
    async def status(self):
        return SimpleNamespace(
            installed=True,
            error="",
            runtime_path="/usr/local/bin/codex",
        )

    async def list_external_threads(self, *, limit):
        assert limit == 10
        return [
            {
                "id": "thread-1",
                "preview": "Existing task",
                "cwd": "/project",
                "createdAt": 1_700_000_000,
            },
        ]

    async def read_external_thread(self, thread_id):
        assert thread_id == "thread-1"
        return [
            HarnessHistoryItem(
                kind=HarnessHistoryKind.USER,
                text="Keep working",
                item_id="item-1",
            ),
        ]

    async def external_skill_records(self, cwd):
        assert cwd.is_absolute()
        return []

    async def discover_mcp(self, cwd):
        assert cwd.is_absolute()
        return [SimpleNamespace(name="filesystem")]

    async def external_mcp_records(self, cwd):
        assert cwd.is_absolute()
        return [
            {
                "name": "filesystem",
                "enabled": True,
                "auth_status": "unsupported",
                "transport": {
                    "type": "stdio",
                    "command": "npx",
                    "args": ["server-filesystem"],
                    "env_vars": ["FILESYSTEM_TOKEN"],
                },
            },
        ]


class _HarnessRuntime:
    def __init__(self, adapter) -> None:
        self._adapter = adapter

    async def adapter(self, provider_id, settings):
        assert provider_id == "codex"
        assert settings == {}
        return self._adapter


class _OfflineCodexAdapter:
    async def status(self):
        return SimpleNamespace(
            installed=False,
            error="codex executable unavailable",
            runtime_path="",
        )


class _UnexpectedHarnessRuntime:
    async def adapter(self, provider_id, settings):
        raise AssertionError("explicit source-home must not use app-server")


def _workspace(tmp_path: Path):
    config = SimpleNamespace(backend="qwenpaw", backend_settings={})
    return SimpleNamespace(
        workspace_dir=tmp_path,
        config=config,
        harness_runtime=_HarnessRuntime(_CodexAdapter()),
    )


@pytest.mark.asyncio
async def test_codex_provider_reuses_runtime_and_normalizes_inventory(
    tmp_path: Path,
) -> None:
    inventory = await CodexMigrationProvider(
        _workspace(tmp_path),
        rollout_reader=CodexRolloutReader(tmp_path / ".codex"),
    ).inventory(limit=10)

    assert inventory.detected is True
    assert inventory.locator == "/usr/local/bin/codex"
    assert inventory.sessions[0].source_id == "thread-1"
    assert inventory.sessions[0].history[0].text == "Keep working"
    assert inventory.mcp_servers[0].command == "npx"
    assert inventory.mcp_servers[0].env == {
        "FILESYSTEM_TOKEN": "${FILESYSTEM_TOKEN}",
    }
    assert inventory.mcp_servers[0].metadata["source_runtime_bound"] is False
    assert any("disabled QwenPaw" in item for item in inventory.warnings)


@pytest.mark.asyncio
async def test_codex_provider_reports_non_root_rollouts_as_ignored(
    tmp_path: Path,
) -> None:
    guardian_id = "01a013a9-e0c1-7853-8ce5-ffbac53bbbf1"
    rollout = (
        tmp_path
        / ".codex/sessions/2026/08/18"
        / f"rollout-2026-08-18T00-00-00-{guardian_id}.jsonl"
    )
    rollout.parent.mkdir(parents=True)
    rollout.write_text(
        json.dumps(
            {
                "timestamp": "2026-08-18T00:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": guardian_id,
                    "parent_thread_id": "thread-1",
                    "source": {"subagent": {"other": "guardian"}},
                    "thread_source": "subagent",
                },
            },
        )
        + "\n"
        + json.dumps(
            {
                "timestamp": "2026-08-18T00:00:01Z",
                "type": "event_msg",
                "payload": {
                    "type": "user_message",
                    "message": "The following is the Codex agent history",
                },
            },
        )
        + "\n",
        encoding="utf-8",
    )

    inventory = await CodexMigrationProvider(
        _workspace(tmp_path),
        rollout_reader=CodexRolloutReader(tmp_path / ".codex"),
    ).inventory(limit=10)

    assert [item.source_id for item in inventory.sessions] == ["thread-1"]
    assert inventory.ignored_session_ids == [guardian_id]
    assert any("non-root" in item for item in inventory.warnings)


@pytest.mark.asyncio
async def test_codex_provider_detects_portable_assets_without_cli_or_sessions(
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "custom-codex-home"
    skill = codex_home / "skills" / "portable-skill"
    memory = codex_home / "memories"
    skill.mkdir(parents=True)
    memory.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Portable", encoding="utf-8")
    (memory / "MEMORY.md").write_text("durable fact", encoding="utf-8")
    workspace = _workspace(tmp_path)
    workspace.harness_runtime = _HarnessRuntime(_OfflineCodexAdapter())

    inventory = await CodexMigrationProvider(
        workspace,
        rollout_reader=CodexRolloutReader(codex_home),
    ).inventory(limit=10)

    assert inventory.detected is True
    assert inventory.sessions == []
    assert any(item.name == "portable-skill" for item in inventory.skills)
    assert [item.source_id for item in inventory.memory_projects] == [
        "codex:global",
    ]


@pytest.mark.asyncio
async def test_codex_explicit_source_home_is_local_only(
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "mini-codex"
    skill = codex_home / "skills/local/SKILL.md"
    plugin = codex_home / "plugins/cache/market/demo/1.0.0"
    manifest = plugin / ".codex-plugin/plugin.json"
    skill.parent.mkdir(parents=True)
    manifest.parent.mkdir(parents=True)
    skill.write_text("# Local", encoding="utf-8")
    manifest.write_text(
        json.dumps({"name": "demo", "mcpServers": "./.mcp.json"}),
        encoding="utf-8",
    )
    (plugin / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "local-mcp": {"type": "http", "url": "https://local"},
                },
            },
        ),
        encoding="utf-8",
    )
    (plugin.parent / ".codex-remote-plugin-install.json").write_text(
        '{"schema_version":1}',
        encoding="utf-8",
    )
    workspace = _workspace(tmp_path)
    workspace.harness_runtime = _UnexpectedHarnessRuntime()

    inventory = await create_migration_provider(
        "codex",
        workspace,
        source_home=codex_home,
    ).inventory(limit=10)

    assert [item.name for item in inventory.skills] == ["local"]
    assert [item.name for item in inventory.plugins] == ["demo"]
    assert [item.name for item in inventory.mcp_servers] == ["local-mcp"]


@pytest.mark.asyncio
async def test_qoder_provider_detects_skill_only_custom_home(
    tmp_path: Path,
) -> None:
    qoder_home = tmp_path / "custom-qoder-home"
    skill = qoder_home / "skills" / "only-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Only skill", encoding="utf-8")

    inventory = await QoderMigrationProvider(
        SimpleNamespace(workspace_dir=tmp_path),
        qoder_home=qoder_home,
        qoder_user_data=tmp_path / "missing-user-data",
    ).inventory(limit=10)

    assert inventory.detected is True
    assert inventory.sessions == []
    assert [item.name for item in inventory.skills] == ["only-skill"]


def test_provider_registry_is_explicit_and_rejects_unknown_sources(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)

    assert provider_names() == ("codex", "qoder")
    assert create_migration_provider(
        "openai-codex",
        workspace,
    ).provider_id == ("codex")
    with pytest.raises(ValueError, match="Supported providers: codex, qoder"):
        create_migration_provider("unknown", workspace)
