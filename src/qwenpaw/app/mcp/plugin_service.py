# -*- coding: utf-8 -*-
"""MCP host admission and connection-free configuration routing."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException

from ...config.utils import get_config_path, load_config
from ..mcp_plugin_upgrade import PLUGIN_ID


def unavailable():
    return HTTPException(
        503,
        detail={
            "code": "mcp_plugin_unavailable",
            "message": "MCP plugin is unavailable",
        },
    )


def require_runtime(request):
    loader = getattr(request.app.state, "plugin_loader", None)
    record = loader.get_loaded_plugin(PLUGIN_ID) if loader else None
    if record is None or not record.enabled or record.status != "active":
        raise unavailable()
    return record


async def plugin_state(request):
    loader = getattr(request.app.state, "plugin_loader", None)
    record = loader.get_loaded_plugin(PLUGIN_ID) if loader else None
    if loader:
        directory = await asyncio.to_thread(
            loader.find_installed_plugin_dir,
            PLUGIN_ID,
        )
    else:
        from ...config.utils import get_plugins_dir

        directory = get_plugins_dir() / PLUGIN_ID
        if not await asyncio.to_thread((directory / "plugin.json").is_file):
            directory = None
    config = await asyncio.to_thread(load_config)
    from ...plugins.settings import is_plugin_enabled

    enabled = bool(directory or record) and is_plugin_enabled(
        config.plugins.get(PLUGIN_ID),
    )
    status = (
        "not_installed"
        if not directory and not record
        else "disabled"
        if not enabled
        else getattr(record, "status", "inactive")
    )
    if status not in {"not_installed", "disabled", "active", "failed"}:
        status = "inactive"
    return {
        "id": PLUGIN_ID,
        "installed": bool(directory or record),
        "enabled": enabled,
        "status": status,
        "diagnostics": list(getattr(record, "diagnostics", None) or []),
    }


async def configuration_workspace(request):
    """Resolve saved identity without the manager's lazy-start operation."""
    config = await asyncio.to_thread(load_config)
    agent_id = (
        getattr(request.state, "agent_id", None)
        or request.headers.get("X-Agent-Id")
        or config.agents.active_agent
        or "default"
    )
    profile = config.agents.profiles.get(agent_id)
    if profile is None:
        raise HTTPException(404, f"Agent '{agent_id}' not found")
    if not profile.enabled:
        raise HTTPException(403, f"Agent '{agent_id}' is disabled")
    manager = getattr(request.app.state, "multi_agent_manager", None)
    active = getattr(manager, "agents", {}).get(agent_id)
    # Existing live workspaces retain policy/chat metadata and write refresh.
    # Without the optional runtime, even writes use only the saved stores.
    if active is not None:
        try:
            require_runtime(request)
        except HTTPException:
            pass
        else:
            return active
    directory = Path(profile.workspace_dir).expanduser()
    if not directory.is_absolute():
        directory = get_config_path().parent / directory
    return SimpleNamespace(
        agent_id=agent_id,
        workspace_dir=directory,
        chat_manager=getattr(active, "chat_manager", None),
    )
