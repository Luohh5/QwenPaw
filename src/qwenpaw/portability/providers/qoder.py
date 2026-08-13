# -*- coding: utf-8 -*-
"""Qoder Migration Provider for SDK and current Qoder IDE sessions."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from ..models import ProviderInventory, SourceSession
from .base import ProgressReporter
from .external_state import discover_project_memory, discover_qoder_plugins
from .qoder_sessions import (
    default_qoder_user_data,
    discover_qoder_transcripts,
    load_qoder_index,
    read_qoder_transcript,
)

_SESSION_TIMEOUT_SECONDS = 60
_READ_CONCURRENCY = 4


def _milestone(index: int, total: int) -> bool:
    if total <= 20:
        return True
    step = max(1, total // 20)
    return index == 1 or index == total or index % step == 0


class QoderMigrationProvider:  # pylint: disable=too-few-public-methods
    """Read both Qoder IDE and Qoder Agent SDK local session layouts."""

    provider_id = "qoder"

    def __init__(
        self,
        workspace: Any,
        *,
        qoder_home: Path | None = None,
        qoder_user_data: Path | None = None,
    ) -> None:
        self._workspace = workspace
        self._qoder_home = qoder_home or (Path.home() / ".qoder")
        self._qoder_user_data = qoder_user_data or default_qoder_user_data()

    # pylint: disable-next=too-many-locals,too-many-statements
    async def inventory(
        self,
        *,
        limit: int,
        progress: ProgressReporter | None = None,
    ) -> ProviderInventory:
        """Discover and normalize local Qoder JSONL sessions."""
        discovery = await asyncio.gather(
            asyncio.to_thread(discover_project_memory, self._qoder_home),
            asyncio.to_thread(discover_qoder_plugins, self._qoder_home),
            asyncio.to_thread(discover_qoder_transcripts, self._qoder_home),
            asyncio.to_thread(load_qoder_index, self._qoder_user_data),
        )
        memory_projects, plugin_state, records, index_state = discovery
        marketplaces, plugins = plugin_state
        qoder_index, index_warnings = index_state

        total_records = len(records)
        records = records[:limit]
        sessions: list[SourceSession] = []
        warnings: list[str] = list(index_warnings)
        warnings.append(
            "Qoder settings, hooks, agents, tool policies, credentials and "
            "runtime state are harness-bound and were not copied.",
        )
        if total_records > limit:
            warnings.append(
                f"Qoder session safety limit ({limit}) was reached; older "
                "sessions beyond this bounded import were not read.",
            )
        if progress is not None:
            await progress(
                f"发现 {total_records} 个 Qoder 会话文件，本次读取 "
                f"{len(records)} 个（最多 4 个同时读取）…",
            )

        semaphore = asyncio.Semaphore(_READ_CONCURRENCY)
        progress_lock = asyncio.Lock()
        completed = 0

        async def _read_one(
            record: Any,
        ) -> tuple[SourceSession | None, list[str]]:
            nonlocal completed
            try:
                async with semaphore:
                    result = await asyncio.wait_for(
                        asyncio.to_thread(
                            read_qoder_transcript,
                            record,
                            qoder_index,
                        ),
                        timeout=_SESSION_TIMEOUT_SECONDS,
                    )
            except asyncio.TimeoutError:
                result = (
                    None,
                    [
                        f"Could not read Qoder session {record.source_id}: "
                        "timed out after 60s.",
                    ],
                )
            except Exception as exc:  # pylint: disable=broad-except
                warning = "Could not read Qoder session "
                warning += f"{record.source_id}: {exc}"
                result = (
                    None,
                    [warning],
                )
            async with progress_lock:
                completed += 1
                if progress is not None and _milestone(
                    completed,
                    len(records),
                ):
                    await progress(
                        f"Qoder 会话读取进度：{completed}/{len(records)}",
                    )
            return result

        results = await asyncio.gather(
            *(_read_one(record) for record in records),
        )
        for session, session_warnings in results:
            if session is not None:
                sessions.append(session)
            warnings.extend(session_warnings)

        # The filename is normally the session id. De-duplicate once more by
        # the authoritative id stored inside JSONL for copied/renamed files.
        unique_sessions: dict[str, SourceSession] = {}
        for session in sessions:
            unique_sessions.setdefault(session.source_id, session)
        sessions = list(unique_sessions.values())

        if memory_projects:
            warnings.append(
                f"Prepared {len(memory_projects)} Qoder memory scope(s) as "
                "project-scoped source resources, without merging them into "
                "QwenPaw MEMORY.md.",
            )
        if plugins:
            compatible = sum(bool(item.install_source) for item in plugins)
            warnings.append(
                f"Found {len(plugins)} enabled Qoder plugin(s) across "
                f"{len(marketplaces)} Marketplace source(s); {compatible} "
                "expose a QwenPaw-compatible native install source. Qoder "
                "cache directories are never copied.",
            )
        projects = self._qoder_home / "projects"
        return ProviderInventory(
            provider_id=self.provider_id,
            provider_name="Qoder",
            detected=(
                bool(records)
                or projects.is_dir()
                or bool(memory_projects)
                or bool(plugins)
            ),
            locator=str(projects),
            sessions=sessions,
            memory_projects=memory_projects,
            marketplaces=marketplaces,
            plugins=plugins,
            warnings=warnings,
        )


__all__ = ["QoderMigrationProvider"]
