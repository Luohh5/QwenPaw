# -*- coding: utf-8 -*-
"""Constrained translation for user-authored Qoder Skill-only plugins."""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

from .models import SourcePlugin

_MAX_PLUGIN_FILES = 5000
_MAX_PLUGIN_BYTES = 64 * 1024 * 1024
_QODER_NATIVE_PLUGIN_FEATURES = {
    "agents",
    "canvas",
    "commands",
    "hooks",
    "mcp",
    "mcpServers",
    "tools",
}
_QODER_BINDING_MARKERS = (
    b".qoder",
    b"qoder_",
    b"qoder-",
    b"sharedclientcache",
)


def _has_skill_directory(root: Path) -> bool:
    if not root.is_dir() or root.is_symlink():
        return False
    if (root / "SKILL.md").is_file():
        return True
    try:
        children = root.iterdir()
    except OSError:
        return False
    for child in children:
        if not child.is_dir() or child.is_symlink():
            continue
        if (child / "SKILL.md").is_file():
            return True
    return False


def _contains_qoder_bindings(skills_root: Path) -> bool:
    for path in sorted(skills_root.rglob("*")):
        if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
            continue
        try:
            content = path.read_bytes().lower()
        except OSError:
            continue
        if any(marker in content for marker in _QODER_BINDING_MARKERS):
            return True
    return False


# pylint: disable-next=too-many-return-statements
def discover_qoder_custom_skill_adapter(
    qoder_home: Path,
    install_path: Path | None,
    manifest: Any,
    record: dict[str, Any],
) -> dict[str, Any] | None:
    """Describe a safe local Qoder Skill-only plugin translation.

    Qoder's ``custom`` ledger entries are canonical user-owned plugin sources,
    unlike ``plugins/cache``. The importer repeats these checks immediately
    before staging so discovery metadata cannot relax the security boundary.
    """
    if install_path is None or not isinstance(manifest, dict):
        return None
    # Current Qoder releases omit ``source`` for local-custom records; older
    # releases wrote ``source: custom``.
    if str(record.get("source") or "") not in {"", "custom"}:
        return None
    try:
        custom_root = (qoder_home / "plugins" / "custom").resolve(strict=True)
        source = install_path.resolve(strict=True)
    except OSError:
        return None
    if (
        install_path.is_symlink()
        or not source.is_dir()
        or not source.is_relative_to(custom_root)
    ):
        return None
    skills_value = manifest.get("skills")
    if not isinstance(skills_value, str) or not skills_value.strip():
        return None
    try:
        skills_root = (source / skills_value).resolve(strict=True)
    except OSError:
        return None
    if (
        not skills_root.is_dir()
        or not skills_root.is_relative_to(source)
        or not _has_skill_directory(skills_root)
    ):
        return None
    if any(manifest.get(key) for key in _QODER_NATIVE_PLUGIN_FEATURES):
        return None
    for path in skills_root.rglob("*"):
        if path.is_symlink():
            return None
    harness_bound = _contains_qoder_bindings(skills_root)
    return {
        "adapter": "qoder_skill_only_v1",
        "canonical_custom_root": str(custom_root),
        "skills_relative_path": str(skills_root.relative_to(source)),
        "harness_bound": harness_bound,
        "skills_enabled_by_default": not harness_bound,
    }


def _validated_custom_source(plugin: SourcePlugin) -> Path:
    source = Path(plugin.install_source).expanduser()
    custom_root_raw = str(plugin.metadata.get("canonical_custom_root") or "")
    if not custom_root_raw:
        raise ValueError("Qoder custom plugin provenance is missing")
    custom_root = Path(custom_root_raw).expanduser().resolve(strict=True)
    if source.is_symlink():
        raise ValueError("Qoder custom plugin source is symbolic")
    source = source.resolve(strict=True)
    if not source.is_dir() or not source.is_relative_to(custom_root):
        raise ValueError("Qoder custom plugin source escaped plugins/custom")
    return source


def _read_qoder_manifest(source: Path) -> dict[str, Any]:
    path = source / ".qoder-plugin" / "plugin.json"
    if path.is_symlink() or not path.is_file():
        raise ValueError("Qoder custom plugin manifest is unavailable")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("Qoder custom plugin manifest is invalid")
    if any(manifest.get(key) for key in _QODER_NATIVE_PLUGIN_FEATURES):
        raise ValueError(
            "Qoder-native tools/hooks/MCP/commands cannot be auto-adapted",
        )
    return manifest


def _validated_skills_root(
    source: Path,
    manifest: dict[str, Any],
) -> Path:
    raw = manifest.get("skills")
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("Qoder custom plugin has no Skill-only source")
    relative = Path(raw)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Qoder custom plugin Skills path is unsafe")
    root = (source / relative).resolve(strict=True)
    if not root.is_dir() or not root.is_relative_to(source):
        raise ValueError("Qoder custom plugin Skills path escaped its source")
    if not _has_skill_directory(root):
        raise ValueError("Qoder custom plugin contains no usable Skills")
    return root


def _validate_source_tree(source: Path) -> None:
    count = 0
    total = 0
    for entry in sorted(source.rglob("*")):
        if entry.is_symlink():
            raise ValueError(f"Qoder custom plugin contains a link: {entry}")
        if not entry.is_file():
            continue
        if not entry.resolve(strict=True).is_relative_to(source):
            raise ValueError(f"Qoder custom plugin file escaped: {entry}")
        count += 1
        total += entry.stat().st_size
        if count > _MAX_PLUGIN_FILES or total > _MAX_PLUGIN_BYTES:
            raise ValueError(
                "Qoder custom plugin exceeds the 5,000 file / 64 MiB limit",
            )


def _plugin_backend(enabled: bool) -> str:
    return (
        "# -*- coding: utf-8 -*-\n"
        '"""Generated adapter for a Qoder Skill-only plugin."""\n\n'
        "from pathlib import Path\n\n"
        "_ROOT = Path(__file__).parent\n\n\n"
        "class ImportedQoderSkillPlugin:\n"
        "    def register(self, api) -> None:\n"
        "        api.register_skill_provider(\n"
        '            skills_dir=_ROOT / "skills",\n'
        f"            enabled_by_default={enabled!r},\n"
        '            channels=["all"],\n'
        "        )\n\n\n"
        "plugin = ImportedQoderSkillPlugin()\n"
    )


def _author(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("name") or value.get("email") or ""
    return str(value or "Imported from Qoder")


def _qwenpaw_manifest(
    plugin: SourcePlugin,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    plugin_id = str(manifest.get("name") or plugin.name).strip()
    if not plugin_id or "/" in plugin_id or "\\" in plugin_id:
        raise ValueError("Qoder custom plugin name is unsafe")
    descriptions = {}
    description_zh = manifest.get("descriptionZh")
    if isinstance(description_zh, str) and description_zh.strip():
        descriptions["zh-CN"] = description_zh.strip()
    return {
        "id": plugin_id,
        "name": str(manifest.get("displayName") or plugin_id),
        "version": str(manifest.get("version") or plugin.version or "0.0.0"),
        "type": "general",
        "description": str(manifest.get("description") or ""),
        "description_i18n": descriptions,
        "author": _author(manifest.get("author")),
        "entry": {"backend": "plugin.py"},
        "dependencies": [],
        "meta": {
            "migration": {
                "source": "qoder",
                "source_id": plugin.source_id,
                "adapter": "qoder_skill_only_v1",
                "harness_bound": bool(plugin.metadata.get("harness_bound")),
                "requires_review": True,
            },
        },
    }


def stage_qoder_skill_plugin(plugin: SourcePlugin) -> Path:
    """Build a review-safe QwenPaw wrapper for a Qoder Skill-only plugin."""
    source = _validated_custom_source(plugin)
    manifest = _read_qoder_manifest(source)
    skills_root = _validated_skills_root(source, manifest)
    _validate_source_tree(source)
    temp_root = Path(tempfile.mkdtemp(prefix="qwenpaw-qoder-plugin-"))
    target = temp_root / "plugin"
    try:
        target.mkdir()
        shutil.copytree(skills_root, target / "skills")
        readme = source / "README.md"
        if readme.is_file() and not readme.is_symlink():
            shutil.copy2(readme, target / "README.qoder.md")
        enabled = bool(plugin.metadata.get("skills_enabled_by_default", False))
        (target / "plugin.py").write_text(
            _plugin_backend(enabled),
            encoding="utf-8",
        )
        (target / "plugin.json").write_text(
            json.dumps(
                _qwenpaw_manifest(plugin, manifest),
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return target
    except BaseException:
        shutil.rmtree(temp_root, ignore_errors=True)
        raise


__all__ = [
    "discover_qoder_custom_skill_adapter",
    "stage_qoder_skill_plugin",
]
