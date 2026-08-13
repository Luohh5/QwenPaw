# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from pathlib import Path

from qwenpaw.portability.providers.external_state import (
    discover_codex_memory,
    discover_codex_plugins,
    discover_project_memory,
    discover_qoder_plugins,
)


def test_codex_memory_preserves_global_and_extension_scope(tmp_path: Path):
    codex_home = tmp_path / ".codex"
    memories = codex_home / "memories"
    memories.mkdir(parents=True)
    (memories / "MEMORY.md").write_text("global memory", encoding="utf-8")
    project = memories / "extensions/imported/resources/project-a"
    project.mkdir(parents=True)
    (project / "scope.json").write_text(
        json.dumps({"cwd": "/source/project-a"}),
        encoding="utf-8",
    )
    (project / "topic.md").write_text("scoped memory", encoding="utf-8")

    projects = discover_codex_memory(codex_home)

    assert [item.source_id for item in projects] == [
        "codex:global",
        "codex:extension:imported:project-a",
    ]
    assert projects[1].cwd == "/source/project-a"
    assert projects[1].files[0].relative_path == Path("topic.md")


def test_project_memory_recovers_cwd_from_transcript(tmp_path: Path):
    home = tmp_path / ".qoder"
    project = home / "projects/project-key"
    memory = project / "memory/nested"
    transcript = project / "transcript"
    memory.mkdir(parents=True)
    transcript.mkdir()
    (memory / "fact.md").write_text("remember this", encoding="utf-8")
    (transcript / "session.jsonl").write_text(
        json.dumps({"metadata": {"cwd": "/source/worktree"}}) + "\n",
        encoding="utf-8",
    )

    projects = discover_project_memory(home)

    assert len(projects) == 1
    assert projects[0].cwd == "/source/worktree"
    assert projects[0].files[0].relative_path == Path("nested/fact.md")


def test_codex_plugin_uses_marketplace_source_not_installed_cache(
    tmp_path: Path,
):
    codex_home = tmp_path / ".codex"
    marketplace = tmp_path / "marketplace"
    plugin = marketplace / "plugins/demo"
    plugin.mkdir(parents=True)
    (plugin / "plugin.json").write_text(
        json.dumps({"id": "demo", "name": "Demo", "version": "1.0.0"}),
        encoding="utf-8",
    )
    manifest_dir = marketplace / ".codex-plugin"
    manifest_dir.mkdir()
    (manifest_dir / "marketplace.json").write_text(
        json.dumps(
            {"plugins": [{"name": "demo", "source": "../plugins/demo"}]},
        ),
        encoding="utf-8",
    )
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        '[plugins."demo@local"]\nenabled = true\n'
        '[marketplaces.local]\nsource_type = "directory"\n'
        f'source = "{marketplace}"\n',
        encoding="utf-8",
    )
    cache = codex_home / "plugins/cache/local/demo/cache-id/.codex-plugin"
    cache.mkdir(parents=True)
    (cache / "plugin.json").write_text(
        json.dumps({"name": "demo", "version": "9.9.9"}),
        encoding="utf-8",
    )

    marketplaces, plugins = discover_codex_plugins(codex_home)

    assert marketplaces[0].source == str(marketplace.resolve())
    assert plugins[0].version == "9.9.9"
    assert plugins[0].install_source == str(plugin.resolve())
    assert "cache" not in plugins[0].install_source


def test_qoder_plugin_cache_is_metadata_only(tmp_path: Path):
    home = tmp_path / ".qoder"
    plugins_root = home / "plugins"
    cache = plugins_root / "cache/qoder-bundler/demo"
    cache.mkdir(parents=True)
    (cache / "plugin.json").write_text(
        json.dumps({"id": "demo", "name": "Demo", "version": "1.0.0"}),
        encoding="utf-8",
    )
    (plugins_root / "installed_plugins_v2.json").write_text(
        json.dumps(
            {
                "version": 2,
                "plugins": {
                    "demo@qoder-bundler": [
                        {"enabled": True, "version": "1.0.0"},
                    ],
                },
            },
        ),
        encoding="utf-8",
    )

    marketplaces, plugins = discover_qoder_plugins(home)

    assert marketplaces[0].source_type == "builtin"
    assert plugins[0].install_source == ""
