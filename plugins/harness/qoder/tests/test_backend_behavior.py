# -*- coding: utf-8 -*-
"""Qoder behavior through the public Harness context and SDK DTOs."""

# Pytest fixtures are injected by their declared names.
# pylint: disable=redefined-outer-name

import asyncio
import base64
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from qoder_agent_sdk import (
    PermissionResultAllow,
    SessionMessage,
    ToolPermissionContext,
)
from qwenpaw.plugins.harnesses import (
    HarnessAttachment,
    HarnessAttachmentKind,
    HarnessEventKind,
    HarnessMCPServerDefinition,
    HarnessOperationNotSupportedError,
    HarnessRunRequest,
    HarnessRuntimeCapabilities,
    HarnessSkillDefinition,
)


@pytest.fixture()
def backend_factory(harness_context, client_factory, monkeypatch):
    from plugins.harness.qoder.adapter import QoderBackend
    from plugins.harness.qoder.discovery import QoderBinaryResolution

    monkeypatch.setattr(
        "plugins.harness.qoder.adapter.resolve_qoder_binary_info",
        lambda _binary: QoderBinaryResolution(
            Path("/fake/qodercli"),
            "configured",
        ),
    )
    return lambda: QoderBackend(
        harness_context,
        client_factory=client_factory,
        binary="/fake/qodercli",
    )


async def test_models_commands_and_explicit_unsupported_operations(
    backend_factory,
    call_context,
    client_factory,
):
    backend = backend_factory()
    context = call_context()
    try:
        models = await backend.models(context)
        assert [
            (model.id, model.is_default, model.default_reasoning_effort)
            for model in models
        ] == [("model-without-default", True, "high")]
        assert client_factory.created[0].disconnected
        events = await backend.run_command(context, "compact", "")
        assert events[-1].kind == HarnessEventKind.COMPLETED
        assert client_factory.created[-1].prompts == [("/compact", "default")]
        with pytest.raises(HarnessOperationNotSupportedError):
            await backend.run_command(context, "status", "")
        with pytest.raises(HarnessOperationNotSupportedError):
            await backend.discover_mcp(context)
        with pytest.raises(HarnessOperationNotSupportedError):
            await backend.logout(context)
    finally:
        await backend.close()


async def test_status_token_and_read_only_vendor_skills(
    backend_factory,
    call_context,
    client_factory,
    monkeypatch,
):
    backend = backend_factory()
    context = call_context()
    monkeypatch.setenv("QODER_PERSONAL_ACCESS_TOKEN", "test-token")
    monkeypatch.setattr(
        backend,
        "_run_cli",
        AsyncMock(return_value="Account: Not logged in"),
    )
    client_factory.server_info = {
        "skills": [
            {"name": "find-skills", "source": "user"},
            {"name": "qwenpaw-runtime:review", "source": "plugin"},
        ],
    }
    try:
        status = await backend.status(context)
        assert status.installed and status.authenticated
        assert status.account == {"type": "accessToken"}
        monkeypatch.delenv("QODER_PERSONAL_ACCESS_TOKEN")
        backend._run_cli.return_value = (  # pylint: disable=protected-access
            "Version: 1.0.47\nUsername: qoder-user\nEmail: user@example.com"
        )
        cli_status = await backend.status(context)
        assert cli_status.account == {
            "type": "qodercli",
            "username": "qoder-user",
            "email": "user@example.com",
        }
        skills = await backend.discover_skills(context)
        assert [
            (skill.name, skill.provider_id, skill.read_only)
            for skill in skills
        ] == [
            ("find-skills", "qoder", True),
        ]
        client = client_factory.created[-1]
        assert client.options.setting_sources == ["user", "project", "local"]
        assert client.disconnected
    finally:
        await backend.close()


async def test_qoder_projects_scoped_skills_mcp_and_attachments(
    backend_factory,
    harness_context,
    call_context,
    client_factory,
):
    skill = harness_context.workspace_dir / "skills" / "review"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: review\n---\n",
        encoding="utf-8",
    )
    image = harness_context.workspace_dir / "media" / "mockup.png"
    image.parent.mkdir()
    image.write_bytes(b"image-bytes")
    document = harness_context.workspace_dir / "media" / "product brief.pdf"
    document.write_bytes(b"document-bytes")
    capabilities = HarnessRuntimeCapabilities(
        skills=(HarnessSkillDefinition("review", skill),),
        mcp_servers=(
            HarnessMCPServerDefinition(
                name="docs",
                display_name="Docs",
                transport="streamable_http",
                url="https://mcp.example.test",
                headers={"Authorization": "Bearer secret"},
                tools=("search",),
                tool_policies={"search": "ask"},
            ),
        ),
    )
    context = replace(
        call_context(),
        runtime_capabilities=capabilities,
        settings={"model": "auto", "reasoning_effort": "high"},
    )
    backend = backend_factory()
    try:
        events = [
            event
            async for event in backend.run_turn(
                context,
                HarnessRunRequest(
                    "Review",
                    (
                        HarnessAttachment(HarnessAttachmentKind.IMAGE, image),
                        HarnessAttachment(
                            HarnessAttachmentKind.FILE,
                            document,
                        ),
                    ),
                ),
            )
        ]
        assert [event.kind for event in events] == [
            HarnessEventKind.TEXT_DELTA,
            HarnessEventKind.COMPLETED,
        ]
        client = client_factory.created[0]
        options = client.options
        assert options.model == "auto"
        assert options.extra_args == {"reasoning-effort": "high"}
        assert options.allowed_mcp_server_names == ["docs"]
        assert options.mcp_servers["docs"]["headers"] == {
            "Authorization": "Bearer secret",
        }
        assert options.mcp_servers["docs"]["tools"] == [
            {"name": "search", "permission_policy": "always_ask"},
        ]
        projected = Path(options.plugins[0]["path"])
        assert projected.is_relative_to(harness_context.state_dir)
        assert (projected / "skills" / "review" / "SKILL.md").is_file()
        messages, _ = client.prompts[0]
        content = messages[0]["message"]["content"]
        assert content[0] == {
            "type": "text",
            "text": '@"media/product brief.pdf"\nReview',
        }
        assert content[1]["source"]["data"] == base64.b64encode(
            b"image-bytes",
        ).decode("ascii")
        _ = [
            event
            async for event in backend.run_turn(
                context,
                HarnessRunRequest(
                    "",
                    (HarnessAttachment(HarnessAttachmentKind.FILE, document),),
                ),
            )
        ]
        attachment_only, _ = client.prompts[-1]
        assert attachment_only[0]["message"]["content"] == [
            {"type": "text", "text": '@"media/product brief.pdf"'},
        ]
    finally:
        await backend.close()


async def test_history_reset_and_capability_change_resume_the_same_identity(
    backend_factory,
    harness_context,
    call_context,
    client_factory,
    monkeypatch,
):
    context = call_context("alice")
    backend = backend_factory()
    deleted = []
    try:
        _ = [
            event
            async for event in backend.run_turn(
                context,
                HarnessRunRequest("First"),
            )
        ]
        first = client_factory.created[0]
        native_id = first.options.session_id
        messages = [
            SessionMessage(
                type="assistant",
                uuid="assistant-1",
                session_id=native_id,
                message={"content": [{"type": "text", "text": "Recovered"}]},
            ),
        ]

        def read(session_id, cwd):
            assert (session_id, cwd) == (native_id, str(context.cwd))
            return messages

        monkeypatch.setattr(
            "plugins.harness.qoder.adapter.get_session_messages",
            read,
        )
        history = await backend.history(context)
        assert [item.text for item in history] == ["Recovered"]
        skill = harness_context.workspace_dir / "skills" / "review"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("name: review", encoding="utf-8")
        changed = replace(
            context,
            runtime_capabilities=HarnessRuntimeCapabilities(
                skills=(HarnessSkillDefinition("review", skill),),
            ),
        )
        _ = [
            event
            async for event in backend.run_turn(
                changed,
                HarnessRunRequest("Continue"),
            )
        ]
        second = client_factory.created[1]
        assert first.disconnected
        assert second.options.resume == native_id
        assert second.prompts == [("Continue", "default")]
        monkeypatch.setattr(
            "plugins.harness.qoder.adapter.delete_session",
            deleted.append,
        )
        await backend.reset_session(changed)
        assert deleted == [native_id]
        assert second.disconnected
        assert await backend.history(changed) == ()
    finally:
        await backend.close()


async def test_login_process_is_reaped_on_close_and_cancelled_launch(
    backend_factory,
    call_context,
    monkeypatch,
):
    class Process:
        def __init__(self):
            self.returncode = None
            self.terminated = False
            self.reaped = asyncio.Event()

        def terminate(self):
            self.terminated = True
            self.returncode = -15
            self.reaped.set()

        async def communicate(self):
            await self.reaped.wait()
            return b"", b""

        async def wait(self):
            await self.reaped.wait()
            return self.returncode

    created = []
    entered, release = asyncio.Event(), asyncio.Event()
    block_next = False

    async def spawn(*_args, **_kwargs):
        process = Process()
        created.append(process)
        if block_next:
            entered.set()
            await release.wait()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    first = backend_factory()
    context = call_context()
    login = await first.start_login(context)
    assert login.data["type"] == "external"
    await first.close()
    assert created[0].terminated and created[0].reaped.is_set()

    second = backend_factory()
    block_next = True
    task = asyncio.create_task(second.start_login(context))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await second.close()
    assert created[1].terminated and created[1].reaped.is_set()


async def test_live_approval_is_scoped_to_call_and_cancelled_with_turn(
    backend_factory,
    call_context,
    client_factory,
):
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def pending(summary, *, timeout):
        assert (summary.tool_name, summary.parameters["provider"]) == (
            "Bash",
            "qoder",
        )
        assert timeout > 0
        entered.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    context = replace(call_context("alice"), _request_approval=pending)
    backend = backend_factory()
    client = client_factory.created
    response_started = asyncio.Event()

    async def blocked_response():
        response_started.set()
        await asyncio.Future()
        yield None

    # Client is created when the turn starts; replace its response with a
    # pending fake so the approval can be observed before turn cancellation.
    async def consume():
        async for _ in backend.run_turn(context, HarnessRunRequest("Wait")):
            pass

    task = asyncio.create_task(consume())
    try:
        while not client:
            await asyncio.sleep(0)
        client[0].receive_response = blocked_response
        await asyncio.wait_for(response_started.wait(), 2)
        permission = ToolPermissionContext(
            display_name="Shell",
            description="Run tests",
            tool_use_id="tool-1",
        )
        approval = asyncio.create_task(
            client[0].options.can_use_tool(
                "Bash",
                {"command": "pytest"},
                permission,
            ),
        )
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(cancelled.wait(), 2)
        assert approval.done()
        assert client[0].interrupted
        # A saved callback cannot reuse Alice's old authorization.
        result = await client[0].options.can_use_tool(
            "Bash",
            {"command": "pytest"},
            permission,
        )
        assert not isinstance(result, PermissionResultAllow)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await backend.close()
