# -*- coding: utf-8 -*-
"""Codex Migration Provider with app-server and rollout JSONL recovery."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from ...harnesses.codex.rollout_reader import CodexRolloutReader
from ..models import (
    ProviderInventory,
    SourceMCPServer,
    SourceSession,
    SourceSkill,
)
from ._utils import parse_datetime
from .base import ProgressReporter
from .external_state import discover_codex_memory, discover_codex_plugins

_DISCOVERY_TIMEOUT_SECONDS = 60
_SESSION_TIMEOUT_SECONDS = 90
_READ_CONCURRENCY = 2


def _milestone(index: int, total: int) -> bool:
    if total <= 20:
        return True
    step = max(1, total // 20)
    return index == 1 or index == total or index % step == 0


def _error_detail(exc: BaseException) -> str:
    return str(exc).strip() or type(exc).__name__


class CodexMigrationProvider:  # pylint: disable=too-few-public-methods
    """Read Codex sessions, Skills and MCP without changing source data."""

    provider_id = "codex"

    def __init__(
        self,
        workspace: Any,
        rollout_reader: CodexRolloutReader | None = None,
    ) -> None:
        self._workspace = workspace
        self._rollout_reader = rollout_reader

    # pylint: disable-next=too-many-locals,too-many-branches
    async def inventory(
        self,
        *,
        limit: int,
        progress: ProgressReporter | None = None,
    ) -> ProviderInventory:
        """Discover and normalize Codex-owned portable objects."""
        settings = (
            dict(self._workspace.config.backend_settings)
            if self._workspace.config.backend == self.provider_id
            else {}
        )
        adapter = await self._workspace.harness_runtime.adapter(
            self.provider_id,
            settings,
        )
        rollout_reader = self._rollout_reader or CodexRolloutReader()
        offline_threads = await asyncio.to_thread(
            rollout_reader.list_threads,
            limit=limit,
        )

        try:
            status = await asyncio.wait_for(
                adapter.status(),
                timeout=_DISCOVERY_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            status = None
        installed = bool(status is not None and status.installed)
        if not installed and not offline_threads:
            detail = (
                getattr(status, "error", "")
                if status is not None
                else "Codex runtime detection timed out after 60s."
            )
            return ProviderInventory(
                provider_id=self.provider_id,
                provider_name="Codex",
                detected=False,
                warnings=[
                    detail
                    or "Codex runtime and local rollouts were not found.",
                ],
            )

        warnings: list[str] = []
        if not installed:
            warnings.append(
                "Codex CLI was unavailable, so local rollout JSONL files "
                "were used for session recovery.",
            )

        if progress is not None:
            await progress(
                "已连接 Codex，正在检查 Memory、插件、Skill 与 MCP 配置…",
            )

        memory_projects, plugin_state = await asyncio.gather(
            asyncio.to_thread(
                discover_codex_memory,
                rollout_reader.codex_home,
            ),
            asyncio.to_thread(
                discover_codex_plugins,
                rollout_reader.codex_home,
            ),
        )
        marketplaces, plugins = plugin_state

        skills = await self._discover_skills(
            adapter,
            rollout_reader,
            installed=installed,
            warnings=warnings,
        )
        mcp_servers = await self._discover_mcp(
            adapter,
            installed=installed,
            warnings=warnings,
        )

        if progress is not None:
            await progress("正在合并 Codex 会话索引与本地 JSONL 备份…")
        online_threads: list[dict[str, Any]] = []
        if installed:
            try:
                online_threads = await asyncio.wait_for(
                    adapter.list_external_threads(limit=limit),
                    timeout=_DISCOVERY_TIMEOUT_SECONDS,
                )
            except Exception as exc:  # pylint: disable=broad-except
                warnings.append(
                    "Codex app-server session listing failed; local rollout "
                    f"recovery was used: {_error_detail(exc)}",
                )
        raw_threads = _merge_threads(online_threads, offline_threads, limit)
        if len(raw_threads) >= limit:
            warnings.append(
                f"Codex session safety limit ({limit}) was reached; older "
                "threads beyond this bounded import were not read.",
            )
        if progress is not None:
            await progress(
                f"发现 {len(raw_threads)} 个 Codex 会话，正在读取历史"
                f"（最多 {_READ_CONCURRENCY} 个同时读取，失败会自动改读 JSONL）…",
            )

        sessions, read_warnings, recovered = await self._read_sessions(
            adapter,
            rollout_reader,
            raw_threads,
            installed=installed,
            progress=progress,
        )
        warnings.extend(read_warnings)
        if recovered:
            warnings.append(
                f"Recovered {recovered} Codex session(s) from local rollout "
                "JSONL after app-server history was unavailable/incomplete.",
            )
        if skills:
            warnings.append(
                f"Copied {len(skills)} Codex Skill candidate(s) in the "
                "disabled state. Codex-only commands, tools and plugin "
                "dependencies still require review before enabling.",
            )
        if mcp_servers:
            warnings.append(
                f"Prepared {len(mcp_servers)} Codex MCP server(s) for "
                "disabled QwenPaw DriverCards. Literal secrets are moved to "
                "the encrypted credential store; OAuth must be authorized "
                "again.",
            )
        if memory_projects:
            warnings.append(
                f"Prepared {len(memory_projects)} Codex memory scope(s) as "
                "project-scoped source resources. They are not flattened "
                "into QwenPaw MEMORY.md.",
            )
        if plugins:
            compatible = sum(bool(item.install_source) for item in plugins)
            warnings.append(
                f"Found {len(plugins)} enabled Codex plugin(s) across "
                f"{len(marketplaces)} Marketplace source(s); {compatible} "
                "expose a QwenPaw-compatible native install source. "
                "Installed Codex cache directories are never copied.",
            )
        warnings.append(
            "Codex built-in tools, sandbox/approval settings, hooks, agents, "
            "and provider runtime state are harness-bound and were not "
            "copied. Historical tool calls remain session data.",
        )

        locator = (
            str(getattr(status, "runtime_path", "") or "")
            if status is not None
            else ""
        )
        return ProviderInventory(
            provider_id=self.provider_id,
            provider_name="Codex",
            detected=True,
            locator=locator or str(rollout_reader.codex_home),
            sessions=sessions,
            skills=skills,
            mcp_servers=mcp_servers,
            memory_projects=memory_projects,
            marketplaces=marketplaces,
            plugins=plugins,
            discovered_mcp_count=len(mcp_servers),
            warnings=warnings,
        )

    async def _discover_skills(
        self,
        adapter: Any,
        rollout_reader: CodexRolloutReader,
        *,
        installed: bool,
        warnings: list[str],
    ) -> list[SourceSkill]:
        records: list[dict[str, Any]] = []
        if installed:
            try:
                records = await asyncio.wait_for(
                    adapter.external_skill_records(
                        self._workspace.workspace_dir.resolve(),
                    ),
                    timeout=_DISCOVERY_TIMEOUT_SECONDS,
                )
            except Exception as exc:  # pylint: disable=broad-except
                warnings.append(
                    "Codex app-server Skill discovery failed; local Skill "
                    f"folders were scanned instead: {_error_detail(exc)}",
                )
        if not records:
            records = await asyncio.to_thread(rollout_reader.skill_records)

        workspace_skills = self._workspace.workspace_dir.resolve() / "skills"
        skills: list[SourceSkill] = []
        seen: set[Path] = set()
        for item in records:
            raw_path = Path(str(item.get("path") or "")).expanduser()
            if (
                raw_path.is_symlink()
                or raw_path.parent.is_symlink()
                or not raw_path.is_file()
                or raw_path.name != "SKILL.md"
            ):
                continue
            directory = raw_path.parent.resolve()
            if directory in seen or directory.is_relative_to(workspace_skills):
                continue
            seen.add(directory)
            skills.append(
                SourceSkill(
                    source_id=str(directory),
                    name=str(item.get("name") or directory.name),
                    directory=directory,
                    description=str(item.get("description") or ""),
                    scope=str(item.get("scope") or "provider"),
                ),
            )
        return skills

    async def _discover_mcp(
        self,
        adapter: Any,
        *,
        installed: bool,
        warnings: list[str],
    ) -> list[SourceMCPServer]:
        if not installed:
            return []
        external_records = getattr(adapter, "external_mcp_records", None)
        if external_records is None:
            try:
                discovered = await adapter.discover_mcp(
                    self._workspace.workspace_dir.resolve(),
                )
                if discovered:
                    warnings.append(
                        f"Found {len(discovered)} Codex MCP server(s), but "
                        "this Codex adapter exposes redacted discovery only.",
                    )
                return []
            except Exception as exc:  # pylint: disable=broad-except
                warnings.append(
                    f"Codex MCP discovery failed: {_error_detail(exc)}",
                )
                return []
        try:
            records = await asyncio.wait_for(
                external_records(self._workspace.workspace_dir.resolve()),
                timeout=_DISCOVERY_TIMEOUT_SECONDS,
            )
        except Exception as exc:  # pylint: disable=broad-except
            warnings.append(
                f"Codex MCP discovery failed: {_error_detail(exc)}",
            )
            return []
        servers: list[SourceMCPServer] = []
        for item in records:
            server = _mcp_server(item)
            if server is not None:
                servers.append(server)
        return servers

    # pylint: disable-next=too-many-locals
    async def _read_sessions(
        self,
        adapter: Any,
        rollout_reader: CodexRolloutReader,
        raw_threads: list[dict[str, Any]],
        *,
        installed: bool,
        progress: ProgressReporter | None,
    ) -> tuple[list[SourceSession], list[str], int]:
        semaphore = asyncio.Semaphore(_READ_CONCURRENCY)
        progress_lock = asyncio.Lock()
        completed = 0
        recovered = 0

        async def _read_one(
            raw: dict[str, Any],
        ) -> tuple[SourceSession | None, str | None, bool]:
            nonlocal completed
            source_id = str(raw.get("id") or raw.get("threadId") or "")
            used_fallback = False
            failure: BaseException | None = None
            history = []
            if source_id and installed and not raw.get("offlineOnly"):
                try:
                    async with semaphore:
                        history = await asyncio.wait_for(
                            adapter.read_external_thread(source_id),
                            timeout=_SESSION_TIMEOUT_SECONDS,
                        )
                except Exception as exc:  # pylint: disable=broad-except
                    failure = exc
            if source_id and (failure is not None or not history):
                try:
                    history = await asyncio.to_thread(
                        rollout_reader.read_thread,
                        source_id,
                    )
                    used_fallback = bool(history)
                except Exception as exc:  # pylint: disable=broad-except
                    if failure is None:
                        failure = exc

            if not source_id:
                result: tuple[SourceSession | None, str | None, bool] = (
                    None,
                    "Skipped one Codex thread without an id.",
                    False,
                )
            elif failure is not None and not history:
                result = (
                    None,
                    f"Could not read Codex thread {source_id}: "
                    f"{_error_detail(failure)}",
                    False,
                )
            else:
                title = str(
                    raw.get("name")
                    or raw.get("title")
                    or raw.get("preview")
                    or f"Codex {source_id[:8]}",
                )
                result = (
                    SourceSession(
                        source_id=source_id,
                        title=title[:200],
                        cwd=str(raw.get("cwd") or ""),
                        created_at=parse_datetime(
                            raw.get("createdAt") or raw.get("created_at"),
                        ),
                        updated_at=parse_datetime(
                            raw.get("updatedAt") or raw.get("updated_at"),
                        ),
                        history=history,
                        metadata={
                            "status": str(raw.get("status") or ""),
                            "source": str(raw.get("source") or "codex"),
                            "offline_recovery": used_fallback,
                        },
                    ),
                    None,
                    used_fallback,
                )
            async with progress_lock:
                completed += 1
                if progress is not None and _milestone(
                    completed,
                    len(raw_threads),
                ):
                    await progress(
                        f"Codex 会话读取进度：{completed}/{len(raw_threads)}",
                    )
            return result

        results = await asyncio.gather(
            *(_read_one(raw) for raw in raw_threads),
        )
        sessions: list[SourceSession] = []
        warnings: list[str] = []
        for session, warning, used_fallback in results:
            if session is not None:
                sessions.append(session)
            if warning:
                warnings.append(warning)
            if used_fallback:
                recovered += 1
        return sessions, warnings, recovered


def _merge_threads(
    online: list[dict[str, Any]],
    offline: list[dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    offline_by_id = {
        str(item.get("id") or ""): item for item in offline if item.get("id")
    }
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in online:
        thread_id = str(item.get("id") or item.get("threadId") or "")
        if not thread_id or thread_id in seen:
            continue
        fallback = offline_by_id.get(thread_id, {})
        combined = {**fallback, **item}
        if not combined.get("cwd"):
            combined["cwd"] = fallback.get("cwd", "")
        merged.append(combined)
        seen.add(thread_id)
    for item in offline:
        thread_id = str(item.get("id") or "")
        if not thread_id or thread_id in seen:
            continue
        merged.append({**item, "offlineOnly": True})
        seen.add(thread_id)
    return merged[: max(0, limit)]


def _mcp_server(item: dict[str, Any]) -> SourceMCPServer | None:
    name = str(item.get("name") or "")
    transport = item.get("transport")
    if not name or not isinstance(transport, dict):
        return None
    transport_type = str(transport.get("type") or "stdio")
    env = {
        str(key): str(value)
        for key, value in dict(transport.get("env") or {}).items()
    }
    for var in transport.get("env_vars") or []:
        var_name = str(var)
        if var_name:
            env.setdefault(var_name, f"${{{var_name}}}")
    headers = {
        str(key): str(value)
        for key, value in dict(transport.get("http_headers") or {}).items()
    }
    for header, var in dict(transport.get("env_http_headers") or {}).items():
        var_name = str(var)
        if var_name:
            headers.setdefault(str(header), f"${{{var_name}}}")
    bearer_var = str(transport.get("bearer_token_env_var") or "")
    if bearer_var:
        headers.setdefault("Authorization", f"Bearer ${{{bearer_var}}}")
    command = str(transport.get("command") or "")
    args = [str(value) for value in transport.get("args") or []]
    dependency_text = "\n".join(
        [command, *args, str(transport.get("cwd") or "")],
    )
    source_runtime_bound = any(
        marker in dependency_text
        for marker in (
            "/.codex/",
            "\\.codex\\",
            "ChatGPT.app/Contents/Resources",
            "codex-app-server",
        )
    )
    return SourceMCPServer(
        source_id=name,
        name=name,
        transport=transport_type,
        enabled=bool(item.get("enabled")),
        command=command,
        args=args,
        env=env,
        cwd=str(transport.get("cwd") or ""),
        url=str(transport.get("url") or ""),
        headers=headers,
        auth_status=str(item.get("auth_status") or ""),
        metadata={
            "source_enabled": bool(item.get("enabled")),
            "source_runtime_bound": source_runtime_bound,
        },
    )


__all__ = ["CodexMigrationProvider"]
