# -*- coding: utf-8 -*-
"""Privacy-first PawTrace export for QwenPaw session datasets."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..agents.context.scroll.continuation_summary import redact_secrets
from ..app.chats.utils import parse_legacy_memory_state
from ..constant import WORKING_DIR
from ..utils.io_utils import run_sync_io
from .models import TraceExportResult

_TRACE_SCHEMA_VERSION = "1"
_MAX_EVENT_STRING = 1_000_000
_SENSITIVE_KEY_RE = re.compile(
    r"(?i)(?:api[_-]?key|access[_-]?token|auth(?:orization)?|cookie|"
    r"password|passwd|private[_-]?key|client[_-]?secret|credential)",
)
_EMAIL_RE = re.compile(
    r"(?<![\w.+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}(?![\w.-])",
    re.IGNORECASE,
)
_DATA_URL_RE = re.compile(r"^data:[^;,]+(?:;base64)?,", re.IGNORECASE)


@dataclass
class _RedactionState:
    count: int = 0
    skipped: int = 0
    pseudonymized: int = 0


def _pseudonym(
    namespace: str,
    value: Any,
    *,
    salt: str,
    state: _RedactionState,
) -> str | None:
    if value in (None, ""):
        return None
    state.pseudonymized += 1
    digest = hashlib.sha256(
        f"{salt}\x00{namespace}\x00{value}".encode("utf-8", errors="replace"),
    ).hexdigest()[:24]
    return f"{namespace}:{digest}"


def _redact_text(
    text: str,
    *,
    home: str,
    workspace: str,
    state: _RedactionState,
) -> str:
    original = text
    if _DATA_URL_RE.match(text) and len(text) > 256:
        state.count += 1
        return "[binary content redacted]"
    if workspace:
        text = text.replace(workspace, "$WORKSPACE")
    if home:
        text = text.replace(home, "$HOME")
    text = _EMAIL_RE.sub("[email redacted]", text)
    text = redact_secrets(text)
    if len(text) > _MAX_EVENT_STRING:
        text = text[:_MAX_EVENT_STRING] + "\n[content truncated]"
        state.skipped += 1
    if text != original:
        state.count += 1
    return text


def _redact_value(  # pylint: disable=too-many-return-statements
    value: Any,
    *,
    home: str,
    workspace: str,
    state: _RedactionState,
    key: str = "",
) -> Any:
    if key and _SENSITIVE_KEY_RE.search(key):
        if value not in (None, "", [], {}):
            state.count += 1
        return "[secret redacted]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _redact_text(
            value,
            home=home,
            workspace=workspace,
            state=state,
        )
    if isinstance(value, list):
        return [
            _redact_value(
                item,
                home=home,
                workspace=workspace,
                state=state,
            )
            for item in value
        ]
    if isinstance(value, dict):
        return {
            str(k): _redact_value(
                item,
                home=home,
                workspace=workspace,
                state=state,
                key=str(k),
            )
            for k, item in value.items()
        }
    if hasattr(value, "model_dump"):
        return _redact_value(
            value.model_dump(mode="json"),
            home=home,
            workspace=workspace,
            state=state,
        )
    state.skipped += 1
    return f"[unsupported {type(value).__name__} omitted]"


def _raw_messages(session_state: dict[str, Any]) -> list[dict[str, Any]]:
    agent = session_state.get("agent") or {}
    state = agent.get("state") or {}
    context = state.get("context")
    if isinstance(context, list):
        return [item for item in context if isinstance(item, dict)]
    memory = agent.get("memory")
    if isinstance(memory, dict):
        messages, _summary = parse_legacy_memory_state(memory)
        return [item.model_dump(mode="json") for item in messages]
    return []


def _history_db_path(workspace: Any) -> Path | None:
    """Resolve the configured Scroll store without escaping the workspace."""
    config = None
    try:
        config = workspace.config
    except (AttributeError, OSError, RuntimeError, ValueError):
        config = getattr(workspace, "_config", None)

    filename = "history.db"
    if config is not None:
        try:
            light_context = config.running.light_context_config
            if light_context.strategy != "scroll":
                return None
            filename = light_context.scroll_config.db_filename
        except AttributeError:
            pass

    root = Path(workspace.workspace_dir).resolve()
    candidate = (root / filename).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def _decode_json(value: Any, fallback: Any) -> Any:
    if not isinstance(value, str) or not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def _history_messages(
    db_path: Path,
    *,
    session_id: str,
    agent_id: str,
) -> list[dict[str, Any]]:
    """Read one session from Scroll's source of truth in read-only mode."""
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True, timeout=5.0) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT seq, kind, role, name, content, tool_call_id, "
                "tool_input, tool_state, blocks, metadata, created_at, "
                "dedup_key FROM conversation_history "
                "WHERE session_id = ? AND (agent_id = ? OR agent_id IS NULL) "
                "ORDER BY seq",
                (session_id, agent_id),
            ).fetchall()
    except sqlite3.Error:
        return []

    messages: list[dict[str, Any]] = []
    for row in rows:
        blocks = _decode_json(row["blocks"], [])
        if not isinstance(blocks, list) or not blocks:
            if row["kind"] == "tool_result":
                blocks = [
                    {
                        "type": "tool_result",
                        "id": row["tool_call_id"],
                        "name": row["name"],
                        "output": row["content"],
                        "state": row["tool_state"],
                    },
                ]
            else:
                blocks = [{"type": "text", "text": row["content"] or ""}]
        metadata = _decode_json(row["metadata"], {})
        if not isinstance(metadata, dict):
            metadata = {}
        metadata = {
            **metadata,
            "pawtrace_storage": "scroll_history",
            "pawtrace_history_seq": int(row["seq"]),
        }
        if row["tool_input"]:
            metadata["pawtrace_tool_input"] = _decode_json(
                row["tool_input"],
                row["tool_input"],
            )
        messages.append(
            {
                "id": str(row["dedup_key"] or f"history:{row['seq']}"),
                "role": str(row["role"] or "assistant"),
                "content": blocks,
                "metadata": metadata,
                "created_at": row["created_at"],
            },
        )
    return messages


def _merge_messages(
    durable: list[dict[str, Any]],
    live: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep durable history and append only live messages not yet persisted."""
    durable_ids = {
        str(item.get("id"))
        for item in durable
        if item.get("id") not in (None, "")
    }
    merged = list(durable)
    for item in live:
        source_id = item.get("id")
        if source_id not in (None, "") and str(source_id) in durable_ids:
            continue
        copied = dict(item)
        metadata = copied.get("metadata")
        copied["metadata"] = {
            **(metadata if isinstance(metadata, dict) else {}),
            "pawtrace_storage": "live_session_tail",
        }
        merged.append(copied)
    return merged


def _event_kind(role: str, block: dict[str, Any]) -> str:
    block_type = str(block.get("type") or "text")
    if block_type == "thinking":
        return "reasoning_summary"
    if block_type in {"tool_call", "tool_use"}:
        return "tool_call"
    if block_type == "tool_result":
        return "tool_result"
    if role == "user":
        return "user_message"
    if role == "system":
        return "system_message"
    return "assistant_message"


def _events_for_message(
    raw: dict[str, Any],
    *,
    trace_id: str,
    session_id: str,
    sequence_start: int,
    home: str,
    workspace: str,
    state: _RedactionState,
) -> list[dict[str, Any]]:
    role = str(raw.get("role") or "assistant")
    content = raw.get("content")
    if isinstance(content, str):
        blocks = [{"type": "text", "text": content}]
    elif isinstance(content, list):
        blocks = [item for item in content if isinstance(item, dict)]
    else:
        state.skipped += 1
        return []
    metadata = raw.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    safe_metadata = _redact_value(
        metadata,
        home=home,
        workspace=workspace,
        state=state,
    )
    source_harness = _redact_text(
        str(metadata.get("third_party_backend") or "qwenpaw"),
        home=home,
        workspace=workspace,
        state=state,
    )
    timestamp = str(raw.get("created_at") or raw.get("timestamp") or "")
    message_id = str(raw.get("id") or "")
    events: list[dict[str, Any]] = []
    for offset, block in enumerate(blocks):
        sequence = sequence_start + offset
        kind = _event_kind(role, block)
        safe_block = _redact_value(
            block,
            home=home,
            workspace=workspace,
            state=state,
        )
        event: dict[str, Any] = {
            "schema_version": _TRACE_SCHEMA_VERSION,
            "trace_id": trace_id,
            "session_id": session_id,
            "event_id": f"{trace_id}:{sequence}",
            "parent_event_id": (
                f"{trace_id}:{sequence - 1}" if sequence > 1 else None
            ),
            "sequence": sequence,
            "timestamp": timestamp or None,
            "source_harness": source_harness,
            "source_event_id": _pseudonym(
                "event",
                message_id,
                salt=trace_id,
                state=state,
            ),
            "kind": kind,
            "role": role,
            "content_blocks": [safe_block],
            "metadata": safe_metadata,
            "provenance": {
                "source": "qwenpaw_session",
                "normalized": True,
                "reasoning": (
                    "visible_persisted_summary"
                    if kind == "reasoning_summary"
                    else None
                ),
            },
            "redaction_markers": [],
        }
        if kind == "tool_call":
            tool_call_id = _pseudonym(
                "tool",
                block.get("id"),
                salt=trace_id,
                state=state,
            )
            tool_name = _redact_value(
                block.get("name"),
                home=home,
                workspace=workspace,
                state=state,
            )
            safe_block["id"] = tool_call_id
            safe_block["name"] = tool_name
            event["tool_call_id"] = tool_call_id
            event["tool_name"] = tool_name
            event["tool_arguments"] = safe_block.get("input")
        elif kind == "tool_result":
            tool_call_id = _pseudonym(
                "tool",
                block.get("id"),
                salt=trace_id,
                state=state,
            )
            tool_name = _redact_value(
                block.get("name"),
                home=home,
                workspace=workspace,
                state=state,
            )
            safe_block["id"] = tool_call_id
            safe_block["name"] = tool_name
            event["tool_call_id"] = tool_call_id
            event["tool_name"] = tool_name
            event["tool_result"] = safe_block.get("output")
        events.append(event)
    return events


async def _collect_trace_payload(
    workspace: Any,
    trace_path: Path,
) -> tuple[list[dict[str, Any]], int, _RedactionState]:
    chats = await workspace.chat_manager.list_chats(archived=None)
    chats.sort(key=lambda item: (item.created_at, item.id))
    home = str(Path.home().resolve())
    workspace_path = str(workspace.workspace_dir.resolve())
    redaction = _RedactionState()
    catalog: list[dict[str, Any]] = []
    event_count = 0
    history_path = _history_db_path(workspace)
    for chat in chats:
        state = await workspace.session.get_session_state_dict(
            chat.session_id,
            chat.user_id,
            chat.channel,
        )
        live_messages = _raw_messages(state)
        durable_messages: list[dict[str, Any]] = []
        if history_path is not None:
            durable_messages = await run_sync_io(
                _history_messages,
                history_path,
                session_id=chat.session_id,
                agent_id=workspace.agent_id,
            )
        raw_messages = _merge_messages(durable_messages, live_messages)
        trace_id = uuid4().hex
        public_session_id = f"session:{trace_id}"
        catalog.append(
            {
                "trace_id": trace_id,
                "session_id": public_session_id,
                "chat_id": f"chat:{trace_id}",
                "title": _redact_text(
                    chat.name,
                    home=home,
                    workspace=workspace_path,
                    state=redaction,
                ),
                "channel": chat.channel,
                "created_at": chat.created_at.isoformat(),
                "updated_at": chat.updated_at.isoformat(),
                "archived": chat.archived,
                "source": _redact_text(
                    str(
                        (chat.meta.get("portability") or {}).get("source")
                        or "qwenpaw",
                    ),
                    home=home,
                    workspace=workspace_path,
                    state=redaction,
                ),
                "storage_sources": {
                    "scroll_history_rows": len(durable_messages),
                    "live_session_messages": len(live_messages),
                    "merged_messages": len(raw_messages),
                },
            },
        )
        sequence = 1
        session_events: list[dict[str, Any]] = []
        for raw in raw_messages:
            converted = _events_for_message(
                raw,
                trace_id=trace_id,
                session_id=public_session_id,
                sequence_start=sequence,
                home=home,
                workspace=workspace_path,
                state=redaction,
            )
            session_events.extend(converted)
            sequence += len(converted)
        if session_events:
            await run_sync_io(
                _append_trace_events,
                trace_path,
                session_events,
            )
            event_count += len(session_events)
    return catalog, event_count, redaction


def _append_trace_events(
    trace_path: Path,
    events: list[dict[str, Any]],
) -> None:
    """Append one normalized session batch to private JSONL staging."""
    with trace_path.open("a", encoding="utf-8", newline="\n") as handle:
        for event in events:
            line = json.dumps(
                event,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            handle.write(line + "\n")
    trace_path.chmod(0o600)


def _write_trace_archive(
    *,
    destination: Path,
    agent_id: str,
    catalog: list[dict[str, Any]],
    trace_path: Path,
    event_count: int,
    redaction: _RedactionState,
) -> TraceExportResult:
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    with trace_path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    sha256 = digest.hexdigest()
    created_at = datetime.now(timezone.utc).isoformat()
    meta = {
        "id": destination.stem,
        "name": f"PawTrace {agent_id}",
        "type": "qwenpaw-trace",
        "schema_version": _TRACE_SCHEMA_VERSION,
        "created_at": created_at,
        "agent_id": agent_id,
        "session_count": len(catalog),
        "event_count": event_count,
        "redaction": {
            "mode": "safe",
            "mandatory": True,
            "redaction_count": redaction.count,
            "pseudonymized_identifier_count": redaction.pseudonymized,
            "skipped_or_truncated_count": redaction.skipped,
            "hidden_chain_of_thought_included": False,
        },
        "data": {
            "trace_path": "data/traces.jsonl",
            "trace_sha256": sha256,
            "catalog_path": "data/sessions.json",
        },
    }
    tmp = destination.with_suffix(destination.suffix + ".tmp")
    tmp.unlink(missing_ok=True)
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "meta.json",
                json.dumps(meta, ensure_ascii=False, indent=2),
            )
            archive.writestr(
                "data/sessions.json",
                json.dumps(catalog, ensure_ascii=False, indent=2),
            )
            archive.write(trace_path, "data/traces.jsonl")
            archive.writestr(
                "reports/redaction.json",
                json.dumps(meta["redaction"], ensure_ascii=False, indent=2),
            )
        tmp.chmod(0o600)
        os.replace(tmp, destination)
    finally:
        tmp.unlink(missing_ok=True)
    return TraceExportResult(
        path=destination,
        session_count=len(catalog),
        event_count=event_count,
        redaction_count=redaction.count,
        skipped_count=redaction.skipped,
        sha256=sha256,
    )


async def export_trace(workspace: Any) -> TraceExportResult:
    """Export every registered session for the current Agent."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    export_ref = uuid4().hex
    filename = f"pawtrace-agent-{stamp}-{export_ref[:8]}.zip"
    destination = WORKING_DIR / "exports" / filename
    with tempfile.TemporaryDirectory(prefix="qwenpaw_trace_") as temp_name:
        trace_path = Path(temp_name) / "traces.jsonl"
        trace_path.touch(mode=0o600)
        catalog, event_count, redaction = await _collect_trace_payload(
            workspace,
            trace_path,
        )
        return await run_sync_io(
            _write_trace_archive,
            destination=destination,
            agent_id=f"agent:{export_ref}",
            catalog=catalog,
            trace_path=trace_path,
            event_count=event_count,
            redaction=redaction,
        )


__all__ = ["export_trace"]
