# -*- coding: utf-8 -*-
"""Regression tests for current Qoder IDE session discovery."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from qwenpaw.harnesses.events import HarnessHistoryKind
from qwenpaw.portability.providers.qoder import QoderMigrationProvider
from qwenpaw.portability.providers.qoder_sessions import (
    discover_qoder_transcripts,
)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )


def _message(
    session_id: str,
    message_type: str,
    content,
    *,
    cwd: str,
    timestamp: str,
) -> dict:
    return {
        "type": message_type,
        "uuid": f"{message_type}-{timestamp}",
        "sessionId": session_id,
        "timestamp": timestamp,
        "cwd": cwd,
        "message": {"role": message_type, "content": content},
    }


def _create_index(user_data: Path, editor_id: str, quest_id: str) -> None:
    database = user_data / "globalStorage" / "state.vscdb"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE ItemTable (key TEXT UNIQUE ON CONFLICT REPLACE, "
            "value BLOB)",
        )
        connection.executemany(
            "INSERT INTO ItemTable(key, value) VALUES (?, ?)",
            [
                (
                    "lingma.chat.localHistory.workspace-1",
                    json.dumps(
                        [
                            {
                                "sessionId": editor_id,
                                "title": "Editor history title",
                                "timestamp": 1_700_000_000_000,
                            },
                        ],
                    ),
                ),
                (
                    "lingma.chat.localHistory.agents.quest",
                    json.dumps(
                        [
                            {
                                "sessionId": quest_id,
                                "title": "Quest history title",
                                "timestamp": 1_700_000_100_000,
                            },
                        ],
                    ),
                ),
                (f"chat.chatMode.session.{editor_id}", "agent"),
                (f"chat.chatMode.session.{quest_id}", "plan"),
                (
                    "aicoding.questTaskListSnapshot",
                    json.dumps(
                        {
                            "folders": [
                                {
                                    "tasks": [
                                        {
                                            "id": "quest-task-1",
                                            "name": "Quest snapshot title",
                                            "status": "completed",
                                            "questType": "agent",
                                            "executionMode": "plan",
                                            "designSessionId": "design-1",
                                            "executionSessionId": quest_id,
                                            "filePath": "/projects/quest",
                                        },
                                    ],
                                },
                            ],
                        },
                    ),
                ),
            ],
        )


@pytest.mark.asyncio
async def test_provider_imports_ide_editor_and_quest_transcripts(
    tmp_path: Path,
) -> None:
    qoder_home = tmp_path / ".qoder"
    user_data = tmp_path / "Qoder" / "User"
    project = qoder_home / "projects" / "-projects-demo" / "transcript"
    editor_id = "11111111-1111-4111-8111-111111111111"
    quest_id = "task-abc123.session.execution"
    _write_jsonl(
        project / f"{editor_id}.jsonl",
        [
            _message(
                editor_id,
                "user",
                "Fix the editor test",
                cwd="/projects/editor",
                timestamp="2026-08-01T01:00:00Z",
            ),
            _message(
                editor_id,
                "assistant",
                [{"type": "text", "text": "Fixed"}],
                cwd="/projects/editor",
                timestamp="2026-08-01T01:01:00Z",
            ),
        ],
    )
    _write_jsonl(
        project / f"{quest_id}.jsonl",
        [
            {
                "type": "session_meta",
                "sessionId": quest_id,
                "cwd": "/projects/quest",
                "timestamp": "2026-08-02T01:00:00Z",
                "data": {
                    "content": {
                        "mode": "plan",
                        "session_type": "assistant",
                    },
                },
            },
            _message(
                quest_id,
                "user",
                "Run the Quest",
                cwd="/projects/quest",
                timestamp="2026-08-02T01:00:01Z",
            ),
            _message(
                quest_id,
                "assistant",
                [
                    {
                        "type": "tool_use",
                        "id": "tool-1",
                        "name": "Read",
                        "input": {"path": "README.md"},
                    },
                    {"type": "text", "text": "Quest complete"},
                ],
                cwd="/projects/quest",
                timestamp="2026-08-02T01:02:00Z",
            ),
        ],
    )
    _create_index(user_data, editor_id, quest_id)

    progress: list[str] = []

    async def _progress(message: str) -> None:
        progress.append(message)

    inventory = await QoderMigrationProvider(
        SimpleNamespace(workspace_dir=tmp_path),
        qoder_home=qoder_home,
        qoder_user_data=user_data,
    ).inventory(limit=10, progress=_progress)

    sessions = {item.source_id: item for item in inventory.sessions}
    assert set(sessions) == {editor_id, quest_id}
    assert sessions[editor_id].title == "Editor history title"
    assert sessions[editor_id].cwd == "/projects/editor"
    assert sessions[editor_id].metadata["session_kind"] == "editor"
    assert sessions[editor_id].metadata["mode"] == "agent"
    assert [item.kind for item in sessions[editor_id].history] == [
        HarnessHistoryKind.USER,
        HarnessHistoryKind.MESSAGE,
    ]

    quest = sessions[quest_id]
    assert quest.title == "Quest snapshot title"
    assert quest.cwd == "/projects/quest"
    assert quest.metadata["session_kind"] == "quest"
    assert quest.metadata["quest"] == {
        "task_id": "quest-task-1",
        "type": "agent",
        "status": "completed",
        "execution_mode": "plan",
        "design_session_id": "design-1",
    }
    quest_kinds = {item.kind for item in quest.history}
    assert HarnessHistoryKind.TOOL_CALL in quest_kinds
    assert any("发现 2 个 Qoder 会话候选文件" in item for item in progress)
    assert any("识别出 2 个用户可见 Qoder 会话" in item for item in progress)


@pytest.mark.asyncio
async def test_provider_imports_transcripts_without_sdk_or_ui_database(
    tmp_path: Path,
) -> None:
    qoder_home = tmp_path / ".qoder"
    session_id = "task-no-database.session.execution"
    transcript = (
        qoder_home
        / "projects"
        / "-projects-fallback"
        / "transcript"
        / f"{session_id}.jsonl"
    )
    _write_jsonl(
        transcript,
        [
            _message(
                session_id,
                "user",
                "Fallback title from first message",
                cwd="/projects/fallback",
                timestamp="2026-08-03T01:00:00Z",
            ),
        ],
    )

    inventory = await QoderMigrationProvider(
        SimpleNamespace(workspace_dir=tmp_path),
        qoder_home=qoder_home,
        qoder_user_data=tmp_path / "missing-user-data",
    ).inventory(limit=10)

    assert len(inventory.sessions) == 1
    session = inventory.sessions[0]
    assert session.source_id == session_id
    assert session.title == "Fallback title from first message"
    assert session.cwd == "/projects/fallback"


def test_discovery_prefers_ide_layout_over_legacy_sdk_copy(
    tmp_path: Path,
) -> None:
    qoder_home = tmp_path / ".qoder"
    project = qoder_home / "projects" / "-projects-demo"
    session_id = "11111111-1111-4111-8111-111111111111"
    _write_jsonl(project / f"{session_id}.jsonl", [{"legacy": True}])
    _write_jsonl(
        project / "transcript" / f"{session_id}.jsonl",
        [{"ide": True}],
    )

    records = discover_qoder_transcripts(qoder_home)

    assert len(records) == 1
    assert records[0].layout == "ide"
    assert records[0].path.parent.name == "transcript"


@pytest.mark.asyncio
async def test_provider_filters_internal_agent_tool_only_traces(
    tmp_path: Path,
) -> None:
    qoder_home = tmp_path / ".qoder"
    transcript = qoder_home / "projects" / "-project" / "transcript"
    worker_id = "22222222-2222-4222-8222-222222222222"
    visible_id = "33333333-3333-4333-8333-333333333333"
    _write_jsonl(
        transcript / f"{worker_id}.jsonl",
        [
            _message(
                worker_id,
                "assistant",
                [
                    {
                        "type": "tool_use",
                        "id": "tool-1",
                        "name": "Bash",
                        "input": {"command": "pwd"},
                    },
                ],
                cwd="/project",
                timestamp="2026-08-04T01:00:00Z",
            ),
            _message(
                worker_id,
                "user",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool-1",
                        "content": "/project",
                    },
                ],
                cwd="/project",
                timestamp="2026-08-04T01:00:01Z",
            ),
        ],
    )
    _write_jsonl(
        transcript / f"{visible_id}.jsonl",
        [
            _message(
                visible_id,
                "user",
                "Visible conversation",
                cwd="/project",
                timestamp="2026-08-03T01:00:00Z",
            ),
        ],
    )

    inventory = await QoderMigrationProvider(
        SimpleNamespace(workspace_dir=tmp_path),
        qoder_home=qoder_home,
        qoder_user_data=tmp_path / "missing-user-data",
    ).inventory(limit=1)

    assert [item.source_id for item in inventory.sessions] == [visible_id]
    assert inventory.ignored_session_ids == [worker_id]
    assert any("internal Agent/Experts" in item for item in inventory.warnings)
