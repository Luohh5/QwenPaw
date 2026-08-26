# -*- coding: utf-8 -*-
"""Frozen contracts for generated Qoder and Codex plugin wrappers."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Callable

import pytest

from qwenpaw.portability.codex_plugin_adapter import (
    stage_codex_content_plugin,
)
from qwenpaw.portability.models import SourcePlugin
from qwenpaw.portability.qoder_plugin_adapter import stage_qoder_skill_plugin

_FIXTURES = Path(__file__).parents[2] / "fixtures" / "portability"


def _files(root: Path) -> list[str]:
    return sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
    )


def _assert_wrapper(
    staged: Path,
    *,
    files: list[str],
    manifest: dict[str, Any],
    backend: str,
) -> None:
    manifest_text = (staged / "plugin.json").read_text(encoding="utf-8")
    assert _files(staged) == files
    assert json.loads(manifest_text) == manifest
    assert (
        manifest_text
        == json.dumps(
            manifest,
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    assert (staged / "plugin.py").read_text(encoding="utf-8") == backend


def test_canonical_plugin_id_uses_manifest_then_source_id() -> None:
    from qwenpaw.portability.content_plugin_wrapper import (
        canonical_plugin_id,
    )

    assert canonical_plugin_id(
        {"name": " manifest-id "},
        "source-id@repo",
    ) == ("manifest-id")
    assert canonical_plugin_id({}, "source-id@repo") == "source-id"
    assert canonical_plugin_id({"name": ""}, "source-id@repo") == "source-id"


@pytest.mark.parametrize(
    ("manifest", "source_id"),
    [
        ({"name": "."}, "safe@repo"),
        ({"name": "bad/name"}, "safe@repo"),
        ({"name": "x" * 129}, "safe@repo"),
        ({"name": 7}, "safe@repo"),
        ({}, "bad/name@repo"),
    ],
)
def test_canonical_plugin_id_rejects_instead_of_rewriting(
    manifest: dict[str, Any],
    source_id: str,
) -> None:
    from qwenpaw.portability.content_plugin_wrapper import (
        canonical_plugin_id,
    )

    with pytest.raises(ValueError, match="canonical plugin id is unsafe"):
        canonical_plugin_id(manifest, source_id)


def test_codex_wrapper_output_is_frozen() -> None:
    source = _FIXTURES / "codex-mini/plugins/cache/mini-market/expo/1.0.0"
    plugin = SourcePlugin(
        source_id="expo@mini-market",
        name="Expo",
        marketplace="mini-market",
        version="1.0.0",
        install_source=str(source),
    )
    staged = stage_codex_content_plugin(plugin)
    try:
        _assert_wrapper(
            staged,
            files=[
                ".codex-plugin/plugin.json",
                ".mcp.json",
                "plugin.json",
                "plugin.py",
                "skills/building-native-ui/SKILL.md",
            ],
            manifest={
                "id": "expo",
                "name": "Expo",
                "version": "1.0.0",
                "type": "general",
                "description": "Portable Expo fixture",
                "author": "Imported from Codex",
                "entry": {"backend": "plugin.py"},
                "dependencies": [],
                "meta": {
                    "migration": {
                        "source": "codex",
                        "source_id": "expo@mini-market",
                        "adapter": "codex_content_bundle_v1",
                        "requires_review": True,
                    },
                },
            },
            backend=(
                "# -*- coding: utf-8 -*-\n"
                '"""Generated adapter for a Codex content plugin."""\n\n'
                "from pathlib import Path\n\n"
                "_ROOT = Path(__file__).parent\n\n\n"
                "class ImportedCodexContentPlugin:\n"
                "    def register(self, api) -> None:\n"
                "        api.register_skill_provider(\n"
                "            skills_dir=_ROOT / 'skills',\n"
                "            enabled_by_default=False,\n"
                '            channels=["all"],\n'
                "        )\n\n\n"
                "plugin = ImportedCodexContentPlugin()\n"
            ),
        )
        assert (staged / ".mcp.json").read_bytes() == (
            source / ".mcp.json"
        ).read_bytes()
    finally:
        shutil.rmtree(staged.parent)


def test_qoder_wrapper_output_is_frozen() -> None:
    source = _FIXTURES / "qoder-mini/plugins/custom/mini-plugin-0.1.0"
    plugin = SourcePlugin(
        source_id="mini-plugin@local-custom",
        name="Mini Plugin",
        marketplace="local-custom",
        version="0.1.0",
        install_source=str(source),
        metadata={"canonical_plugin_source": str(source.resolve())},
    )
    staged = stage_qoder_skill_plugin(plugin)
    try:
        _assert_wrapper(
            staged,
            files=[
                "plugin.json",
                "plugin.py",
                "skills/report/SKILL.md",
            ],
            manifest={
                "id": "mini-plugin",
                "name": "Mini Plugin",
                "version": "0.1.0",
                "type": "general",
                "description": "Portable Qoder fixture",
                "description_i18n": {},
                "author": "Imported from Qoder",
                "entry": {"backend": "plugin.py"},
                "dependencies": [],
                "meta": {
                    "migration": {
                        "source": "qoder",
                        "source_id": "mini-plugin@local-custom",
                        "adapter": "qoder_skill_only_v1",
                        "harness_bound": False,
                        "requires_review": True,
                    },
                },
            },
            backend=(
                "# -*- coding: utf-8 -*-\n"
                '"""Generated adapter for a Qoder Skill-only plugin."""\n\n'
                "from pathlib import Path\n\n"
                "_ROOT = Path(__file__).parent\n\n\n"
                "class ImportedQoderSkillPlugin:\n"
                "    def register(self, api) -> None:\n"
                "        api.register_skill_provider(\n"
                '            skills_dir=_ROOT / "skills",\n'
                "            enabled_by_default=False,\n"
                '            channels=["all"],\n'
                "        )\n\n\n"
                "plugin = ImportedQoderSkillPlugin()\n"
            ),
        )
        assert not (staged / ".mcp.json").exists()
    finally:
        shutil.rmtree(staged.parent)


@pytest.mark.parametrize(
    "stage",
    [stage_codex_content_plugin, stage_qoder_skill_plugin],
)
def test_enabled_wrapper_registers_enabled_skills(
    stage: Callable[..., Path],
) -> None:
    if stage is stage_codex_content_plugin:
        source = _FIXTURES / "codex-mini/plugins/cache/mini-market/expo/1.0.0"
        plugin = SourcePlugin(
            source_id="expo@mini-market",
            name="Expo",
            marketplace="mini-market",
            install_source=str(source),
        )
    else:
        source = _FIXTURES / "qoder-mini/plugins/custom/mini-plugin-0.1.0"
        plugin = SourcePlugin(
            source_id="mini-plugin@local-custom",
            name="Mini Plugin",
            marketplace="local-custom",
            install_source=str(source),
            metadata={"canonical_plugin_source": str(source.resolve())},
        )

    staged = stage(plugin, enabled=True)
    try:
        manifest = json.loads(
            (staged / "plugin.json").read_text(encoding="utf-8"),
        )
        backend = (staged / "plugin.py").read_text(encoding="utf-8")
        assert "enabled_by_default=True" in backend
        assert manifest["meta"]["migration"]["requires_review"] is False
    finally:
        shutil.rmtree(staged.parent)
