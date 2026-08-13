# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import stat
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from qwenpaw.app.chats.manager import ChatManager
from qwenpaw.app.chats.models import ChatSpec
from qwenpaw.app.chats.repo import JsonChatRepository
from qwenpaw.app.chats.session import SafeJSONSession
from qwenpaw.agents.context.scroll.history import HistoryStore
from qwenpaw.agents.context.types import LogEntry
from qwenpaw.runtime._state_utils import StateProxy
from qwenpaw.portability import trace


def _workspace(tmp_path: Path):
    root = tmp_path / "workspace"
    root.mkdir()
    return SimpleNamespace(
        workspace_dir=root,
        agent_id="agent-1",
        session=SafeJSONSession(str(root / "sessions")),
        chat_manager=ChatManager(
            repo=JsonChatRepository(root / "chats.json"),
        ),
    )


@pytest.mark.asyncio
async def test_trace_export_is_redacted_signed_by_hash_and_owner_only(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = _workspace(tmp_path)
    monkeypatch.setattr(trace, "WORKING_DIR", tmp_path / "qwenpaw-data")
    chat = ChatSpec(
        id="chat-1",
        name="person@example.com session",
        session_id="console:user-1",
        user_id="user-1",
        channel="console",
    )
    await workspace.chat_manager.create_chat(chat)
    proxy = StateProxy()
    proxy.data = {
        "state": {
            "context": [
                {
                    "id": "msg-1",
                    "name": "user",
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                f"Read {Path.home()}/private and use "
                                "api_key=super-secret-value"
                            ),
                        },
                    ],
                    "metadata": {"authorization": "Bearer abcdefghijklmnop"},
                },
                {
                    "id": "msg-2",
                    "name": "assistant",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_call",
                            "id": "call-1",
                            "name": "login",
                            "input": {"password": "do-not-export"},
                        },
                    ],
                },
                {
                    "id": "msg-3",
                    "name": "assistant",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_result",
                            "id": "call-1",
                            "name": "login",
                            "output": "authenticated",
                        },
                    ],
                },
            ],
        },
    }
    await workspace.session.save_session_state(
        chat.session_id,
        chat.user_id,
        chat.channel,
        agent=proxy,
    )

    result = await trace.export_trace(workspace)

    assert result.session_count == 1
    assert result.event_count == 3
    assert result.redaction_count >= 4
    assert stat.S_IMODE(result.path.stat().st_mode) == 0o600
    with zipfile.ZipFile(result.path) as archive:
        assert set(archive.namelist()) == {
            "meta.json",
            "data/sessions.json",
            "data/traces.jsonl",
            "reports/redaction.json",
        }
        raw = archive.read("data/traces.jsonl")
        text = raw.decode("utf-8")
        assert "super-secret-value" not in text
        assert "do-not-export" not in text
        assert "person@example.com" not in archive.read(
            "data/sessions.json",
        ).decode("utf-8")
        assert str(Path.home()) not in text
        assert "$HOME/private" in text
        assert "console:user-1" not in text
        assert "msg-1" not in text
        assert "call-1" not in text
        events = [json.loads(line) for line in text.splitlines()]
        tool_call = next(
            item for item in events if item["kind"] == "tool_call"
        )
        tool_result = next(
            item for item in events if item["kind"] == "tool_result"
        )
        assert tool_call["tool_call_id"] == tool_result["tool_call_id"]
        assert (
            tool_call["content_blocks"][0]["id"] == tool_call["tool_call_id"]
        )
        assert (
            tool_result["content_blocks"][0]["id"]
            == tool_result["tool_call_id"]
        )
        catalog_text = archive.read("data/sessions.json").decode("utf-8")
        assert "chat-1" not in catalog_text
        assert "console:user-1" not in catalog_text
        meta = json.loads(archive.read("meta.json"))
        assert meta["type"] == "qwenpaw-trace"
        assert meta["agent_id"] != "agent-1"
        assert meta["redaction"]["pseudonymized_identifier_count"] >= 3
        assert meta["redaction"]["hidden_chain_of_thought_included"] is False
        assert meta["data"]["trace_sha256"] == result.sha256


@pytest.mark.asyncio
async def test_trace_merges_full_scroll_history_with_unpersisted_tail(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = _workspace(tmp_path)
    monkeypatch.setattr(trace, "WORKING_DIR", tmp_path / "qwenpaw-data")
    chat = ChatSpec(
        id="chat-scroll",
        name="long conversation",
        session_id="console:scroll-user",
        user_id="scroll-user",
        channel="console",
    )
    await workspace.chat_manager.create_chat(chat)

    history = HistoryStore(workspace.workspace_dir / "history.db")
    history.append(
        session_id=chat.session_id,
        agent_id=workspace.agent_id,
        entry=LogEntry(
            kind="context_msg",
            role="user",
            content="old evicted request",
            blocks=[{"type": "text", "text": "old evicted request"}],
        ),
        dedup_key="old-msg",
    )
    history.append(
        session_id=chat.session_id,
        agent_id=workspace.agent_id,
        entry=LogEntry(
            kind="model_turn",
            role="assistant",
            content="persisted reply",
            blocks=[{"type": "text", "text": "persisted reply"}],
        ),
        dedup_key="persisted-msg",
    )
    history.close()

    proxy = StateProxy()
    proxy.data = {
        "state": {
            "context": [
                {
                    "id": "persisted-msg",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "duplicate tail"}],
                },
                {
                    "id": "new-msg",
                    "role": "user",
                    "content": [{"type": "text", "text": "new live request"}],
                },
            ],
        },
    }
    await workspace.session.save_session_state(
        chat.session_id,
        chat.user_id,
        chat.channel,
        agent=proxy,
    )

    result = await trace.export_trace(workspace)

    assert result.event_count == 3
    with zipfile.ZipFile(result.path) as archive:
        exported = archive.read("data/traces.jsonl").decode("utf-8")
        assert "old evicted request" in exported
        assert "persisted reply" in exported
        assert "new live request" in exported
        assert "duplicate tail" not in exported
        catalog = json.loads(archive.read("data/sessions.json"))
        assert catalog[0]["storage_sources"] == {
            "scroll_history_rows": 2,
            "live_session_messages": 2,
            "merged_messages": 3,
        }
