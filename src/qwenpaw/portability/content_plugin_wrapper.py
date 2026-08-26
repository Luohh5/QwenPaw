# -*- coding: utf-8 -*-
"""Shared contracts for generated content-plugin wrappers."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

_PLUGIN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def canonical_plugin_id(
    source_manifest: Mapping[str, Any],
    source_id: str,
) -> str:
    """Return the stable source identity used by generated QwenPaw plugins."""
    value = source_manifest.get("name")
    if value is None or value == "":
        value = source_id.rsplit("@", 1)[0]
    if not isinstance(value, str):
        raise ValueError("canonical plugin id is unsafe")
    plugin_id = value.strip()
    if not _PLUGIN_ID_RE.fullmatch(plugin_id):
        raise ValueError("canonical plugin id is unsafe")
    return plugin_id


__all__ = ["canonical_plugin_id"]
