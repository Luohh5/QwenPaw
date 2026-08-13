# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from pathlib import Path

from qwenpaw.harnesses.codex.rollout_reader import CodexRolloutReader
from qwenpaw.harnesses.events import HarnessHistoryKind


def _line(entry_type: str, payload: dict, timestamp: str) -> str:
    return json.dumps(
        {"timestamp": timestamp, "type": entry_type, "payload": payload},
    )


def test_rollout_reader_indexes_and_normalizes_visible_history(
    tmp_path: Path,
) -> None:
    thread_id = "019fe9ac-2e78-7a10-a196-27b001cdf1f5"
    project = tmp_path / "project"
    project.mkdir()
    rollout = (
        tmp_path
        / ".codex/sessions/2026/08/12"
        / f"rollout-2026-08-12T00-00-00-{thread_id}.jsonl"
    )
    rollout.parent.mkdir(parents=True)
    rollout.write_text(
        "\n".join(
            [
                _line(
                    "session_meta",
                    {
                        "id": thread_id,
                        "cwd": str(project),
                        "timestamp": "2026-08-12T00:00:00Z",
                    },
                    "2026-08-12T00:00:00Z",
                ),
                _line(
                    "event_msg",
                    {"type": "user_message", "message": "Fix import"},
                    "2026-08-12T00:00:01Z",
                ),
                _line(
                    "event_msg",
                    {"type": "agent_message", "message": "Working"},
                    "2026-08-12T00:00:02Z",
                ),
                _line(
                    "response_item",
                    {
                        "type": "custom_tool_call",
                        "call_id": "call-1",
                        "name": "exec",
                        "input": "pytest",
                    },
                    "2026-08-12T00:00:03Z",
                ),
                _line(
                    "response_item",
                    {
                        "type": "custom_tool_call_output",
                        "call_id": "call-1",
                        "output": "passed",
                    },
                    "2026-08-12T00:00:04Z",
                ),
            ],
        )
        + "\n",
        encoding="utf-8",
    )
    reader = CodexRolloutReader(tmp_path / ".codex")

    threads = reader.list_threads(limit=10)
    history = reader.read_thread(thread_id)

    assert threads[0]["id"] == thread_id
    assert threads[0]["cwd"] == str(project)
    assert threads[0]["preview"] == "Fix import"
    assert [item.kind for item in history] == [
        HarnessHistoryKind.USER,
        HarnessHistoryKind.MESSAGE,
        HarnessHistoryKind.TOOL_CALL,
        HarnessHistoryKind.TOOL_OUTPUT,
    ]
    assert history[-1].text == "passed"


def test_rollout_reader_stitches_compacted_lineage(tmp_path: Path) -> None:
    thread_id = "019fe9ac-2e78-7a10-a196-27b001cdf1f5"
    root = tmp_path / ".codex/sessions/2026/08/12"
    root.mkdir(parents=True)
    first = root / f"rollout-2026-08-12T00-00-00-{thread_id}.jsonl"
    continuation = root / (
        "rollout-2026-08-12T01-00-00-"
        "019ff000-0000-7000-8000-000000000000.jsonl"
    )
    first.write_text(
        "\n".join(
            [
                _line(
                    "session_meta",
                    {"id": thread_id, "timestamp": "2026-08-12T00:00:00Z"},
                    "2026-08-12T00:00:00Z",
                ),
                _line(
                    "event_msg",
                    {"type": "user_message", "message": "First part"},
                    "2026-08-12T00:00:01Z",
                ),
            ],
        )
        + "\n",
        encoding="utf-8",
    )
    continuation.write_text(
        "\n".join(
            [
                _line(
                    "session_meta",
                    {"id": thread_id, "timestamp": "2026-08-12T01:00:00Z"},
                    "2026-08-12T01:00:00Z",
                ),
                _line(
                    "event_msg",
                    {"type": "agent_message", "message": "Continued part"},
                    "2026-08-12T01:00:01Z",
                ),
            ],
        )
        + "\n",
        encoding="utf-8",
    )
    reader = CodexRolloutReader(tmp_path / ".codex")

    threads = reader.list_threads(limit=10)
    history = reader.read_thread(thread_id)

    assert len(threads) == 1
    assert threads[0]["rolloutLineageLength"] == 2
    assert [item.text for item in history] == ["First part", "Continued part"]
