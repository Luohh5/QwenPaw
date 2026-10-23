# -*- coding: utf-8 -*-
"""Tests for Codex app-server JSON-RPC request handling."""

# pylint: disable=protected-access

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plugins.harness.codex.app_server import (
    CodexAppServerClient,
    _STDIO_STREAM_LIMIT_BYTES,
)


@pytest.mark.asyncio
async def test_server_request_handler_returns_decision() -> None:
    client = CodexAppServerClient()
    client._send = AsyncMock()  # type: ignore[method-assign]
    handled = asyncio.Event()

    async def accept(_message):
        handled.set()
        return {"decision": "accept"}

    handler = AsyncMock(side_effect=accept)
    client.set_server_request_handler(handler)
    message = {
        "id": 7,
        "method": "item/commandExecution/requestApproval",
        "params": {"command": "pytest"},
    }

    await client._dispatch(message)
    await asyncio.wait_for(handled.wait(), 2)

    handler.assert_awaited_once_with(message)
    client._send.assert_awaited_once_with(
        {"id": 7, "result": {"decision": "accept"}},
    )


@pytest.mark.asyncio
async def test_pending_approval_does_not_block_reader() -> None:
    lines = asyncio.Queue()
    exited = asyncio.Event()
    handler_started = asyncio.Event()
    handler_released = asyncio.Event()

    async def readline():
        return await lines.get()

    process = SimpleNamespace(
        returncode=None,
        stdout=SimpleNamespace(readline=readline),
        stderr=None,
    )

    def terminate():
        process.returncode = 0
        exited.set()

    process.terminate = terminate
    process.wait = exited.wait
    client = CodexAppServerClient()
    client._process = process
    client._send = AsyncMock()  # type: ignore[method-assign]

    async def approval(_message):
        handler_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            handler_released.set()

    client.set_server_request_handler(approval)
    client._reader_task = asyncio.create_task(client._read_stdout())
    future = asyncio.get_running_loop().create_future()
    client._pending[9] = future
    try:
        await lines.put(
            b'{"id":7,"method":'
            b'"item/commandExecution/requestApproval","params":{}}',
        )
        await asyncio.wait_for(handler_started.wait(), 2)
        await lines.put(b'{"id":9,"result":{"interrupted":true}}')
        assert await asyncio.wait_for(future, 2) == {"interrupted": True}
    finally:
        await client.stop()
    assert handler_released.is_set()


@pytest.mark.asyncio
async def test_app_server_uses_large_bounded_stdio_limit(monkeypatch) -> None:
    stream = SimpleNamespace(readline=AsyncMock(return_value=b""))
    process = SimpleNamespace(
        returncode=None,
        stdin=SimpleNamespace(),
        stdout=stream,
        stderr=stream,
    )
    create = AsyncMock(return_value=process)
    monkeypatch.setattr(
        "plugins.harness.codex.app_server.resolve_codex_binary_info",
        lambda _binary: SimpleNamespace(path=Path("/tmp/codex")),
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    client = CodexAppServerClient()
    client.request = AsyncMock(return_value={})  # type: ignore[method-assign]
    client.notify = AsyncMock()  # type: ignore[method-assign]

    await client.start()
    await asyncio.sleep(0)

    assert create.await_args.kwargs["limit"] == _STDIO_STREAM_LIMIT_BYTES
