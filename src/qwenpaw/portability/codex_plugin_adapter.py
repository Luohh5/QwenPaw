# -*- coding: utf-8 -*-
"""Translate a staged Codex content bundle into a QwenPaw plugin."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .content_plugin_wrapper import (
    staging_target,
    write_wrapper,
)
from .models import SourcePlugin
from .skill_transfer import read_bounded_tree, write_tree_entry

ADAPTER = "codex_content_bundle_v1"
_SOURCE_MANIFESTS = (
    Path(".codex-plugin/plugin.json"),
    Path(".claude-plugin/plugin.json"),
)
_ROOT_CONFLICTS = {
    Path("plugin.json"),
    Path("plugin.py"),
    Path("requirements.txt"),
}


def _manifest(source: Path) -> tuple[dict[str, Any], Path]:
    relative = next(
        (item for item in _SOURCE_MANIFESTS if (source / item).is_file()),
        _SOURCE_MANIFESTS[0],
    )
    path = source / relative
    if (
        path.is_symlink()
        or not path.is_file()
        or path.stat().st_size > 1024 * 1024
    ):
        raise ValueError("Codex plugin manifest is unavailable or too large")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Codex plugin manifest is invalid")
    return value, relative


def _skill_paths(source: Path, manifest: dict[str, Any]) -> list[Path]:
    declared = manifest.get("skills")
    values = [declared] if isinstance(declared, str) else declared
    if not isinstance(values, list):
        values = ["skills"] if (source / "skills").is_dir() else []
    paths: list[Path] = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            continue
        relative = Path(value)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Codex plugin Skills path is unsafe")
        path = (source / relative).resolve(strict=True)
        if not path.is_dir() or not path.is_relative_to(source):
            raise ValueError("Codex plugin Skills path escaped its source")
        if path not in paths:
            paths.append(path)
    return paths


def stage_codex_content_plugin(
    plugin: SourcePlugin,
    *,
    enabled: bool = False,
) -> Path:
    """Build a native wrapper from an isolated Codex plugin snapshot."""
    source = Path(plugin.install_source).expanduser()
    if source.is_symlink():
        raise ValueError("Codex plugin source is symbolic")
    source = source.resolve(strict=True)
    manifest, source_manifest = _manifest(source)
    skills = _skill_paths(source, manifest)
    if not skills and not manifest.get("mcpServers"):
        raise ValueError("Codex plugin has no portable Skills or MCP servers")
    interface = manifest.get("interface")
    display_name = (
        interface.get("displayName") if isinstance(interface, dict) else ""
    )

    with staging_target("qwenpaw-codex-plugin-") as target:
        target.mkdir(mode=0o700)
        for entry in read_bounded_tree(
            source,
            required_file=str(source_manifest),
        ):
            if entry.relative in _ROOT_CONFLICTS:
                continue
            write_tree_entry(target, entry)
        write_wrapper(
            target,
            plugin,
            manifest,
            source="codex",
            adapter=ADAPTER,
            default_author="Imported from Codex",
            backend_description=(
                "Generated adapter for a Codex content plugin."
            ),
            class_name="ImportedCodexContentPlugin",
            skill_paths=[
                path.relative_to(source).as_posix() for path in skills
            ],
            display_name=display_name or plugin.name,
            enabled=enabled,
        )
    return target


__all__ = ["ADAPTER", "stage_codex_content_plugin"]
