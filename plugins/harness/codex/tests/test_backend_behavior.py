# -*- coding: utf-8 -*-
"""Codex RPC behavior through the public Harness SDK boundary."""

# pylint: disable=protected-access

import asyncio
import errno
import json
import sys
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from qwenpaw.plugins import SDKError
from qwenpaw.plugins.harnesses import (
    HarnessAttachment,
    HarnessAttachmentKind,
    HarnessEventKind,
    HarnessRuntimeCapabilities,
    HarnessRunRequest,
    HarnessSkillDefinition,
)

from plugins.harness.codex.adapter import CodexBackend
from plugins.harness.codex.app_server import CodexAppServerError
from plugins.harness.codex.io_helpers import stop_process


def test_notification_conversion_keeps_reasoning_and_tool_outcome():
    reasoning = CodexBackend._convert_notification(
        {
            "method": "item/reasoning/summaryTextDelta",
            "params": {"itemId": "reason-1", "delta": "Checking"},
        },
    )
    started = CodexBackend._convert_notification(
        {
            "method": "item/started",
            "params": {
                "item": {
                    "id": "tool-1",
                    "type": "commandExecution",
                    "command": "pytest -q",
                    "status": "inProgress",
                },
            },
        },
    )
    completed = CodexBackend._convert_notification(
        {
            "method": "item/completed",
            "params": {
                "item": {
                    "id": "tool-1",
                    "type": "commandExecution",
                    "command": "pytest -q",
                    "status": "completed",
                    "aggregatedOutput": "1 passed",
                    "exitCode": 0,
                },
            },
        },
    )
    mcp = CodexBackend._convert_notification(
        {
            "method": "item/completed",
            "params": {
                "item": {
                    "id": "mcp-1",
                    "type": "mcpToolCall",
                    "server": "github",
                    "tool": "search",
                    "arguments": {"query": "bug"},
                    "result": {"content": "found"},
                },
            },
        },
    )

    assert reasoning.kind == HarnessEventKind.REASONING_DELTA
    assert reasoning.data["source"] == "summary"
    assert started.kind == HarnessEventKind.TOOL_STARTED
    assert started.tool_name == "shell"
    assert started.data["arguments"]["command"] == "pytest -q"
    assert completed.kind == HarnessEventKind.TOOL_COMPLETED
    assert completed.text == "1 passed"
    assert completed.data["exit_code"] == 0
    assert mcp.tool_name == "github.search"
    assert mcp.data["arguments"] == {"query": "bug"}
    assert '"content": "found"' in mcp.text
    assert [
        CodexBackend._tool_name(item)
        for item in (
            {"type": "fileChange"},
            {"type": "dynamicToolCall", "namespace": "docs", "tool": "search"},
            {"type": "collabAgentToolCall", "tool": "spawnAgent"},
            {"type": "webSearch"},
            {"type": "imageView"},
            {"type": "imageGeneration"},
        )
    ] == [
        "apply_patch",
        "docs.search",
        "agent.spawnAgent",
        "web_search",
        "view_image",
        "image_generation",
    ]


async def test_status_login_and_models_use_app_server(
    harness_context,
    call_context,
    codex_client,
):
    original = codex_client.request
    account = {
        "type": "chatgpt",
        "email": "person@example.com",
        "futureCredential": "private",
    }

    async def request(method, params):
        if method == "account/read":
            return {"account": account}
        if method == "account/login/start":
            return {"authUrl": "https://example.com"}
        if method == "model/list":
            return {
                "data": [
                    {
                        "model": "gpt-test-codex",
                        "supportedReasoningEfforts": [
                            {"reasoningEffort": "low"},
                            {"reasoningEffort": "medium"},
                        ],
                        "defaultReasoningEffort": "medium",
                    },
                ],
            }
        return await original(method, params)

    codex_client.request = request
    backend = CodexBackend(harness_context, client=codex_client)
    try:
        call = call_context()
        status = await backend.status(call)
        login = await backend.start_login(call)
        models = await backend.models(call)
        assert status.authenticated is True
        assert status.account == {
            "type": "chatgpt",
            "email": "person@example.com",
        }
        assert login.data == {"authUrl": "https://example.com"}
        assert [
            (model.id, model.reasoning_efforts, model.default_reasoning_effort)
            for model in models
        ] == [
            ("gpt-test-codex", ("low", "medium"), "medium"),
        ]

        account["type"] = "apiKey"
        assert (await backend.status(call)).account["type"] == "apiKey"

        codex_client.binary_resolution = None
        codex_client.start = AsyncMock(
            side_effect=CodexAppServerError("Codex CLI not found"),
        )
        missing = await backend.status(call)
        assert missing.installed is False
        assert "Repair" in missing.error
        with pytest.raises(SDKError, match="Codex CLI"):
            await backend.start_login(call)
        with pytest.raises(SDKError, match="Codex CLI"):
            await backend.models(call)
    finally:
        await backend.close()


async def test_invalid_executable_is_public_error_for_main_and_mcp(
    harness_context,
    call_context,
    tmp_path,
):
    binary = tmp_path / "codex"
    binary.write_text("ordinary text, not a program", encoding="utf-8")
    binary.chmod(0o755)
    backend = CodexBackend(harness_context, binary=str(binary))
    call = call_context()
    try:
        status = await backend.status(call)
        assert status.installed is True
        assert "codex" in status.error.lower()
        for operation in (backend.models, backend.external_mcp_records):
            with pytest.raises(SDKError) as raised:
                await operation(call)
            assert "codex" in str(raised.value).lower()
            assert isinstance(raised.value.__cause__, OSError)
            assert raised.value.__cause__.errno == errno.ENOEXEC
    finally:
        await backend.close()


async def test_provider_discovery_keeps_read_only_skill_and_mcp_metadata(
    harness_context,
    call_context,
    codex_client,
    monkeypatch,
):
    original = codex_client.request

    async def request(method, params):
        if method == "skills/list":
            assert params["cwds"] == [str(harness_context.workspace_dir)]
            return {
                "data": [
                    {
                        "skills": [
                            {
                                "name": "openai-docs",
                                "description": "Read API docs",
                                "scope": "user",
                            },
                        ],
                    },
                ],
            }
        return await original(method, params)

    codex_client.request = request
    process = AsyncMock()
    process.returncode = 0
    process.communicate.return_value = (
        json.dumps(
            [
                {
                    "name": "local-docs",
                    "enabled": True,
                    "transport": {"type": "streamable_http"},
                    "auth_status": "authenticated",
                },
            ],
        ).encode(),
        b"",
    )
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    backend = CodexBackend(harness_context, client=codex_client)
    try:
        call = call_context()
        skills = await backend.discover_skills(call)
        servers = await backend.discover_mcp(call)
        assert [
            (item.name, item.source, item.read_only, item.scope)
            for item in skills
        ] == [
            ("openai-docs", "user", True, "provider"),
        ]
        assert [
            (item.name, item.transport, item.read_only, item.scope)
            for item in servers
        ] == [
            ("local-docs", "streamable_http", True, "provider"),
        ]
        assert spawn.await_args.args == (
            "/fake/codex",
            "mcp",
            "list",
            "--json",
        )
        assert spawn.await_args.kwargs["cwd"] == str(
            harness_context.workspace_dir,
        )
    finally:
        await backend.close()


async def test_turn_preserves_settings_and_attachments(
    harness_context,
    call_context,
    codex_client,
):
    backend = CodexBackend(harness_context, client=codex_client)
    call = replace(
        call_context(),
        settings={
            "model": "gpt-test-codex",
            "reasoning_effort": "high",
            "sandbox": "read-only",
            "approval_policy": "on-request",
        },
    )
    image = harness_context.workspace_dir / "screen.png"
    document = harness_context.workspace_dir / "notes.txt"
    request = HarnessRunRequest(
        "Inspect both",
        (
            HarnessAttachment(HarnessAttachmentKind.IMAGE, image),
            HarnessAttachment(HarnessAttachmentKind.FILE, document),
        ),
    )
    try:
        events = [event async for event in backend.run_turn(call, request)]
        assert [event.kind for event in events] == [
            HarnessEventKind.TEXT_DELTA,
            HarnessEventKind.COMPLETED,
        ]
        thread = next(
            params
            for method, params in codex_client.requests
            if method == "thread/start"
        )
        turn = next(
            params
            for method, params in codex_client.requests
            if method == "turn/start"
        )
        assert thread["model"] == turn["model"] == "gpt-test-codex"
        assert thread["sandbox"] == "read-only"
        assert (
            thread["approvalPolicy"] == turn["approvalPolicy"] == "on-request"
        )
        assert turn["sandboxPolicy"] == {"type": "readOnly"}
        assert turn["effort"] == "high"
        assert turn["summary"] == "auto"
        assert turn["input"] == [
            {"type": "text", "text": "Inspect both"},
            {"type": "localImage", "path": str(image)},
            {"type": "text", "text": f"Attached file notes.txt: {document}"},
        ]
    finally:
        await backend.close()


async def test_runtime_fingerprints_isolate_clients_and_resume_on_switch(
    harness_context,
    call_context,
    codex_client,
    monkeypatch,
):
    clients = [type(codex_client)() for _ in range(3)]
    clients[1].next_thread = 10
    clients[2].next_thread = 20
    available = iter(clients)
    monkeypatch.setattr(
        "plugins.harness.codex.adapter.CodexAppServerClient",
        lambda **_kwargs: next(available),
    )
    backend = CodexBackend(harness_context)
    first_skill = harness_context.workspace_dir / "skills" / "first"
    second_skill = harness_context.workspace_dir / "skills" / "second"
    first = HarnessRuntimeCapabilities(
        skills=(
            HarnessSkillDefinition("review", first_skill, revision="one"),
        ),
    )
    second = HarnessRuntimeCapabilities(
        skills=(
            HarnessSkillDefinition("review", second_skill, revision="two"),
        ),
    )

    async def turn(call):
        return [
            event
            async for event in backend.run_turn(
                call,
                HarnessRunRequest("Continue"),
            )
        ]

    try:
        await turn(replace(call_context(), runtime_capabilities=first))
        await turn(
            replace(
                call_context(session_id="chat-2"),
                runtime_capabilities=second,
            ),
        )
        await turn(replace(call_context(), runtime_capabilities=second))
        assert (
            "skills/extraRoots/set",
            {"extraRoots": [str(first_skill)]},
        ) in clients[1].requests
        assert (
            "skills/extraRoots/set",
            {"extraRoots": [str(second_skill)]},
        ) in clients[2].requests
        assert [method for method, _ in clients[1].requests].count(
            "thread/start",
        ) == 1
        assert [method for method, _ in clients[2].requests].count(
            "thread/start",
        ) == 1
        assert ("thread/resume", {"threadId": "new-thread-11"}) in clients[
            2
        ].requests
    finally:
        await backend.close()
    assert all(client.stopped for client in clients)


async def test_commands_and_session_reset_use_the_same_native_thread(
    harness_context,
    call_context,
    codex_client,
):
    backend = CodexBackend(harness_context, client=codex_client)
    call = call_context()
    original = codex_client.request

    async def request(method, params):
        if method == "review/start":
            await codex_client.queue.put(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": params["threadId"],
                        "turn": {"id": "review-1", "status": "completed"},
                    },
                },
            )
            return {"turn": {"id": "review-1"}}
        return await original(method, params)

    codex_client.request = request
    try:
        _ = [
            event
            async for event in backend.run_turn(
                call,
                HarnessRunRequest("Start"),
            )
        ]
        compact = await backend.run_command(call, "compact", "")
        skills = await backend.run_command(call, "skills", "")
        status = await backend.run_command(call, "status", "")
        review = await backend.run_command(call, "review", "")
        assert "compacted" in compact[0].text
        assert "No Codex skills" in skills[0].text
        assert "Codex: offline" in status[0].text
        assert review[-1].kind == HarnessEventKind.COMPLETED
        assert [method for method, _ in codex_client.requests].count(
            "thread/start",
        ) == 1
        assert (
            "thread/compact/start",
            {"threadId": "new-thread-1"},
        ) in codex_client.requests
        await backend.reset_session(call)
        _ = [
            event
            async for event in backend.run_turn(
                call,
                HarnessRunRequest("New task"),
            )
        ]
        assert [method for method, _ in codex_client.requests].count(
            "thread/start",
        ) == 2
    finally:
        await backend.close()


async def test_approval_uses_public_call_context(
    harness_context,
    call_context,
    codex_client,
):
    approve = AsyncMock(return_value="approved")
    call = replace(call_context(), _request_approval=approve)
    backend = CodexBackend(harness_context, client=codex_client)
    try:

        async def request(method, params):
            if method == "turn/start":
                decision = await codex_client.server_request_handler(
                    {
                        "method": "item/commandExecution/requestApproval",
                        "params": {
                            "threadId": params["threadId"],
                            "itemId": "item-1",
                            "command": "pytest -q",
                        },
                    },
                )
                assert decision == {"decision": "accept"}
                permissions = {"network": {"enabled": True}}
                granted = await codex_client.server_request_handler(
                    {
                        "method": "item/permissions/requestApproval",
                        "params": {
                            "threadId": params["threadId"],
                            "itemId": "permission-1",
                            "permissions": permissions,
                            "reason": "Allow network access",
                        },
                    },
                )
                assert granted == {"permissions": permissions, "scope": "turn"}
                await codex_client.queue.put(
                    {
                        "method": "turn/completed",
                        "params": {
                            "threadId": params["threadId"],
                            "turn": {"id": "turn-1", "status": "completed"},
                        },
                    },
                )
                return {"turn": {"id": "turn-1"}}
            return await original(method, params)

        original = codex_client.request
        codex_client.request = request
        _ = [
            event
            async for event in backend.run_turn(
                call,
                HarnessRunRequest("Check"),
            )
        ]
        command, permissions = [
            item.args[0] for item in approve.await_args_list
        ]
        assert command.parameters["command"] == "pytest -q"
        assert command.parameters["provider_item_id"] == "item-1"
        assert permissions.parameters["permissions"] == {
            "network": {"enabled": True},
        }
        assert all(
            item.kwargs == {"timeout": 300} for item in approve.await_args_list
        )
    finally:
        await backend.close()


async def test_cancelling_turn_interrupts_native_turn(
    harness_context,
    call_context,
    codex_client,
):
    started = asyncio.Event()
    original = codex_client.request

    async def request(method, params):
        if method == "turn/start":
            started.set()
            return {"turn": {"id": "turn-1"}}
        return await original(method, params)

    codex_client.request = request
    backend = CodexBackend(harness_context, client=codex_client)

    async def consume():
        async for _ in backend.run_turn(
            call_context(),
            HarnessRunRequest("Keep working"),
        ):
            pass

    try:
        task = asyncio.create_task(consume())
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (
            "turn/interrupt",
            {"threadId": "new-thread-1", "turnId": "turn-1"},
        ) in codex_client.requests
    finally:
        await backend.close()


async def test_turn_cancellation_retires_pending_approval(
    harness_context,
    call_context,
    codex_client,
):
    pending = asyncio.Event()
    released = asyncio.Event()

    async def approval(_summary, *, timeout):
        del timeout
        pending.set()
        try:
            await asyncio.Event().wait()
        finally:
            released.set()

    call = replace(call_context(), _request_approval=approval)
    backend = CodexBackend(harness_context, client=codex_client)
    original = codex_client.request
    server_tasks = []

    async def request(method, params):
        if method == "turn/start":
            server_tasks.append(
                asyncio.create_task(
                    codex_client.server_request_handler(
                        {
                            "method": "item/commandExecution/requestApproval",
                            "params": {
                                "threadId": params["threadId"],
                                "itemId": "item-1",
                                "command": "pytest -q",
                            },
                        },
                    ),
                ),
            )
            return {"turn": {"id": "turn-1"}}
        return await original(method, params)

    codex_client.request = request

    async def consume():
        async for _ in backend.run_turn(call, HarnessRunRequest("Review")):
            pass

    turn = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(pending.wait(), 2)
        turn.cancel()
        with pytest.raises(asyncio.CancelledError):
            await turn
        await asyncio.wait_for(released.wait(), 2)
    finally:
        for task in server_tasks:
            task.cancel()
        await asyncio.gather(*server_tasks, return_exceptions=True)
        await backend.close()


async def test_close_reaps_ephemeral_mcp_discovery_process(
    harness_context,
    call_context,
    codex_client,
    monkeypatch,
):
    launched = asyncio.Event()
    reaped = asyncio.Event()

    class FakeProcess:
        returncode = None
        terminated = False

        async def communicate(self):
            launched.set()
            await reaped.wait()
            return b"", b""

        def terminate(self):
            self.terminated = True
            self.returncode = 0
            reaped.set()

        async def wait(self):
            await reaped.wait()
            return self.returncode

    process = FakeProcess()
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    backend = CodexBackend(harness_context, client=codex_client)
    task = asyncio.create_task(backend.external_mcp_records(call_context()))
    await asyncio.wait_for(launched.wait(), 2)
    await asyncio.wait_for(backend.close(), 2)
    assert task.cancelled()
    assert process.terminated is True
    assert reaped.is_set()
    assert spawn.await_args.args == ("/fake/codex", "mcp", "list", "--json")


async def test_reap_drains_a_real_child_with_unread_pipe_output():
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import os,time; os.write(2,b'ready\\n'); "
        "os.write(1,b'x' * (16 * 1024 * 1024)); time.sleep(30)",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        assert await asyncio.wait_for(child.stderr.readline(), 2) == b"ready\n"
        await asyncio.sleep(0.05)
        assert child.returncode is None
        await asyncio.wait_for(stop_process(child, drain=True), 3)
        assert child.returncode is not None
    finally:
        if child.returncode is None:
            child.kill()
        await asyncio.wait_for(child.stdout.read(), 3)
        await asyncio.wait_for(child.wait(), 3)
