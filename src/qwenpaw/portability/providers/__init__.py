# -*- coding: utf-8 -*-
"""Migration Provider registry."""

from __future__ import annotations

from typing import Any

from .base import MigrationProvider
from .codex import CodexMigrationProvider
from .qoder import QoderMigrationProvider

_ALIASES = {
    "codex": "codex",
    "openai-codex": "codex",
    "qoder": "qoder",
}


def provider_names() -> tuple[str, ...]:
    """Return canonical source names currently supported for migration."""
    return ("codex", "qoder")


def create_migration_provider(
    source: str,
    workspace: Any,
) -> MigrationProvider:
    """Create one read-only provider or raise a user-actionable error."""
    provider_id = _ALIASES.get(source.strip().lower())
    if provider_id == "codex":
        return CodexMigrationProvider(workspace)
    if provider_id == "qoder":
        return QoderMigrationProvider(workspace)
    supported = ", ".join(provider_names())
    raise ValueError(
        f"Unsupported import source {source!r}. Supported providers: "
        f"{supported}; or pass a QwenPaw backup .zip path.",
    )


__all__ = [
    "MigrationProvider",
    "create_migration_provider",
    "provider_names",
]
