# -*- coding: utf-8 -*-
"""Command-facing backup and trace export services."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..backup import create_stream, export_backup
from ..backup.models import BackupScope, CreateBackupRequest
from .trace import export_trace


async def export_to_backup(workspace: Any):
    """Create a normal QwenPaw backup for the current Agent."""
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
    request = CreateBackupRequest(
        name=f"PawBundle {workspace.agent_id} {stamp}",
        description="Created by /export to backup",
        scope=BackupScope(
            include_agents=True,
            include_global_config=True,
            include_secrets=False,
            include_skill_pool=True,
        ),
        agents=[workspace.agent_id],
    )
    completed = None
    async for event in create_stream(request):
        if event.get("type") == "error":
            raise RuntimeError(str(event.get("message") or "Backup failed"))
        if event.get("type") == "done":
            completed = event.get("meta")
    if not completed:
        raise RuntimeError("Backup did not produce a completed archive.")
    backup_id = str(completed.get("id") or "")
    path, name = await export_backup(backup_id)
    return completed, path, name


__all__ = ["export_to_backup", "export_trace"]
