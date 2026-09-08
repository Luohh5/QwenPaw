# -*- coding: utf-8 -*-
# Pytest injects named fixtures; these tests also inspect agent internals.
# pylint: disable=redefined-outer-name,unused-argument,protected-access
"""Summary uses the normal chat stream and session persistence, offline."""

import asyncio
import json
from unittest.mock import AsyncMock
from types import SimpleNamespace

import pytest
from agentscope.message import Msg, TextBlock
from agentscope.state import AgentState

from qwenpaw.hooks.session.session_hook import SessionSaveHook
from qwenpaw.runtime.hooks import HookRegistry
from qwenpaw.runtime.runtime import Runtime
from qwenpaw.runtime.slash_command_registry import SlashCommandRegistry
from qwenpaw.selflearn import command, summarizer
from tests.unit.runtime import test_selflearn_summary as summary

config = summary.config
ctx = summary.ctx
episode = summary.episode
result = summary.result
inputs = summary.inputs
proposal = summary.proposal
offline = summary.offline


def message(role, text):
    return Msg(name=role, role=role, content=[TextBlock(text=text)])


def runtime_for(ctx, monkeypatch):
    registry = SlashCommandRegistry()
    registry.register(command.analyze_command_spec())
    hooks = HookRegistry()
    hooks.register(SessionSaveHook())
    ctx.workspace.plugins = SimpleNamespace(
        hook_registry=hooks,
        slash_command_registry=registry,
        modes=[],
    )
    runtime = Runtime(workspace=ctx.workspace, app_services=None)
    monkeypatch.setattr(runtime, "_normalize", lambda request: request)
    monkeypatch.setattr(runtime, "_build_context", lambda request: ctx)
    return runtime


async def test_summary_chat_stream_preserves_history_and_tools(
    ctx,
    config,
    inputs,
    proposal,
    offline,
    monkeypatch,
):
    model = summary.SummaryModel(proposal, repair=3)
    monkeypatch.setattr(
        "qwenpaw.runtime.builder.AgentBuilder.build_model",
        lambda self, cfg: (model, None),
    )
    build = AsyncMock(
        side_effect=AssertionError("must not build writable chat agent"),
    )
    monkeypatch.setattr("qwenpaw.runtime.builder.AgentBuilder.build", build)
    prior = {
        "state": AgentState(
            context=[
                message("user", "EARLIER_CHAT"),
                message("assistant", "EARLIER_REPLY"),
            ],
        ).model_dump(mode="json"),
    }

    async def load(**kwargs):
        kwargs["agent"].load_state_dict(prior)

    ctx.workspace.session.load_session_state.side_effect = load
    ctx.input_msgs = [
        message(
            "user",
            f'/analyze summarize "{inputs[0]}" --targets "{inputs[1]}"',
        ),
    ]
    runtime = runtime_for(ctx, monkeypatch)
    events = [
        ev.model_dump(mode="json") async for ev in runtime.run(ctx.request)
    ]
    assert events[0]["status"] == "created"
    assert events[-1]["status"] == "completed"
    assert (
        sum(
            ev["object"] == "response" and ev["status"] == "completed"
            for ev in events
        )
        == 1
    )
    rendered = json.dumps(events, ensure_ascii=False)
    for text in ("read_case", "read_harness", "第 3/3 次修正", "案例", "任务单"):
        assert text in rendered
    assert any(ev.get("role") == "user" for ev in events)
    assert "EARLIER_CHAT" in str(model.calls[0]["messages"])
    tools = {tool["function"]["name"] for tool in model.calls[0]["tools"]}
    assert tools == {
        "read_case",
        "read_harness",
        "read_file",
        "recall_history",
        "GenerateStructuredOutput",
    }
    build.assert_not_called()
    saved = ctx.workspace.session.save_session_state.call_args.kwargs
    assert saved["session_id"] == ctx.session_id
    state = saved["agent"].data
    text = json.dumps(state, ensure_ascii=False)
    assert "EARLIER_CHAT" in text and "EARLIER_REPLY" in text
    assert "第 3/3 次修正" in text and "read_harness" in text
    assert "improvement_proposals.json" in text
    status = json.loads(
        (ctx.workspace_dir / "selflearn/status.json").read_text(),
    )
    output = summary.Path(status["output"])
    assert (
        json.loads(output.read_text())["run"]["chat_session_id"]
        == ctx.session_id
    )
    debug_input = json.loads((output.parent / "input.json").read_text())
    assert debug_input["session_before"] == prior
    assert (
        "system_prompt" in debug_input
        and "signal_strength" not in debug_input["task"]
    )
    assert ctx.agent._context_manager._history.closed


@pytest.mark.parametrize("fail", [False, True])
async def test_chat_cancel_and_failure_preserve_debug_history(
    ctx,
    inputs,
    proposal,
    offline,
    monkeypatch,
    fail,
):
    entered = asyncio.Event()

    class InterruptedModel(summary.SummaryModel):
        async def __call__(self, **kwargs):
            if self.calls:
                entered.set()
                if fail:
                    raise TimeoutError("model disconnected")
                await asyncio.Event().wait()
            return await super().__call__(**kwargs)

    model = InterruptedModel(proposal)
    monkeypatch.setattr(
        "qwenpaw.runtime.builder.AgentBuilder.build_model",
        lambda self, cfg: (model, None),
    )
    ctx.input_msgs = [
        message(
            "user",
            f'/analyze summarize "{inputs[0]}" --targets "{inputs[1]}"',
        ),
    ]
    runtime = runtime_for(ctx, monkeypatch)

    async def consume():
        return [ev async for ev in runtime.run(ctx.request)]

    task = asyncio.create_task(consume())
    await asyncio.wait_for(entered.wait(), 5)
    if fail:
        await task
    else:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    status = json.loads(
        (ctx.workspace_dir / "selflearn/status.json").read_text(),
    )
    assert status["state"] == ("failed" if fail else "stopped")
    output = summary.Path(status["output"])
    assert not output.exists()
    trace = json.loads((output.parent / "trace.json").read_text())
    assert trace["inputs"] and trace["reads"]
    saved = ctx.workspace.session.save_session_state.call_args.kwargs[
        "agent"
    ].data
    assert "read_case" in json.dumps(saved)
    assert ctx.agent._context_manager._history.closed


async def test_plain_command_still_uses_normal_response(ctx, monkeypatch):
    ctx.input_msgs = [message("user", "/analyze")]
    runtime = runtime_for(ctx, monkeypatch)
    events = [
        ev.model_dump(mode="json") async for ev in runtime.run(ctx.request)
    ]
    assert events[-1]["status"] == "completed"
    assert "用法" in json.dumps(events, ensure_ascii=False)
    assert ctx.agent is None


async def test_schema_errors_also_get_three_corrections(
    config,
    inputs,
    proposal,
    monkeypatch,
):
    responses = [None, {}, {"summary": "incomplete"}, proposal.model_dump()]

    async def reply_stream(**kwargs):
        yield Msg(
            name="assistant",
            role="assistant",
            content=[],
            structured_output=responses.pop(0),
        )

    agent = SimpleNamespace(
        reply_stream=reply_stream,
        close=AsyncMock(),
        _system_prompt="Analyze",
    )
    monkeypatch.setattr(
        summarizer,
        "build_readonly_agent",
        lambda *a, **kw: agent,
    )
    cases, coverage = summarizer.load_cases(inputs[0])
    actual = await summarizer.summarize(
        config,
        None,
        cases,
        summarizer.snapshot_harness(inputs[1]),
        coverage,
        inputs[0].parent,
        "Analyze",
    )
    assert actual == proposal and not responses
    agent.close.assert_awaited_once()
