# -*- coding: utf-8 -*-
"""Read-only discovery for external memory and plugin source state.

Installed plugin caches are intentionally used only as metadata.  A plugin is
installable only when its Marketplace resolves to an independent source that
already contains a valid QwenPaw ``plugin.json`` entry point.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

from ..models import (
    SourceMarketplace,
    SourceMemoryFile,
    SourceMemoryProject,
    SourcePlugin,
)

_CODEX_BUILTIN_MARKETPLACES = {
    "openai-bundled",
    "openai-curated",
    "openai-curated-remote",
    "openai-primary-runtime",
}
_CWD_KEYS = ("cwd", "directory", "project_path", "projectPath")
_MAX_TRANSCRIPT_PROBE_BYTES = 1024 * 1024


def _read_json(path: Path) -> Any:
    try:
        with path.open(encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def _markdown_files(root: Path) -> list[SourceMemoryFile]:
    files: list[SourceMemoryFile] = []
    if not root.is_dir() or root.is_symlink():
        return files
    resolved_root = root.resolve()
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        if path.suffix.lower() != ".md":
            continue
        try:
            resolved = path.resolve(strict=True)
            relative = resolved.relative_to(resolved_root)
        except (OSError, ValueError):
            continue
        files.append(
            SourceMemoryFile(
                source_path=resolved,
                relative_path=relative,
            ),
        )
    return files


def discover_codex_memory(codex_home: Path) -> list[SourceMemoryProject]:
    """Discover Codex's global memory and scoped extension resources."""
    memories_root = codex_home.expanduser() / "memories"
    projects: list[SourceMemoryProject] = []
    if not memories_root.is_dir():
        return projects

    global_files = []
    for path in sorted(memories_root.glob("*.md")):
        if path.is_symlink() or not path.is_file():
            continue
        global_files.append(
            SourceMemoryFile(
                source_path=path.resolve(),
                relative_path=Path(path.name),
            ),
        )
    if global_files:
        projects.append(
            SourceMemoryProject(
                source_id="codex:global",
                project_key="global",
                files=global_files,
                metadata={"layout": "codex_global_memory"},
            ),
        )

    extensions_root = memories_root / "extensions"
    if not extensions_root.is_dir():
        return projects
    for extension in sorted(extensions_root.iterdir()):
        resources = extension / "resources"
        if extension.is_symlink() or not resources.is_dir():
            continue
        for project_root in sorted(resources.iterdir()):
            if project_root.is_symlink() or not project_root.is_dir():
                continue
            files = _markdown_files(project_root)
            if not files:
                continue
            scope = _read_json(project_root / "scope.json")
            cwd = (
                str(scope.get("cwd") or "") if isinstance(scope, dict) else ""
            )
            projects.append(
                SourceMemoryProject(
                    source_id=(
                        f"codex:extension:{extension.name}:"
                        f"{project_root.name}"
                    ),
                    project_key=f"{extension.name}-{project_root.name}",
                    cwd=cwd,
                    files=files,
                    metadata={
                        "layout": "codex_extension_resource",
                        "extension": extension.name,
                        "source_project_key": project_root.name,
                    },
                ),
            )
    return projects


def _absolute_cwd(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    path = Path(value).expanduser()
    return str(path) if path.is_absolute() else ""


def _cwd_in_value(value: Any) -> str:
    if isinstance(value, dict):
        for key in _CWD_KEYS:
            cwd = _absolute_cwd(value.get(key))
            if cwd:
                return cwd
        for child in value.values():
            cwd = _cwd_in_value(child)
            if cwd:
                return cwd
    elif isinstance(value, list):
        for child in value:
            cwd = _cwd_in_value(child)
            if cwd:
                return cwd
    return ""


def _project_cwd_from_transcripts(project_root: Path) -> str:
    candidates = sorted(
        project_root.rglob("*.jsonl"),
        key=lambda path: path.stat().st_mtime if path.is_file() else 0,
        reverse=True,
    )
    for path in candidates[:20]:
        if path.is_symlink() or not path.is_file():
            continue
        consumed = 0
        try:
            with path.open(encoding="utf-8", errors="replace") as stream:
                for line in stream:
                    consumed += len(line.encode("utf-8", errors="replace"))
                    if consumed > _MAX_TRANSCRIPT_PROBE_BYTES:
                        break
                    try:
                        value = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    cwd = _cwd_in_value(value)
                    if cwd:
                        return cwd
        except OSError:
            continue
    return ""


def discover_project_memory(agent_home: Path) -> list[SourceMemoryProject]:
    """Discover Qoder/Claude-style ``projects/*/memory/**/*.md`` stores."""
    projects_root = agent_home.expanduser() / "projects"
    projects: list[SourceMemoryProject] = []
    if not projects_root.is_dir():
        return projects
    for project_root in sorted(projects_root.iterdir()):
        if project_root.is_symlink() or not project_root.is_dir():
            continue
        files = _markdown_files(project_root / "memory")
        if not files:
            continue
        projects.append(
            SourceMemoryProject(
                source_id=f"project-memory:{project_root.name}",
                project_key=project_root.name,
                cwd=_project_cwd_from_transcripts(project_root),
                files=files,
                metadata={"layout": "project_memory"},
            ),
        )
    return projects


def _marketplace_source(config: dict[str, Any], base: Path) -> tuple[str, str]:
    source_type = str(config.get("source_type") or config.get("type") or "")
    source = str(
        config.get("source") or config.get("path") or config.get("url") or "",
    ).strip()
    if source and source_type in {"directory", "local", "path"}:
        path = Path(source).expanduser()
        if not path.is_absolute():
            path = base / path
        source = str(path.resolve())
    return source_type or "unknown", source


def _marketplace_manifest(root: Path) -> Path | None:
    candidates = (
        root / ".codex-plugin" / "marketplace.json",
        root / ".qoder-plugin" / "marketplace.json",
        root / "marketplace.json",
    )
    return next((path for path in candidates if path.is_file()), None)


def _qwen_plugin_source(  # pylint: disable=too-many-return-statements
    root: Path,
    plugin_name: str,
) -> str:
    """Resolve only a Marketplace entry containing QwenPaw plugin.json."""
    manifest_path = _marketplace_manifest(root)
    if manifest_path is None:
        direct = root / plugin_name
        return (
            str(direct.resolve()) if (direct / "plugin.json").is_file() else ""
        )
    manifest = _read_json(manifest_path)
    entries = manifest.get("plugins") if isinstance(manifest, dict) else None
    if not isinstance(entries, list):
        return ""
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("name") or entry.get("id") or "") != plugin_name:
            continue
        raw_source = entry.get("source")
        if isinstance(raw_source, dict):
            raw_source = (
                raw_source.get("path")
                or raw_source.get("url")
                or raw_source.get("source")
            )
        if not isinstance(raw_source, str) or not raw_source.strip():
            return ""
        if raw_source.startswith(("http://", "https://")):
            return raw_source if raw_source.lower().endswith(".zip") else ""
        source_path = Path(raw_source).expanduser()
        if not source_path.is_absolute():
            source_path = manifest_path.parent / source_path
        try:
            source_path = source_path.resolve()
        except OSError:
            return ""
        return (
            str(source_path) if (source_path / "plugin.json").is_file() else ""
        )
    return ""


def _cached_plugin_version(
    cache_root: Path,
    marketplace: str,
    name: str,
) -> str:
    plugin_root = cache_root / marketplace / name
    manifests = list(plugin_root.glob("*/.codex-plugin/plugin.json"))
    if not manifests:
        return ""
    manifests.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    manifest = _read_json(manifests[0])
    return (
        str(manifest.get("version") or "")
        if isinstance(manifest, dict)
        else ""
    )


# pylint: disable-next=too-many-locals,too-many-branches
def discover_codex_plugins(
    codex_home: Path,
) -> tuple[list[SourceMarketplace], list[SourcePlugin]]:
    """Read enabled Codex plugin IDs and their Marketplace declarations."""
    codex_home = codex_home.expanduser()
    config_path = codex_home / "config.toml"
    try:
        with config_path.open("rb") as stream:
            config = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError):
        config = {}
    plugin_config = config.get("plugins")
    if not isinstance(plugin_config, dict):
        plugin_config = {}
    marketplace_config = config.get("marketplaces")
    if not isinstance(marketplace_config, dict):
        marketplace_config = {}

    enabled_ids: list[str] = []
    for plugin_id, value in plugin_config.items():
        enabled = value if isinstance(value, bool) else False
        if isinstance(value, dict):
            enabled = bool(value.get("enabled", False))
        if enabled and "@" in str(plugin_id):
            enabled_ids.append(str(plugin_id))

    remote_ids: set[str] = set()
    cache_root = codex_home / "plugins" / "cache"
    for marker in cache_root.glob("*/*/.codex-remote-plugin-install.json"):
        if marker.is_symlink() or not marker.is_file():
            continue
        plugin_id = f"{marker.parent.name}@{marker.parent.parent.name}"
        remote_ids.add(plugin_id)
        if plugin_id not in enabled_ids:
            enabled_ids.append(plugin_id)

    marketplace_names = sorted(
        {item.rsplit("@", 1)[1] for item in enabled_ids},
    )
    marketplaces: list[SourceMarketplace] = []
    resolved_roots: dict[str, Path] = {}
    for name in marketplace_names:
        raw = marketplace_config.get(name)
        if isinstance(raw, dict):
            source_type, source = _marketplace_source(raw, codex_home)
            ref_name = str(raw.get("ref") or "")
        else:
            source_type = (
                "builtin" if name in _CODEX_BUILTIN_MARKETPLACES else "unknown"
            )
            source = ""
            ref_name = ""
        source_path = Path(source).expanduser() if source else None
        if source_path is not None and source_path.is_dir():
            resolved_roots[name] = source_path.resolve()
        marketplaces.append(
            SourceMarketplace(
                source_id=f"codex:{name}",
                name=name,
                source=source,
                source_type=source_type,
                ref_name=ref_name,
            ),
        )

    plugins: list[SourcePlugin] = []
    for plugin_id in sorted(enabled_ids):
        name, marketplace = plugin_id.rsplit("@", 1)
        root = resolved_roots.get(marketplace)
        plugins.append(
            SourcePlugin(
                source_id=plugin_id,
                name=name,
                marketplace=marketplace,
                version=_cached_plugin_version(
                    cache_root,
                    marketplace,
                    name,
                ),
                install_source=(
                    _qwen_plugin_source(root, name) if root is not None else ""
                ),
                metadata={
                    "source_manifest": "codex",
                    "remote_install": plugin_id in remote_ids,
                },
            ),
        )
    return marketplaces, plugins


def discover_qoder_plugins(
    qoder_home: Path,
) -> tuple[list[SourceMarketplace], list[SourcePlugin]]:
    """Read Qoder's installed-plugin ledger without copying its cache."""
    qoder_home = qoder_home.expanduser()
    ledger = _read_json(qoder_home / "plugins" / "installed_plugins_v2.json")
    records = ledger.get("plugins") if isinstance(ledger, dict) else None
    if not isinstance(records, dict):
        return [], []
    plugins: list[SourcePlugin] = []
    marketplace_names: set[str] = set()
    for plugin_id, installs in sorted(records.items()):
        if "@" not in str(plugin_id) or not isinstance(installs, list):
            continue
        enabled_records = [
            item
            for item in installs
            if isinstance(item, dict) and bool(item.get("enabled", True))
        ]
        if not enabled_records:
            continue
        name, marketplace = str(plugin_id).rsplit("@", 1)
        marketplace_names.add(marketplace)
        version = str(enabled_records[0].get("version") or "")
        marketplace_root = (
            qoder_home / "plugins" / "marketplaces" / marketplace
        )
        plugins.append(
            SourcePlugin(
                source_id=str(plugin_id),
                name=name,
                marketplace=marketplace,
                version=version,
                install_source=(
                    _qwen_plugin_source(marketplace_root, name)
                    if marketplace_root.is_dir()
                    else ""
                ),
                metadata={"source_manifest": "qoder"},
            ),
        )
    marketplaces = []
    for name in sorted(marketplace_names):
        root = qoder_home / "plugins" / "marketplaces" / name
        marketplaces.append(
            SourceMarketplace(
                source_id=f"qoder:{name}",
                name=name,
                source=str(root.resolve()) if root.is_dir() else "",
                source_type="directory" if root.is_dir() else "builtin",
            ),
        )
    return marketplaces, plugins


__all__ = [
    "discover_codex_memory",
    "discover_codex_plugins",
    "discover_project_memory",
    "discover_qoder_plugins",
]
