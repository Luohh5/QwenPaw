# -*- coding: utf-8 -*-
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from qwenpaw.portability import exporter


@pytest.mark.asyncio
async def test_backup_export_reuses_safe_current_agent_scope(
    tmp_path: Path,
    monkeypatch,
) -> None:
    seen = []

    async def fake_create_stream(request):
        seen.append(request)
        yield {"type": "progress", "message": "working"}
        yield {"type": "done", "meta": {"id": "backup-id"}}

    async def fake_export_backup(backup_id):
        assert backup_id == "backup-id"
        return tmp_path / "backup.zip", "backup.zip"

    monkeypatch.setattr(exporter, "create_stream", fake_create_stream)
    monkeypatch.setattr(exporter, "export_backup", fake_export_backup)
    workspace = SimpleNamespace(agent_id="agent-1")

    completed, path, name = await exporter.export_to_backup(workspace)

    assert completed == {"id": "backup-id"}
    assert path == tmp_path / "backup.zip"
    assert name == "backup.zip"
    assert len(seen) == 1
    request = seen[0]
    assert request.agents == ["agent-1"]
    assert request.scope.include_agents is True
    assert request.scope.include_global_config is True
    assert request.scope.include_skill_pool is True
    assert request.scope.include_secrets is False
