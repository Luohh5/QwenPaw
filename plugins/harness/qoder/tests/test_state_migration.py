# -*- coding: utf-8 -*-
"""Qoder session state survives extraction without crossing identities."""

import asyncio
import json
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from qwenpaw.plugins import SDKError
from qwenpaw.plugins.harnesses import HarnessRunRequest
from qwenpaw.plugins.storage import PluginStorage


def backend(context, factory, monkeypatch):
    from plugins.harness.qoder.adapter import QoderBackend
    from plugins.harness.qoder.discovery import QoderBinaryResolution

    monkeypatch.setattr(
        "plugins.harness.qoder.adapter.resolve_qoder_binary_info",
        lambda _binary: QoderBinaryResolution(
            Path("/fake/qodercli"),
            "configured",
        ),
    )

    return QoderBackend(
        context,
        client_factory=factory,
        binary="/fake/qodercli",
    )


def legacy_file(context):
    path = context.workspace_dir / "harnesses" / "qoder_sessions.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


async def turn(instance, call):
    async with asyncio.timeout(3):
        return [
            event
            async for event in instance.run_turn(
                call,
                HarnessRunRequest("Continue"),
            )
        ]


async def test_qoder_sessions_scoped_to_workspace_and_identity(
    harness_context,
    call_context,
    client_factory,
    tmp_path,
    monkeypatch,
):
    native = "550e8400-e29b-41d4-a716-446655440000"
    old = legacy_file(harness_context)
    old.write_text(json.dumps({"chat-1": native}), encoding="utf-8")
    instance = backend(harness_context, client_factory, monkeypatch)
    try:
        await instance.prepare()
        await turn(instance, call_context("alice"))
        await turn(instance, call_context("bob"))
        await turn(instance, call_context("alice", channel="slack"))
        saved = json.loads(
            (harness_context.state_dir / "qoder_sessions.json").read_text(),
        )
        assert len(saved) == 3
        assert list(saved.values()).count(native) == 1
        assert old.read_text(encoding="utf-8") == json.dumps(
            {"chat-1": native},
        )
        assert len(client_factory.created) == 3
        assert len({id(client) for client in client_factory.created}) == 3
    finally:
        await instance.close()

    other_workspace = tmp_path / "other"
    other_workspace.mkdir()
    other_state = other_workspace / "plugin-data" / "qwenpaw-harness-qoder"
    other_state.mkdir(parents=True)
    other_context = replace(
        harness_context,
        workspace_id="other",
        workspace_dir=other_workspace,
        state_dir=other_state,
        storage=PluginStorage(other_state, lambda: None),
    )
    other = backend(other_context, client_factory, monkeypatch)
    try:
        await turn(other, replace(call_context("alice"), cwd=other_workspace))
        assert client_factory.created[-1].options.resume is None
        assert (
            len(json.loads((other_state / "qoder_sessions.json").read_text()))
            == 1
        )
        assert (
            len(
                json.loads(
                    (
                        harness_context.state_dir / "qoder_sessions.json"
                    ).read_text(),
                ),
            )
            == 3
        )
    finally:
        await other.close()


@pytest.mark.parametrize(
    "target",
    [None, "null", "{not JSON", '{"chat-1":"target-native"}'],
)
async def test_qoder_mapping_migration_is_idempotent(
    harness_context,
    client_factory,
    target,
    monkeypatch,
):
    old = legacy_file(harness_context)
    old.write_bytes(b'{"chat-1":"old-native"}')
    saved = harness_context.state_dir / "qoder_sessions.json"
    if target is not None:
        saved.write_bytes(target.encode())
    instance = backend(harness_context, client_factory, monkeypatch)
    try:
        if target in {"null", "{not JSON"}:
            with pytest.raises(SDKError) as raised:
                await instance.prepare()
            assert "qoder" in str(raised.value).lower()
            assert saved.read_bytes() == target.encode()
        else:
            await instance.prepare()
            assert json.loads(saved.read_text()) == {
                "chat-1": "target-native" if target else "old-native",
            }
        assert old.read_bytes() == b'{"chat-1":"old-native"}'
    finally:
        await instance.close()


async def test_corrupt_legacy_state_is_actionable_and_preserved(
    harness_context,
    client_factory,
    monkeypatch,
):
    old = legacy_file(harness_context)
    old.write_bytes(b"{broken")
    saved = harness_context.state_dir / "qoder_sessions.json"
    instance = backend(harness_context, client_factory, monkeypatch)
    try:
        with pytest.raises(SDKError, match="Qoder session state"):
            await instance.prepare()
        assert old.read_bytes() == b"{broken"
        assert not saved.exists()
    finally:
        await instance.close()


async def test_qoder_legacy_copy_keeps_lock_until_io_finishes(
    harness_context,
    client_factory,
    monkeypatch,
):
    old = legacy_file(harness_context)
    old.write_bytes(b'{"chat-1":"old-native"}')
    entered, release = threading.Event(), threading.Event()
    original = Path.read_bytes

    def held_read(path):
        if path == old:
            entered.set()
            release.wait(3)
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", held_read)
    instance = backend(harness_context, client_factory, monkeypatch)
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


@pytest.mark.parametrize("operation", ["reset", "rebuild"])
async def test_cancelled_disconnect_does_not_reuse_closed_client(
    harness_context,
    call_context,
    client_factory,
    monkeypatch,
    operation,
):
    instance = backend(harness_context, client_factory, monkeypatch)
    call = call_context()
    await turn(instance, call)
    first = client_factory.created[0]
    native_id = first.options.session_id
    deleted = []
    monkeypatch.setattr(
        "plugins.harness.qoder.adapter.delete_session",
        deleted.append,
    )
    entered, release = asyncio.Event(), asyncio.Event()

    async def held_disconnect():
        entered.set()
        await release.wait()
        first.disconnected = True

    first.disconnect = held_disconnect
    interrupted = asyncio.create_task(
        instance.reset_session(call)
        if operation == "reset"
        else turn(instance, replace(call, settings={"model": "other"})),
    )
    try:
        await asyncio.wait_for(entered.wait(), 2)
        interrupted.cancel()
        await asyncio.sleep(0)
        interrupted.cancel()
        assert not interrupted.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await interrupted
    try:
        assert first.disconnected
        await turn(instance, call)
        assert len(client_factory.created) == 2
        assert client_factory.created[1].connected
        assert deleted == ([native_id] if operation == "reset" else [])
    finally:
        await instance.close()


async def test_cancelled_reset_delete_commits_mapping_before_next_turn(
    harness_context,
    call_context,
    client_factory,
    monkeypatch,
):
    instance = backend(harness_context, client_factory, monkeypatch)
    call = call_context()
    await turn(instance, call)
    native_id = client_factory.created[0].options.session_id
    entered, release = threading.Event(), threading.Event()
    deleted = []

    def held_delete(session_id):
        assert session_id == native_id
        entered.set()
        release.wait()
        deleted.append(session_id)

    monkeypatch.setattr(
        "plugins.harness.qoder.adapter.delete_session",
        held_delete,
    )
    reset = asyncio.create_task(instance.reset_session(call))
    next_turn = None
    try:
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait), 2)
        reset.cancel()
        await asyncio.sleep(0)
        reset.cancel()
        next_turn = asyncio.create_task(turn(instance, call))
        await asyncio.sleep(0)
        assert not reset.done() and not next_turn.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await reset
        if next_turn is not None:
            await asyncio.wait_for(next_turn, 2)
    try:
        assert deleted == [native_id]
        assert len(client_factory.created) == 2
        assert client_factory.created[1].options.resume is None
        assert client_factory.created[1].options.session_id != native_id
    finally:
        await instance.close()
