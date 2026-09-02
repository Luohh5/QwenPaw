# -*- coding: utf-8 -*-
"""Native runtime coverage for long-running slash-command progress."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from agentscope.message import Msg, TextBlock

from qwenpaw.runtime.hooks import HookRegistry
from qwenpaw.runtime.runtime import Runtime
from qwenpaw.runtime.slash_command_registry import (
    CommandSpec,
    SlashCommandRegistry,
)
from qwenpaw.schemas import AgentRequest, Message, Role, TextContent


@pytest.mark.asyncio
async def test_native_runtime_streams_command_progress(tmp_path) -> None:
    async def _handler(ctx, _args):
        reporter = ctx.extras["_command_progress_reporter"]
        await reporter("读取会话 1/2")
        await reporter("读取会话 2/2")
        return Msg(
            name="assistant",
            role="assistant",
            content=[TextBlock(type="text", text="Command complete")],
        )

    command_registry = SlashCommandRegistry()
    command_registry.register(
        CommandSpec(name="progress", handler=_handler, category="control"),
    )
    workspace = SimpleNamespace(
        workspace_dir=tmp_path,
        agent_id="agent",
        plugins=SimpleNamespace(
            slash_command_registry=command_registry,
            hook_registry=HookRegistry(),
            modes=[],
        ),
    )
    runtime = Runtime(workspace=workspace, app_services=None)
    request = AgentRequest(
        session_id="chat-1",
        input=[
            Message(
                role=Role.USER,
                content=[TextContent(text="/progress")],
            ),
        ],
    )

    output = [item async for item in runtime.run(request)]
    deltas = [
        item.text
        for item in output
        if getattr(item, "object", None) == "content"
        and getattr(item, "delta", False)
    ]

    assert any("读取会话 1/2" in item for item in deltas)
    assert any("读取会话 2/2" in item for item in deltas)
    final_response = output[-1]
    assert final_response.status == "completed"
    final_text = final_response.output[-1].content[0].text
    assert "读取会话 1/2" in final_text
    assert final_text.endswith("Command complete")
