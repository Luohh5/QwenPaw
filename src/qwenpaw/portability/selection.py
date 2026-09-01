# -*- coding: utf-8 -*-
"""Select the user-approved subset of a provider inventory."""

from __future__ import annotations

from typing import Any

from .models import ImportSelection, ProviderInventory

_FIELDS = {
    "memory": "memory_projects",
    "cron": "scheduled_tasks",
    "skills": "skills",
    "mcp": "mcp_servers",
    "plugins": "plugins",
}


def _selected(values: list[Any], ids: set[str], label: str) -> list[Any]:
    available = {item.source_id for item in values}
    unknown = ids - available
    if unknown:
        raise ValueError(f"unknown {label} selection: {sorted(unknown)[0]}")
    return [item for item in values if item.source_id in ids]


def select_inventory(
    inventory: ProviderInventory,
    selection: ImportSelection,
) -> ProviderInventory:
    """Return a deep copy containing only selected assets."""
    chosen = {key: set(getattr(selection, key)) for key in _FIELDS}
    for server in inventory.mcp_servers:
        parent = str(server.metadata.get("source_plugin") or "")
        if (
            server.source_id in chosen["mcp"]
            and server.metadata.get("source_plugin_relative_cwd")
            and parent not in chosen["plugins"]
        ):
            raise ValueError(
                f"plugin-owned MCP {server.source_id} requires {parent}",
            )

    tasks = _selected(
        inventory.scheduled_tasks,
        chosen["cron"],
        "cron",
    )
    if not selection.sessions and any(
        str(item.metadata.get("source_kind") or "").lower() == "heartbeat"
        for item in tasks
    ):
        raise ValueError("heartbeat selection requires sessions")

    updates = {
        field: _selected(getattr(inventory, field), chosen[key], key)
        for key, field in _FIELDS.items()
    }
    selected_plugins = updates["plugins"]
    marketplace_refs = {item.marketplace for item in selected_plugins}
    updates["marketplaces"] = [
        item
        for item in inventory.marketplaces
        if item.source_id in marketplace_refs or item.name in marketplace_refs
    ]
    updates["sessions"] = (
        list(inventory.sessions) if selection.sessions else []
    )
    updates["ignored_session_ids"] = (
        list(inventory.ignored_session_ids) if selection.sessions else []
    )
    return inventory.model_copy(update=updates, deep=True)


__all__ = ["select_inventory"]
