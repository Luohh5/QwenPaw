# -*- coding: utf-8 -*-
"""Shared generation primitives for content-plugin wrappers."""

from __future__ import annotations

import json
import re
import shutil
import tempfile
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .models import SourcePlugin

_PLUGIN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def canonical_plugin_id(
    source_manifest: Mapping[str, Any],
    source_id: str,
) -> str:
    """Return the manifest ID, falling back only to the source identity."""
    value = source_manifest.get("name")
    if value is None or value == "":
        value = source_id.rsplit("@", 1)[0]
    if not isinstance(value, str):
        raise ValueError("canonical plugin id is unsafe")
    plugin_id = value.strip()
    if not _PLUGIN_ID_RE.fullmatch(plugin_id):
        raise ValueError("canonical plugin id is unsafe")
    return plugin_id


def _text(value: Any, limit: int) -> str:
    return "".join(
        character
        for character in str(value or "")[:limit]
        if ord(character) >= 32 and ord(character) != 127
    ).strip()


# One explicit renderer keeps both source adapters small and byte-compatible.
# pylint: disable=too-many-arguments,too-many-locals
def write_wrapper(
    target: Path,
    plugin: SourcePlugin,
    source_manifest: Mapping[str, Any],
    *,
    source: str,
    adapter: str,
    default_author: str,
    backend_description: str,
    class_name: str,
    skill_paths: list[str],
    display_name: Any,
    enabled: bool,
    double_quote_paths: bool = False,
    include_description_i18n: bool = False,
    description_zh: Any = "",
    migration_extra: Mapping[str, Any] | None = None,
) -> None:
    """Write a source-labelled QwenPaw manifest and Skill backend."""
    try:
        plugin_id = canonical_plugin_id(source_manifest, plugin.source_id)
    except ValueError as exc:
        raise ValueError(f"{source.title()} plugin name is unsafe") from exc
    author = source_manifest.get("author")
    if isinstance(author, dict):
        author = author.get("name") or author.get("email")
    manifest: dict[str, Any] = {
        "id": plugin_id,
        "name": _text(display_name or plugin_id, 200),
        "version": _text(
            source_manifest.get("version") or plugin.version or "0.0.0",
            100,
        ),
        "type": "general",
        "description": _text(source_manifest.get("description"), 4096),
    }
    if include_description_i18n:
        descriptions = {}
        if isinstance(description_zh, str) and description_zh.strip():
            descriptions["zh-CN"] = _text(description_zh, 4096)
        manifest["description_i18n"] = descriptions
    migration = {
        "source": source,
        "source_id": plugin.source_id,
        "adapter": adapter,
        **dict(migration_extra or {}),
        "requires_review": not enabled,
    }
    manifest["author"] = _text(author or default_author, 200)
    manifest["entry"] = {"backend": "plugin.py"}
    manifest["dependencies"] = []
    manifest["meta"] = {"migration": migration}

    calls = []
    for path in skill_paths:
        literal = json.dumps(path) if double_quote_paths else repr(path)
        calls.append(
            "        api.register_skill_provider(\n"
            f"            skills_dir=_ROOT / {literal},\n"
            f"            enabled_by_default={enabled!r},\n"
            '            channels=["all"],\n'
            "        )",
        )
    body = "\n".join(calls) or "        return None"
    backend = (
        "# -*- coding: utf-8 -*-\n"
        f'"""{backend_description}"""\n\n'
        "from pathlib import Path\n\n"
        "_ROOT = Path(__file__).parent\n\n\n"
        f"class {class_name}:\n"
        "    def register(self, api) -> None:\n"
        f"{body}\n\n\n"
        f"plugin = {class_name}()\n"
    )
    (target / "plugin.py").write_text(backend, encoding="utf-8")
    (target / "plugin.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


@contextmanager
def staging_target(prefix: str) -> Iterator[Path]:
    """Retain a completed staging tree and clean a failed one."""
    temp_root = Path(tempfile.mkdtemp(prefix=prefix))
    try:
        yield temp_root / "plugin"
    except BaseException:
        shutil.rmtree(temp_root, ignore_errors=True)
        raise


__all__ = ["canonical_plugin_id", "staging_target", "write_wrapper"]
