# -*- coding: utf-8 -*-
"""Qoder Migration Provider for SDK and current Qoder IDE sessions."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from ..models import ProviderInventory, SourceLocation, SourceSession
from .base import ProgressReporter
from .external_state import (
    discover_qoder_mcp,
    discover_qoder_memory,
    discover_qoder_plugin_mcp,
    discover_qoder_plugins,
    discover_qoder_skills,
)
from .qoder_sessions import (
    default_qoder_user_data,
    discover_qoder_transcripts,
    load_qoder_index,
    read_qoder_transcript,
)
from .qoder_schedules import discover_qoder_scheduled_tasks
from .locator import resolve_source_location

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
        source_location: SourceLocation | None = None,
    ) -> None:
        self._workspace = workspace
        if source_location is None:
            source_location = resolve_source_location(
                "qoder",
                source_home=qoder_home,
            )
        if qoder_home is not None:
            source_location.data_home = str(qoder_home.expanduser())
            source_location.data_home_source = "injected"
            source_location.data_home_exists = qoder_home.is_dir()
        if qoder_user_data is not None:
            source_location.user_data_home = str(qoder_user_data.expanduser())
            source_location.user_data_home_source = "injected"
            source_location.user_data_home_exists = qoder_user_data.is_dir()
        self._source_location = source_location
        self._qoder_home = Path(source_location.data_home)
        self._qoder_user_data = (
            Path(source_location.user_data_home)
            if source_location.user_data_home
            else default_qoder_user_data()
        )

    # pylint: disable-next=R0914,R0915,R0912
    async def inventory(
        self,
        *,
        limit: int,
        progress: ProgressReporter | None = None,
    ) -> ProviderInventory:
        """Discover and normalize local Qoder JSONL sessions."""
        discovery = await asyncio.gather(
            asyncio.to_thread(discover_qoder_memory, self._qoder_home),
            asyncio.to_thread(discover_qoder_plugins, self._qoder_home),
            asyncio.to_thread(discover_qoder_skills, self._qoder_home),
            asyncio.to_thread(discover_qoder_mcp, self._qoder_home),
            asyncio.to_thread(discover_qoder_transcripts, self._qoder_home),
            asyncio.to_thread(load_qoder_index, self._qoder_user_data),
            asyncio.to_thread(
                discover_qoder_scheduled_tasks,
                self._qoder_user_data,
            ),
        )
        (
            memory_projects,
            plugin_state,
            skills,
            mcp_state,
            records,
            index_state,
            scheduled_task_state,
        ) = discovery
        marketplaces, plugins = plugin_state
        mcp_servers, mcp_warnings, discovered_mcp_count = mcp_state
        (
            plugin_mcp_servers,
            plugin_mcp_warnings,
            plugin_mcp_count,
        ) = await asyncio.to_thread(discover_qoder_plugin_mcp, plugins)
        mcp_servers.extend(plugin_mcp_servers)
        mcp_warnings.extend(plugin_mcp_warnings)
        discovered_mcp_count += plugin_mcp_count
        qoder_index, index_warnings = index_state
        (
            scheduled_tasks,
            scheduled_task_warnings,
            discovered_scheduled_task_count,
        ) = scheduled_task_state

        total_records = len(records)
        sessions: list[SourceSession] = []
        warnings: list[str] = [
            *index_warnings,
            *mcp_warnings,
            *scheduled_task_warnings,
        ]
        warnings.append(
            "Qoder built-in IDE runtime, credentials and tool policies are "
            "not copied. Components of enabled third-party plugins enter "
            "the compatibility Goal as one plugin asset for Agent review.",
        )
        if progress is not None:
            await progress(
                f"发现 {total_records} 个 Qoder 会话候选文件，正在区分 "
                "用户会话与内部 Agent 轨迹（最多 4 个同时读取）…",
            )

        semaphore = asyncio.Semaphore(_READ_CONCURRENCY)
        progress_lock = asyncio.Lock()
        completed = 0

        async def _read_one(
            record: Any,
        ) -> tuple[SourceSession | None, list[str], bool]:
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
                    False,
                )
            except Exception as exc:  # pylint: disable=broad-except
                warning = "Could not read Qoder session "
                warning += f"{record.source_id}: {exc}"
                result = (
                    None,
                    [warning],
                    False,
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
        ignored_session_ids: list[str] = []
        for record, result in zip(records, results):
            session, session_warnings, internal_trace = result
            if session is not None:
                sessions.append(session)
            if internal_trace:
                ignored_session_ids.append(record.source_id)
            warnings.extend(session_warnings)

        # The filename is normally the session id. De-duplicate once more by
        # the authoritative id stored inside JSONL for copied/renamed files.
        unique_sessions: dict[str, SourceSession] = {}
        for session in sessions:
            unique_sessions.setdefault(session.source_id, session)
        sessions = list(unique_sessions.values())
        visible_total = len(sessions)
        if visible_total > limit:
            sessions = sessions[:limit]
            warnings.append(
                f"Qoder session safety limit ({limit}) was reached; older "
                "user-visible sessions beyond this bounded import were not "
                "imported.",
            )
        if ignored_session_ids:
            warnings.append(
                f"Ignored {len(ignored_session_ids)} Qoder internal "
                "Agent/Experts execution traces. Their worker names, roles "
                "and final results remain available in parent conversations.",
            )
        if progress is not None:
            await progress(
                f"识别出 {visible_total} 个用户可见 Qoder 会话；已忽略 "
                f"{len(ignored_session_ids)} 条内部 Agent 执行轨迹。",
            )

        if memory_projects:
            scoped_memory = [
                item
                for item in memory_projects
                if item.metadata.get("scope") == "project"
            ]
            missing_cwd = sum(not item.cwd for item in scoped_memory)
            warnings.append(
                f"Prepared {len(memory_projects)} Qoder memory scope(s) as "
                "project-scoped source resources, without merging them into "
                "QwenPaw MEMORY.md.",
            )
            if missing_cwd:
                warnings.append(
                    f"{missing_cwd} Qoder Memory scope(s) refer to projects "
                    "that are no longer available at their original path. "
                    "Their source keys and content were preserved without a "
                    "working-directory binding.",
                )
        if plugins:
            compatible = sum(
                bool(item.install_source or item.metadata.get("install_path"))
                for item in plugins
            )
            adapted_custom = sum(
                item.metadata.get("adapter") == "qoder_skill_only_v1"
                for item in plugins
            )
            plugin_owned_skills = sum(
                int(item.metadata.get("plugin_owned_skill_count") or 0)
                for item in plugins
                if item.metadata.get("adapter") != "qoder_skill_only_v1"
            )
            warnings.append(
                f"Found {len(plugins)} enabled Qoder plugin(s) across "
                f"{len(marketplaces)} Marketplace source(s); {compatible} "
                "provide a source that can enter compatibility review. "
                "Local plugin sources are copied into the isolated staging "
                "area and are never modified in place.",
            )
            if plugin_owned_skills:
                warnings.append(
                    f"Found {plugin_owned_skills} Skill declaration(s) owned "
                    "by enabled Qoder plugins. They were not detached from "
                    "their plugins because their scripts/references and "
                    "Qoder-specific runtime contracts would be incomplete.",
                )
            if adapted_custom:
                warnings.append(
                    f"Prepared {adapted_custom} enabled Qoder "
                    "Skill-only plugin(s) for a constrained QwenPaw native "
                    "wrapper. Generated-wrapper Skills remain disabled "
                    "until explicitly reviewed and enabled.",
                )
        if not skills:
            warnings.append(
                "No standalone user/project Qoder Skills were found outside "
                "plugin-owned directories.",
            )
        if plugin_mcp_count:
            warnings.append(
                f"Prepared {plugin_mcp_count} MCP server(s) declared by "
                "enabled Qoder plugins for the normal QwenPaw MCP "
                "compatibility and DriverCard migration flow.",
            )
        elif discovered_mcp_count == 0:
            warnings.append(
                "Qoder's user-level mcp.json currently contains no MCP "
                "servers, and no enabled third-party plugin declares one.",
            )
        if scheduled_tasks:
            warnings.append(
                f"Prepared {len(scheduled_tasks)} Qoder scheduled task(s) "
                "as disabled QwenPaw Agent jobs. Running/queued state and "
                "execution history were not resumed or copied.",
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
                or bool(skills)
                or bool(mcp_servers)
                or bool(marketplaces)
                or bool(scheduled_tasks)
            ),
            locator=str(self._qoder_home),
            source_location=self._source_location,
            sessions=sessions,
            ignored_session_ids=ignored_session_ids,
            skills=skills,
            mcp_servers=mcp_servers,
            memory_projects=memory_projects,
            marketplaces=marketplaces,
            plugins=plugins,
            scheduled_tasks=scheduled_tasks,
            discovered_mcp_count=discovered_mcp_count,
            discovered_scheduled_task_count=(discovered_scheduled_task_count),
            warnings=warnings,
        )


__all__ = ["QoderMigrationProvider"]
