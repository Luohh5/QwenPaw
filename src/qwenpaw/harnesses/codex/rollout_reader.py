# -*- coding: utf-8 -*-
"""Bounded, read-only fallback reader for Codex rollout JSONL files."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping

from ..events import HarnessHistoryItem, HarnessHistoryKind

_UUID_PATTERN = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.IGNORECASE,
)
_MAX_ROLLOUT_BYTES = 128 * 1024 * 1024
_MAX_LINE_BYTES = 128 * 1024 * 1024


def _source_label(value: Any) -> str:
    """Return a stable label for a serialized Codex source variant."""
    if isinstance(value, str):
        return re.sub(r"(?<!^)(?=[A-Z])", "_", value).lower()
    if not isinstance(value, Mapping):
        return "unknown"
    for key, child in value.items():
        label = _source_label(key)
        if label == "other" and isinstance(child, str):
            return _source_label(child)
        return label
    return "unknown"


def codex_non_root_session_kind(metadata: Mapping[str, Any]) -> str:
    """Classify Codex internal/child sessions from structural metadata.

    Codex's app-server omits these sessions from its default ``thread/list``
    response.  The JSONL fallback must apply the same boundary or Guardian,
    compaction, memory and delegated worker rollouts appear as user chats.
    Message text is deliberately ignored because a normal conversation may
    quote an internal prompt while debugging it.
    """
    source = metadata.get("source")
    if isinstance(source, Mapping):
        for raw_key, value in source.items():
            key = _source_label(raw_key)
            if key == "internal":
                return f"internal:{_source_label(value)}"
            if key in {"subagent", "sub_agent"}:
                return f"subagent:{_source_label(value)}"
    elif isinstance(source, str):
        label = _source_label(source)
        if label.startswith(("internal", "subagent", "sub_agent")):
            return label

    thread_source = metadata.get("thread_source")
    if thread_source is None:
        thread_source = metadata.get("threadSource")
    label = _source_label(thread_source)
    if label in {
        "subagent",
        "sub_agent",
        "memory_consolidation",
        "automation",
    }:
        return label
    return ""


@dataclass(frozen=True)
class CodexRolloutRecord:
    """Small index entry for one local Codex rollout."""

    thread_id: str
    path: Path
    cwd: str = ""
    title: str = ""
    created_at: str = ""
    updated_at: str = ""
    non_root_kind: str = ""
    parent_thread_id: str = ""
    lineage_paths: tuple[Path, ...] = ()

    def as_thread(self) -> dict[str, Any]:
        """Return app-server-compatible thread metadata."""
        return {
            "id": self.thread_id,
            "preview": self.title or f"Codex {self.thread_id[:8]}",
            "cwd": self.cwd,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "rolloutPath": str(self.path),
            "rolloutLineageLength": len(self.paths),
            "source": "codex-rollout-jsonl",
            "parentThreadId": self.parent_thread_id or None,
        }

    @property
    def paths(self) -> tuple[Path, ...]:
        """Return every rollout segment in chronological order."""
        return self.lineage_paths or (self.path,)


@dataclass
class _RolloutMetadataState:
    """Mutable metadata accumulated while scanning one rollout."""

    thread_id: str
    cwd: str = ""
    title: str = ""
    created_at: str = ""
    updated_at: str = ""
    non_root_kind: str = ""
    parent_thread_id: str = ""

    def update(self, entry: dict[str, Any]) -> None:
        """Merge metadata carried by one rollout entry."""
        timestamp = str(entry.get("timestamp") or "")
        if timestamp:
            self.created_at = self.created_at or timestamp
            self.updated_at = timestamp
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            return
        entry_type = str(entry.get("type") or "")
        if entry_type == "session_meta":
            self.thread_id = str(
                payload.get("id")
                or payload.get("session_id")
                or self.thread_id,
            )
            self.cwd = str(payload.get("cwd") or self.cwd)
            self.created_at = str(
                payload.get("timestamp") or self.created_at,
            )
            self.parent_thread_id = str(
                payload.get("parent_thread_id")
                or payload.get("parentThreadId")
                or self.parent_thread_id,
            )
            self.non_root_kind = (
                codex_non_root_session_kind(payload) or self.non_root_kind
            )
        elif entry_type == "turn_context":
            self.cwd = str(payload.get("cwd") or self.cwd)
        elif (
            not self.title
            and entry_type == "event_msg"
            and payload.get("type") == "user_message"
        ):
            self.title = _visible_text(payload)[:200]


class CodexRolloutReader:
    """Index and normalize local Codex JSONL without changing source data."""

    def __init__(self, codex_home: Path | None = None) -> None:
        configured = os.environ.get("CODEX_HOME", "").strip()
        self.codex_home = (
            codex_home
            or (Path(configured).expanduser() if configured else None)
            or (Path.home() / ".codex")
        )
        self._records: dict[str, CodexRolloutRecord] | None = None

    def list_threads(self, *, limit: int = 500) -> list[dict[str, Any]]:
        """Return newest user-facing root rollout metadata records."""
        records = sorted(
            (
                item
                for item in self._index().values()
                if not item.non_root_kind
            ),
            key=lambda item: (item.updated_at, str(item.path)),
            reverse=True,
        )
        return [item.as_thread() for item in records[: max(0, limit)]]

    def list_non_root_thread_ids(self, *, limit: int = 5000) -> list[str]:
        """Return bounded child/internal IDs for prior-import cleanup."""
        records = sorted(
            (item for item in self._index().values() if item.non_root_kind),
            key=lambda item: (item.updated_at, str(item.path)),
            reverse=True,
        )
        return [item.thread_id for item in records[: max(0, limit)]]

    def has_thread(self, thread_id: str) -> bool:
        """Return whether a local rollout exists for ``thread_id``."""
        return thread_id in self._index()

    def read_thread(self, thread_id: str) -> list[HarnessHistoryItem]:
        """Normalize visible chat and tool events from one rollout."""
        record = self._index().get(thread_id)
        if record is None:
            raise FileNotFoundError(f"Codex rollout not found: {thread_id}")
        history: list[HarnessHistoryItem] = []
        seen: set[tuple[str, str]] = set()
        for path in record.paths:
            for item in _read_rollout_history(path):
                stable_id = item.item_id
                key = (item.kind.value, stable_id)
                if stable_id:
                    if key in seen:
                        continue
                    seen.add(key)
                history.append(item)
        return history

    def skill_records(self, *, limit: int = 500) -> list[dict[str, Any]]:
        """Find local Codex/Agents/Plugin Skills
        when app-server is unavailable."""
        roots = [
            self.codex_home / "skills",
            self.codex_home / "plugins" / "cache",
            Path.home() / ".agents" / "skills",
        ]
        records: list[dict[str, Any]] = []
        seen: set[Path] = set()
        for root in roots:
            if not root.is_dir():
                continue
            try:
                candidates = root.rglob("SKILL.md")
                for path in candidates:
                    if len(records) >= limit:
                        return records
                    if path.is_symlink() or not path.is_file():
                        continue
                    resolved = path.resolve()
                    if resolved in seen:
                        continue
                    seen.add(resolved)
                    records.append(
                        {
                            "name": resolved.parent.name,
                            "description": "",
                            "path": str(resolved),
                            "scope": "local-fallback",
                        },
                    )
            except OSError:
                continue
        return records

    def _index(self) -> dict[str, CodexRolloutRecord]:
        if self._records is None:
            self._records = {}
            for root in (
                self.codex_home / "sessions",
                self.codex_home / "archived_sessions",
            ):
                if not root.is_dir():
                    continue
                for path in root.rglob("*.jsonl"):
                    record = _read_rollout_metadata(path)
                    if record is None:
                        continue
                    existing = self._records.get(record.thread_id)
                    if existing is None:
                        self._records[record.thread_id] = record
                        continue
                    lineage = tuple(
                        sorted(
                            {*existing.paths, *record.paths},
                            key=str,
                        ),
                    )
                    latest = (
                        record
                        if record.updated_at >= existing.updated_at
                        else existing
                    )
                    created_values = [
                        value
                        for value in (existing.created_at, record.created_at)
                        if value
                    ]
                    earliest_created = (
                        min(created_values) if created_values else ""
                    )
                    self._records[record.thread_id] = replace(
                        latest,
                        created_at=earliest_created,
                        non_root_kind=(
                            existing.non_root_kind or record.non_root_kind
                        ),
                        parent_thread_id=(
                            latest.parent_thread_id
                            or existing.parent_thread_id
                            or record.parent_thread_id
                        ),
                        lineage_paths=lineage,
                    )
        return self._records


def _read_rollout_metadata(path: Path) -> CodexRolloutRecord | None:
    try:
        if not path.is_file() or path.stat().st_size > _MAX_ROLLOUT_BYTES:
            return None
    except OSError:
        return None
    match = _UUID_PATTERN.search(path.stem)
    state = _RolloutMetadataState(match.group(1) if match else "")
    try:
        with path.open("rb") as stream:
            for raw_line in stream:
                if len(raw_line) > _MAX_LINE_BYTES:
                    return None
                try:
                    entry = json.loads(raw_line)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if not isinstance(entry, dict):
                    continue
                state.update(entry)
    except OSError:
        return None
    if not state.thread_id:
        return None
    return CodexRolloutRecord(
        thread_id=state.thread_id,
        path=path.resolve(),
        cwd=state.cwd,
        title=" ".join(state.title.split()),
        created_at=state.created_at,
        updated_at=state.updated_at or state.created_at,
        non_root_kind=state.non_root_kind,
        parent_thread_id=state.parent_thread_id,
    )


def _read_rollout_history(path: Path) -> list[HarnessHistoryItem]:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise FileNotFoundError(path) from exc
    if size > _MAX_ROLLOUT_BYTES:
        raise ValueError(f"Codex rollout exceeds 128 MiB: {path.name}")
    history: list[HarnessHistoryItem] = []
    with path.open("rb") as stream:
        for index, raw_line in enumerate(stream, start=1):
            if len(raw_line) > _MAX_LINE_BYTES:
                raise ValueError(
                    f"Codex rollout line {index} exceeds 128 MiB",
                )
            try:
                entry = json.loads(raw_line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(entry, dict):
                continue
            payload = entry.get("payload")
            if not isinstance(payload, dict):
                continue
            item = _history_item(
                str(entry.get("type") or ""),
                payload,
                index,
                str(entry.get("timestamp") or ""),
            )
            history.extend(item)
    return history


def _history_item(
    entry_type: str,
    payload: dict[str, Any],
    index: int,
    timestamp: str,
) -> list[HarnessHistoryItem]:
    payload_type = str(payload.get("type") or "")
    item_id = str(
        payload.get("id")
        or payload.get("call_id")
        or _fallback_item_id(entry_type, payload, index, timestamp),
    )
    event_kinds = {
        "user_message": HarnessHistoryKind.USER,
        "agent_message": HarnessHistoryKind.MESSAGE,
        "agent_reasoning": HarnessHistoryKind.REASONING,
    }
    if entry_type == "event_msg":
        kind = event_kinds.get(payload_type)
        return _text_item(kind, payload, item_id) if kind is not None else []
    if entry_type != "response_item":
        return []
    if payload_type in {
        "custom_tool_call",
        "function_call",
        "local_shell_call",
        "computer_call",
        "web_search_call",
    }:
        tool_name = str(
            payload.get("name")
            or (
                "shell" if payload_type == "local_shell_call" else payload_type
            ),
        )
        arguments = (
            payload.get("input")
            if "input" in payload
            else payload.get("arguments", payload.get("action", {}))
        )
        return [
            HarnessHistoryItem(
                kind=HarnessHistoryKind.TOOL_CALL,
                item_id=item_id,
                tool_name=tool_name,
                data={"arguments": arguments, "provider_type": payload_type},
            ),
        ]
    if payload_type in {
        "custom_tool_call_output",
        "function_call_output",
        "local_shell_call_output",
        "computer_call_output",
    }:
        return [
            HarnessHistoryItem(
                kind=HarnessHistoryKind.TOOL_OUTPUT,
                text=_visible_text(payload),
                item_id=item_id,
                data={"provider_type": payload_type},
            ),
        ]
    return []


def _fallback_item_id(
    entry_type: str,
    payload: dict[str, Any],
    index: int,
    timestamp: str,
) -> str:
    """Build a stable ID for copied lineage events lacking provider IDs."""
    try:
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        serialized = repr(payload)
    digest = hashlib.sha256(
        f"{timestamp}\0{entry_type}\0{serialized}".encode(
            "utf-8",
            errors="replace",
        ),
    ).hexdigest()[:24]
    return f"rollout-{digest or index}"


def _text_item(
    kind: HarnessHistoryKind,
    payload: dict[str, Any],
    item_id: str,
) -> list[HarnessHistoryItem]:
    text = _visible_text(payload)
    if not text:
        return []
    return [HarnessHistoryItem(kind=kind, text=text, item_id=item_id)]


def _visible_text(payload: dict[str, Any]) -> str:
    for key in ("message", "text", "output"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    content = payload.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        values: list[str] = []
        for block in content:
            if isinstance(block, str):
                values.append(block)
            elif isinstance(block, dict):
                value = block.get("text") or block.get("output_text")
                if value:
                    values.append(str(value))
        return "\n".join(values)
    if payload.get("output") is not None:
        try:
            return json.dumps(payload["output"], ensure_ascii=False)
        except (TypeError, ValueError):
            return str(payload["output"])
    return ""


__all__ = [
    "CodexRolloutReader",
    "CodexRolloutRecord",
    "codex_non_root_session_kind",
]
