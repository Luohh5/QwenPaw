# -*- coding: utf-8 -*-
"""Safe local snapshots and component inventories for adaptation."""

from __future__ import annotations

import os
import re
import secrets
import shutil
import stat
from pathlib import Path
from typing import Any

from .compatibility import AssetType
from .compatibility_testing import discover_components
from .models import ProviderInventory
from .skill_transfer import read_bounded_skill_tree, read_bounded_tree

_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _target(root: Path, name: str, fallback: str) -> Path:
    slug = _SLUG_RE.sub("-", name).strip(".-")[:48] or fallback
    target, suffix = root / slug, 1
    while target.exists():
        suffix += 1
        target = root / f"{slug}-{suffix}"
    return target


def _copy(source: Path, target: Path, required_file: str) -> None:
    target.mkdir(mode=0o700)
    entries = (
        read_bounded_skill_tree(source)
        if required_file == "SKILL.md"
        else read_bounded_tree(source, required_file=required_file)
    )
    for entry in entries:
        output = target / entry.relative
        if entry.is_dir:
            output.mkdir(parents=True, mode=0o700, exist_ok=True)
            continue
        output.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        output.write_bytes(entry.data or b"")
        os.chmod(output, 0o700 if entry.mode & stat.S_IXUSR else 0o600)


def stage_local_assets(inventory: ProviderInventory, root: Path) -> list[str]:
    """Copy local Skill and plugin inputs into the private staging tree."""
    warnings: list[str] = []
    skills_root, plugins_root = root / "skills", root / "plugins"
    skills_root.mkdir(parents=True, mode=0o700, exist_ok=True)
    plugins_root.mkdir(parents=True, mode=0o700, exist_ok=True)
    for index, skill in enumerate(inventory.skills, start=1):
        source = Path(skill.directory)
        target = _target(skills_root, skill.name, f"skill-{index}")
        skill.directory = target
        try:
            _copy(source, target, "SKILL.md")
        except Exception as exc:  # pylint: disable=broad-except
            shutil.rmtree(target, ignore_errors=True)
            skill.directory = skills_root / f".failed-{secrets.token_hex(12)}"
            warnings.append(
                f"Skill {skill.name!r} 无法进入安全暂存区："
                f"{type(exc).__name__}: {exc}",
            )

    for index, plugin in enumerate(inventory.plugins, start=1):
        source_text = str(
            plugin.install_source or plugin.metadata.get("install_path") or "",
        )
        if not source_text or source_text.startswith(("http://", "https://")):
            continue
        source = Path(source_text).expanduser()
        target = _target(plugins_root, plugin.name, f"plugin-{index}")
        plugin.install_source = str(target)
        try:
            qoder_manifest = source / ".qoder-plugin" / "plugin.json"
            required = (
                ".qoder-plugin/plugin.json"
                if qoder_manifest.is_file()
                else "plugin.json"
            )
            _copy(source, target, required)
            if plugin.metadata.get("adapter") == "qoder_skill_only_v1":
                plugin.metadata["canonical_plugin_source"] = str(
                    target.resolve(),
                )
        except Exception as exc:  # pylint: disable=broad-except
            shutil.rmtree(target, ignore_errors=True)
            plugin.install_source = str(
                plugins_root / f".failed-{secrets.token_hex(12)}",
            )
            warnings.append(
                f"Plugin {plugin.name!r} 无法进入安全暂存区："
                f"{type(exc).__name__}: {exc}",
            )
    return warnings


def component_map(inventory: ProviderInventory) -> dict[str, list[Any]]:
    result: dict[str, list[Any]] = {}
    for asset_type, values in (
        (AssetType.SKILL, inventory.skills),
        (AssetType.PLUGIN, inventory.plugins),
    ):
        for value in values:
            key = f"{asset_type.value}:{value.source_id}"
            result[key] = discover_components(asset_type, value)
    return result


__all__ = ["component_map", "stage_local_assets"]
