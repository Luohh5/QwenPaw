# -*- coding: utf-8 -*-
"""Legacy Codex mapping, identity, and external archive compatibility."""

import asyncio
import json
import threading
from pathlib import Path

import pytest

from qwenpaw.plugins import SDKError
from qwenpaw.plugins.harnesses import HarnessRunRequest


def backend(context, client):
    from plugins.harness.codex.adapter import CodexBackend

    return CodexBackend(context, client=client)


def legacy_file(context):
    path = context.workspace_dir / "harnesses" / "codex_sessions.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


async def turn(instance, call):
    async with asyncio.timeout(3):
        return [
            event
            async for event in instance.run_turn(
                call,
                HarnessRunRequest("Continue the task"),
            )
        ]


async def test_resume_existing_thread_mapping(
    harness_context,
    call_context,
    codex_client,
):
    old = legacy_file(harness_context)
    old.write_text('{"chat-1":"native-thread"}', encoding="utf-8")
    instance = backend(harness_context, codex_client)
    try:
        await instance.prepare()
        await turn(instance, call_context("alice"))
        await turn(instance, call_context("bob"))
        await turn(instance, call_context("alice", channel="slack"))
        history = await instance.history(call_context("alice"))
        assert [item.text for item in history] == ["Old prompt", "Old answer"]
        assert (
            "thread/resume",
            {"threadId": "native-thread"},
        ) in codex_client.requests
        assert [
            method
            for method, _ in codex_client.requests
            if method == "thread/start"
        ] == ["thread/start", "thread/start"]
        saved = json.loads(
            (harness_context.state_dir / "codex_sessions.json").read_text(),
        )
        assert set(saved.values()) == {
            "native-thread",
            "new-thread-1",
            "new-thread-2",
        }
        assert old.read_text(encoding="utf-8") == '{"chat-1":"native-thread"}'
    finally:
        await instance.close()


async def test_state_copy_keeps_legacy_and_existing_target(
    harness_context,
    codex_client,
):
    old = legacy_file(harness_context)
    old.write_text('{"chat-1":"old-thread"}', encoding="utf-8")
    target = harness_context.state_dir / "codex_sessions.json"
    preferred = {json.dumps(["alice", "console", "chat-1"]): "target-thread"}
    target.write_text(json.dumps(preferred), encoding="utf-8")
    original = target.read_bytes()
    instance = backend(harness_context, codex_client)
    try:
        await instance.prepare()
        assert target.read_bytes() == original
        assert old.read_text(encoding="utf-8") == '{"chat-1":"old-thread"}'
    finally:
        await instance.close()


async def test_existing_null_target_is_not_replaced_by_legacy_mapping(
    harness_context,
    codex_client,
):
    old = legacy_file(harness_context)
    old.write_bytes(b'{"chat-1":"old-thread"}')
    target = harness_context.state_dir / "codex_sessions.json"
    target.write_bytes(b"null")
    instance = backend(harness_context, codex_client)
    try:
        with pytest.raises(SDKError) as raised:
            await instance.prepare()
        assert "codex" in str(raised.value).lower()
        assert (
            "state" in str(raised.value).lower()
            or "mapping"
            in str(
                raised.value,
            ).lower()
        )
        assert target.read_bytes() == b"null"
        assert old.read_bytes() == b'{"chat-1":"old-thread"}'
    finally:
        await instance.close()


@pytest.mark.parametrize("corrupt_target", [False, True])
async def test_corrupt_state_is_public_error_without_overwrite(
    harness_context,
    codex_client,
    corrupt_target,
):
    old = legacy_file(harness_context)
    old.write_bytes(
        b'{"chat-1":"old-thread"}' if corrupt_target else b"{not JSON",
    )
    target = harness_context.state_dir / "codex_sessions.json"
    if corrupt_target:
        target.write_bytes(b"{not JSON")
    instance = backend(harness_context, codex_client)
    try:
        with pytest.raises(SDKError) as raised:
            await instance.prepare()
        assert "codex" in str(raised.value).lower()
        assert (
            "state" in str(raised.value).lower()
            or "mapping"
            in str(
                raised.value,
            ).lower()
        )
        assert old.read_bytes() == (
            b'{"chat-1":"old-thread"}' if corrupt_target else b"{not JSON"
        )
        if corrupt_target:
            assert target.read_bytes() == b"{not JSON"
        else:
            assert not target.exists()
    finally:
        await instance.close()


async def test_legacy_copy_holds_lock_without_blocking_loop(
    harness_context,
    codex_client,
    monkeypatch,
):
    old = legacy_file(harness_context)
    old.write_text('{"chat-1":"native-thread"}', encoding="utf-8")
    entered = threading.Event()
    release = threading.Event()
    original = Path.read_bytes

    def held_read(path):
        if path == old:
            entered.set()
            release.wait(3)
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", held_read)
    instance = backend(harness_context, codex_client)
    first = asyncio.create_task(instance.prepare())
    second = None
    try:
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 2), 3)
        heartbeat = asyncio.Event()
        asyncio.get_running_loop().call_soon(heartbeat.set)
        await asyncio.wait_for(heartbeat.wait(), 2)
        first.cancel()
        second = asyncio.create_task(instance.prepare())
        await asyncio.sleep(0)
        assert not second.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        if second is not None:
            await asyncio.wait_for(second, 2)
        await instance.close()
    assert json.loads(
        (harness_context.state_dir / "codex_sessions.json").read_text(),
    ) == {"chat-1": "native-thread"}


async def test_archive_preserves_external_thread_metadata(
    harness_context,
    call_context,
    codex_client,
):
    instance = backend(harness_context, codex_client)
    try:
        await instance.prepare()
        call = call_context()
        threads = await instance.list_external_threads(
            call,
            limit=10,
            archived=True,
        )
        history = await instance.read_external_thread(call, "native-thread")
        assert threads == (
            {
                "id": "native-thread",
                "preview": "Old Codex task",
                "cwd": "/external/project",
                "archived": True,
            },
        )
        assert [item.text for item in history] == ["Old prompt", "Old answer"]
        assert (
            "thread/list",
            {"limit": 10, "archived": True},
        ) in codex_client.requests
    finally:
        await instance.close()
