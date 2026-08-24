# -*- coding: utf-8 -*-
"""Conversation phase of a provider import transaction."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..app.chats.models import ChatSpec
from ..harnesses.session import HarnessSessionBridge
from .import_support import (
    _chat_id,
    _progress_milestone,
    _project_directory,
    _session_key,
)
from .models import ProviderInventory, SourceSession
from .providers.base import ProgressReporter, report_progress as _report


@dataclass
class ConversationState:
    """Imported chat changes retained for receipt generation and rollback."""

    imported: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    archived_internal: list[str] = field(default_factory=list)
    created_chats: list[str] = field(default_factory=list)
    created_states: list[tuple[str, str, str]] = field(default_factory=list)
    patched_project_dirs: list[tuple[str, str | None]] = field(
        default_factory=list,
    )
    archived_chats: list[str] = field(default_factory=list)


async def import_conversations(
    workspace: Any,
    inventory: ProviderInventory,
    sessions: list[SourceSession],
    existing_by_source: dict[tuple[str, str], Any],
    warnings: list[str],
    started_at: datetime,
    progress: ProgressReporter | None,
    state: ConversationState,
) -> None:
    """Import readable root chats and archive old imported internals."""
    bridge = HarnessSessionBridge(workspace.session)
    if inventory.ignored_session_ids:
        await _report(progress, "正在整理此前误导入的内部执行轨迹…")
    for source_id in inventory.ignored_session_ids:
        chat = existing_by_source.get((inventory.provider_id, source_id))
        if chat is None or chat.archived:
            continue
        archived = await workspace.chat_manager.archive_chat(chat.id)
        if archived is not None:
            state.archived_chats.append(chat.id)
            state.archived_internal.append(source_id)
    if state.archived_internal:
        warnings.append(
            f"Archived {len(state.archived_internal)} previously imported "
            f"{inventory.provider_name} non-root/internal execution traces. "
            "They remain recoverable from the archived chat list.",
        )

    total = len(sessions)
    for index, session in enumerate(sessions, start=1):
        if _progress_milestone(index, total):
            await _report(
                progress,
                f"正在写入会话：{index}/{total}（聊天记录阶段）",
            )
        source_key = (inventory.provider_id, session.source_id)
        existing = existing_by_source.get(source_key)
        project_dir = _project_directory(session, warnings)
        if existing is not None:
            if project_dir:
                runtime = existing.meta.get("runtime_context") or {}
                current = str(runtime.get("project_dir") or "")
                if current != project_dir:
                    state.patched_project_dirs.append(
                        (existing.id, current or None),
                    )
                    await workspace.chat_manager.set_project_dir(
                        existing.id,
                        project_dir,
                    )
            state.skipped.append(session.source_id)
            continue
        if not session.history:
            state.skipped.append(session.source_id)
            warnings.append(
                f"Session {session.source_id} contained no readable "
                "conversation history.",
            )
            continue

        session_id = _session_key(inventory.provider_id, session.source_id)
        user_id, channel = session_id, "console"
        await bridge.hydrate(
            session_id=session_id,
            user_id=user_id,
            channel=channel,
            backend=inventory.provider_id,
            history=session.history,
        )
        state.created_states.append((session_id, user_id, channel))
        portability = {
            "schema_version": "1",
            "source": inventory.provider_id,
            "source_id": session.source_id,
            "source_locator": inventory.locator,
            "source_cwd": session.cwd,
            "imported_at": datetime.now(timezone.utc).isoformat(),
            "import_mode": "historical_archive",
            "read_only_enforced": False,
            "continuation_fidelity": "not_guaranteed",
            "historical_tools_are_data": True,
            "fidelity": "normalized_lossy",
        }
        meta: dict[str, Any] = {"portability": portability}
        if project_dir:
            meta["runtime_context"] = {"project_dir": project_dir}
        spec = ChatSpec(
            id=_chat_id(inventory.provider_id, session.source_id),
            name=session.title,
            session_id=session_id,
            user_id=user_id,
            channel=channel,
            created_at=session.created_at or started_at,
            updated_at=session.updated_at or session.created_at or started_at,
            meta=meta,
        )
        await workspace.chat_manager.create_chat(spec)
        existing_by_source[source_key] = spec
        state.created_chats.append(spec.id)
        state.imported.append(session.source_id)

    await _report(
        progress,
        "聊天记录阶段完成；开始迁移并检查工具和设置…",
    )
