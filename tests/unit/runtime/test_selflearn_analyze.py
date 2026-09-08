# -*- coding: utf-8 -*-
# Pytest injects named fixtures; these tests also inspect agent internals.
# pylint: disable=redefined-outer-name,unused-argument,protected-access
"""Test the command, persistence and real agent/tool loop offline."""

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agentscope.message import ToolCallBlock
from agentscope.model import ChatResponse

from qwenpaw.app.task_tracker import TaskTracker
from qwenpaw.config.config import AgentProfileConfig, ModelSlotConfig
from qwenpaw.runtime.builtin_commands import collect_builtin_command_specs
from qwenpaw.runtime.slash_command_registry import SlashCommandRegistry
from qwenpaw.selflearn import analyzer, command


@pytest.fixture
def episode():
    return {
        "schema_version": 1,
        "episode_id": "repo#1:comment-1",
        "agent_output": {"text": "No callers remain.", "reactions": []},
        "reviewed_code": {"diff": "-import old\n+import new\n"},
        "post_review_events": [
            {"text": "There is still a caller.", "reactions": {"+1": 1}},
        ],
    }


@pytest.fixture
def result():
    return analyzer.Analysis(
        findings=[
            analyzer.Finding(
                ai_claim="No callers remain.",
                relation="indirect",
                stance="reject",
                signal_strength="strong",
                feedback_reliability="unverified",
                ai_assessment="uncertain",
                evidence=[
                    analyzer.Evidence(
                        pointer="/post_review_events/0/text",
                        quote="There is still a caller.",
                    ),
                ],
                reason="反馈明确，但缺少被审查版本的调用方文件。",
                problem="可能遗漏跨文件调用方",
            ),
        ],
        summary="存在具体反驳，尚需核实。",
        missing_evidence=["被审查版本的调用方文件"],
    )


@pytest.fixture
def config(tmp_path):
    return AgentProfileConfig(
        id="test",
        name="Test",
        project_dir=str(tmp_path),
        active_model=ModelSlotConfig(provider_id="test", model="offline"),
    )


@pytest.fixture
def ctx(tmp_path):
    workspace = SimpleNamespace(
        task_tracker=TaskTracker(),
        chat_manager=SimpleNamespace(
            get_chat_id_by_session=AsyncMock(
                return_value="chat-test",
            ),
        ),
        session=SimpleNamespace(
            load_session_state=AsyncMock(),
            save_session_state=AsyncMock(),
        ),
    )
    return SimpleNamespace(
        agent_id="test",
        workspace=workspace,
        workspace_dir=tmp_path,
        request=SimpleNamespace(channel="console", user_id="tester"),
        session_id="session-test",
        session_state=None,
        agent=None,
        extras={},
        mode_state={},
        input_msgs=[],
    )


@pytest.fixture
def offline(monkeypatch, config, result):
    monkeypatch.setattr(
        "qwenpaw.config.config.load_agent_config",
        lambda _: config,
    )
    monkeypatch.setattr(
        "qwenpaw.runtime.builder.AgentBuilder.build_model",
        lambda self, cfg: (SimpleNamespace(model="offline"), None),
    )
    mock = AsyncMock(return_value=result)
    monkeypatch.setattr(analyzer, "analyze_episode", mock)
    return mock


def store(path, episodes):
    path.write_text(
        "".join(json.dumps(e) + "\n" for e in episodes),
        encoding="utf-8",
    )
    return path


async def finish(ctx):
    assert await ctx.workspace.task_tracker.wait_all_done(timeout=5)
    return json.loads(
        (ctx.workspace_dir / "selflearn/status.json").read_text(),
    )


def test_command_is_registered_and_advertised():
    registry = SlashCommandRegistry()
    for spec in collect_builtin_command_specs():
        registry.register(spec)
    spec, args = registry.resolve('/analyze "a b.jsonl" --limit 2')
    assert spec.handler is command.handle_analyze
    assert args == '"a b.jsonl" --limit 2'
    assert ("analyze", spec.help_text) in registry.advertisable_commands()


def test_evidence_validation(episode, result):
    analyzer.validate_evidence(result, episode)
    result.findings[0].evidence[0].quote = "Invented quote"
    with pytest.raises(ValueError, match="Quote is not present"):
        analyzer.validate_evidence(result, episode)


@pytest.mark.parametrize(
    "pointer",
    ["text", "/absent", "/post_review_events/9"],
)
def test_invalid_evidence_pointer(episode, result, pointer):
    result.findings[0].evidence[0].pointer = pointer
    with pytest.raises(ValueError, match="Unknown evidence"):
        analyzer.validate_evidence(result, episode)


def test_all_invalid_evidence_is_reported_with_positions(episode, result):
    result.findings[0].evidence.append(
        analyzer.Evidence(
            pointer="/reviewed_code/diff",
            quote="+import missing",
        ),
    )
    second = result.findings[0].model_copy(deep=True)
    second.evidence = [
        analyzer.Evidence(pointer="/absent", quote="missing source"),
        analyzer.Evidence(pointer="/agent_output/text", quote="made up"),
    ]
    result.findings.append(second)
    before = result.model_dump()
    with pytest.raises(ValueError) as exc:
        analyzer.validate_evidence(result, episode)
    errors = str(exc.value).splitlines()
    assert len(errors) == 3
    for error, location, pointer, quote in zip(
        errors,
        [
            "findings[0].evidence[1]",
            "findings[1].evidence[0]",
            "findings[1].evidence[1]",
        ],
        ["/reviewed_code/diff", "/absent", "/agent_output/text"],
        [
            "+import missing",
            "missing source",
            "made up",
        ],
    ):
        assert location in error and pointer in error
        assert analyzer.json_text(quote) in error
    assert "Unknown evidence pointer" in errors[1]
    assert result.model_dump() == before


@pytest.mark.parametrize(
    "source,quote",
    [
        (
            "+assert alive is False\n+\n+\n+def next_test():",
            "+assert alive is False\n\n\n+def next_test():",
        ),
        ("+start\n+middle\n+end", "+start\n+...\n+end"),
        (
            "`probe_timeout` is independent of `deadline`.",
            "probe_timeout is independent of deadline.",
        ),
        (
            "Suggestion:\n\n```python\ncheck()\n```",
            "Suggestion:\n\ncheck()",
        ),
    ],
)
def test_quote_format_changes_are_not_silently_accepted(
    episode,
    result,
    source,
    quote,
):
    episode["reviewed_code"]["diff"] = source
    result.findings[0].evidence = [
        analyzer.Evidence(
            pointer="/reviewed_code/diff",
            quote=quote,
        ),
    ]
    with pytest.raises(ValueError, match="Quote is not present"):
        analyzer.validate_evidence(result, episode)
    assert episode["reviewed_code"]["diff"] == source
    result.findings[0].evidence[0].quote = source
    analyzer.validate_evidence(result, episode)


def test_instructions_require_exact_quotes_and_reviewer_attribution():
    prompt = analyzer.instructions()
    assert "连续原文" in prompt and "新增空行也有 +" in prompt
    assert "PR 中的代码和测试是被审查材料" in prompt
    assert "不能写“AI 编写了错误测试”或“AI 主动断言了错误行为”" in prompt


def test_pointer_escaping_and_numeric_reaction(episode, result):
    episode["a/b"] = {"~": "quoted"}
    assert analyzer.pointer_value(episode, "/a~1b/~0") == "quoted"
    result.findings[0].evidence = [
        analyzer.Evidence(
            pointer="/post_review_events/0/reactions/+1",
            quote="1",
        ),
    ]
    analyzer.validate_evidence(result, episode)


async def test_long_diff_is_readable_without_mutating_input(episode):
    diff = "\n".join(f"line {n}: old" for n in range(1600))
    episode["reviewed_code"]["diff"] = diff
    packet = json.loads(analyzer.episode_prompt(episode).split("\n", 1)[1])
    assert packet["reviewed_code"]["diff"]["pointer"] == "/reviewed_code/diff"
    assert packet["post_review_events"] == episode["post_review_events"]
    assert episode["reviewed_code"]["diff"] == diff
    reads = []
    tool = analyzer.episode_tools(episode, reads)[0]
    chunk = await tool.call(pointer="/reviewed_code/diff", start_line=201)
    data = json.loads(chunk.content[0].text)
    assert data["total_lines"] == 1600
    assert len(data["lines"]) == 200
    assert data["lines"][0] == [201, "line 200: old"]
    chunk = await tool.call(pointer="/reviewed_code/diff", search="line 1500:")
    assert json.loads(chunk.content[0].text)["lines"] == [
        [1501, "line 1500: old"],
    ]
    assert len(reads) == 2
    assert tool.is_read_only


class OfflineToolModel:
    """Scripted model; QwenPaw/AgentScope execution itself is not mocked."""

    model = "offline"
    context_size = 1000000
    formatter = SimpleNamespace(supported_input_media_types=[])

    def __init__(self, result, repair=False):
        self.result = result
        self.repair = repair
        self.calls = []

    async def count_tokens(self, *args, **kwargs):
        return 100

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            name, value = "read_episode", {"pointer": "/reviewed_code/diff"}
        else:
            name, value = "GenerateStructuredOutput", self.result.model_dump()
            if self.repair and len(self.calls) == 2:
                value["findings"][0]["evidence"][0]["quote"] = "fabricated"

        async def stream():
            yield ChatResponse(
                content=[
                    ToolCallBlock(
                        id=f"call-{len(self.calls)}",
                        name=name,
                        input=json.dumps(value),
                    ),
                ],
                is_last=True,
            )

        return stream()


@pytest.mark.parametrize("repair", [False, True])
async def test_real_qwenpaw_loop(
    episode,
    result,
    config,
    tmp_path,
    monkeypatch,
    repair,
):
    model = OfflineToolModel(result, repair=repair)
    monkeypatch.setattr(
        "qwenpaw.plugins.registry.PluginRegistry.get_stop_handlers",
        lambda **kw: pytest.fail("Production hooks leaked into analyzer"),
    )
    trace_path = tmp_path / "session.json"
    actual = await analyzer.analyze_episode(
        config,
        model,
        analyzer.instructions(),
        episode,
        trace_path,
    )
    assert actual == result
    trace = json.loads(trace_path.read_text())
    assert trace["reads"][0]["pointer"] == "/reviewed_code/diff"
    assert len(trace["replies"]) == (2 if repair else 1)
    assert len(model.calls) == (3 if repair else 2)
    names = {t["function"]["name"] for t in model.calls[0]["tools"]}
    assert names == {
        "read_episode",
        "read_file",
        "recall_history",
        "GenerateStructuredOutput",
    }


@pytest.mark.parametrize("corrected", [True, False])
async def test_quote_repair_reports_all_errors_in_one_attempt(
    episode,
    result,
    config,
    tmp_path,
    corrected,
):
    # PR #6203: missing '+' on blank diff lines and stripped backticks.
    diff = "+assert alive is False\n+\n+\n+def next_test():"
    comment = "`probe_timeout` is independent of `deadline`."
    episode["reviewed_code"]["diff"] = diff
    episode["post_review_events"][0]["text"] = comment
    result.findings[0].evidence = [
        analyzer.Evidence(pointer="/reviewed_code/diff", quote=diff),
        analyzer.Evidence(pointer="/post_review_events/0/text", quote=comment),
    ]
    invalid = result.model_copy(deep=True)
    invalid.findings[0].evidence[0].quote = diff.replace("\n+\n+\n", "\n\n\n")
    invalid.findings[0].evidence[1].quote = comment.replace("`", "")

    class QuoteRepairModel(OfflineToolModel):
        async def __call__(self, **kwargs):
            self.result = (
                invalid if len(self.calls) == 1 or not corrected else result
            )
            return await super().__call__(**kwargs)

    model = QuoteRepairModel(result)
    trace_path = tmp_path / "quote-repair.json"
    run = analyzer.analyze_episode(
        config,
        model,
        analyzer.instructions(),
        episode,
        trace_path,
    )
    if corrected:
        assert await run == result
    else:
        with pytest.raises(ValueError, match="Quote is not present"):
            await run
    assert len(model.calls) == 3
    repair_request = str(model.calls[-1])
    for text in (
        "修正以下全部错误",
        "findings[0].evidence[0]",
        "findings[0].evidence[1]",
        "新增空行也有 +",
        "保留已通过的引用",
    ):
        assert text in repair_request
    trace = json.loads(trace_path.read_text())
    assert len(trace["replies"]) == 2
    assert trace["replies"][0]["structured_output"] == invalid.model_dump()


async def test_each_episode_gets_fresh_state(episode, config, tmp_path):
    model = SimpleNamespace(model="offline", context_size=1000000)
    agents = [
        analyzer.build_analyzer(
            config,
            model,
            "Analyze",
            episode,
            str(i),
            [],
            tmp_path / str(i),
        )
        for i in range(2)
    ]
    assert agents[0].state is not agents[1].state
    assert not agents[0].state.context and not agents[1].state.context
    assert agents[0]._get_stop_handlers() == []
    assert agents[0].state.session_id == "0"
    assert agents[0]._context_manager is not agents[1]._context_manager
    for agent in agents:
        await agent.close()


async def test_real_agent_cancellation_is_not_a_format_retry(
    episode,
    result,
    config,
    tmp_path,
    monkeypatch,
):
    entered = asyncio.Event()
    agents = []
    build = analyzer.build_analyzer

    def capture(*args):
        agent = build(*args)
        agents.append(agent)
        return agent

    monkeypatch.setattr(analyzer, "build_analyzer", capture)

    class PausedModel(OfflineToolModel):
        async def __call__(self, **kwargs):
            self.calls.append(kwargs)
            entered.set()
            await asyncio.Event().wait()

    model = PausedModel(result)
    path = tmp_path / "cancelled.json"
    task = asyncio.create_task(
        analyzer.analyze_episode(
            config,
            model,
            analyzer.instructions(),
            episode,
            path,
        ),
    )
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    assert len(model.calls) == 1
    assert path.exists()
    assert agents[0]._context_manager._history.closed


async def test_concurrent_starts_use_one_task(ctx, offline, episode):
    release = asyncio.Event()

    async def pause(*args):
        await release.wait()
        return offline.return_value

    offline.side_effect = pause
    source = store(ctx.workspace_dir / "cases.jsonl", [episode])
    replies = await asyncio.gather(
        command.handle_analyze(ctx, str(source)),
        command.handle_analyze(ctx, str(source)),
    )
    release.set()
    await finish(ctx)
    assert sum("已启动" in r.get_text_content() for r in replies) == 1
    assert offline.await_count == 1


async def test_start_limit_resume_and_changed_input(ctx, offline, episode):
    second = deepcopy(episode) | {"episode_id": "repo#2:comment-2"}
    source = store(
        ctx.workspace_dir / "cases with spaces.jsonl",
        [episode, second],
    )
    response = await command.handle_analyze(
        ctx,
        '"cases with spaces.jsonl" --limit 1',
    )
    assert "已启动" in response.get_text_content()
    status = await finish(ctx)
    assert status["completed"] == 1 and status["total"] == 1
    response = await command.handle_analyze(ctx, f'"{source}"')
    status = await finish(ctx)
    assert status["completed"] == 1 and status["skipped"] == 1
    rows = analyzer.read_jsonl(ctx.workspace_dir / status["output"])
    assert len(rows) == 2
    assert offline.await_count == 2
    episode["agent_output"]["text"] = "Changed input"
    store(source, [episode, second])
    await command.handle_analyze(ctx, f'"{source}"')
    status = await finish(ctx)
    assert status["completed"] == 1 and status["skipped"] == 1
    assert offline.await_count == 3


async def test_failed_case_is_recorded_and_retried(
    ctx,
    offline,
    episode,
    result,
):
    source = store(ctx.workspace_dir / "cases.jsonl", [episode])
    offline.side_effect = RuntimeError("model failed")
    await command.handle_analyze(ctx, str(source))
    status = await finish(ctx)
    assert status["state"] == "completed_with_errors"
    assert analyzer.read_jsonl(ctx.workspace_dir / status["output"]) == []
    offline.side_effect = None
    offline.return_value = result
    await command.handle_analyze(ctx, str(source))
    status = await finish(ctx)
    assert status["completed"] == 1 and status["skipped"] == 0


async def test_no_feedback_is_a_successful_analysis(ctx, offline, episode):
    offline.return_value = analyzer.Analysis(
        findings=[],
        summary="没有可识别的用户反馈。",
        missing_evidence=[],
    )
    source = store(ctx.workspace_dir / "cases.jsonl", [episode])
    await command.handle_analyze(ctx, str(source))
    status = await finish(ctx)
    row = analyzer.read_jsonl(ctx.workspace_dir / status["output"])[0]
    assert row["findings"] == []
    assert status["completed"] == 1


async def test_stop_and_duplicate_start(ctx, offline, episode):
    entered = asyncio.Event()

    async def pause(*args):
        entered.set()
        await asyncio.Event().wait()

    offline.side_effect = pause
    source = store(ctx.workspace_dir / "cases.jsonl", [episode])
    await command.handle_analyze(ctx, str(source))
    await asyncio.wait_for(entered.wait(), 5)
    status_msg = await command.handle_analyze(ctx, "status")
    assert episode["episode_id"] in status_msg.get_text_content()
    response = await command.handle_analyze(ctx, str(source))
    assert "已有分析" in response.get_text_content()
    response = await command.handle_analyze(ctx, "stop")
    assert "已停止" in response.get_text_content()
    status = await finish(ctx)
    assert status["state"] == "stopped"
    assert status["completed"] == 0


async def test_changed_instructions_start_new_output(
    ctx,
    offline,
    episode,
    monkeypatch,
):
    source = store(ctx.workspace_dir / "cases.jsonl", [episode])
    await command.handle_analyze(ctx, str(source))
    first = await finish(ctx)
    monkeypatch.setattr(analyzer, "instructions", lambda: "New policy")
    await command.handle_analyze(ctx, str(source))
    second = await finish(ctx)
    assert first["output"] != second["output"]
    assert second["completed"] == 1 and second["skipped"] == 0


@pytest.mark.parametrize(
    "args",
    ["", "missing.jsonl", "a --limit 0", "a --bad 2"],
)
async def test_help_and_invalid_arguments(ctx, offline, args):
    response = await command.handle_analyze(ctx, args)
    assert "已启动" not in response.get_text_content()
    assert not await ctx.workspace.task_tracker.has_active_tasks()
    offline.assert_not_called()


async def test_non_console_cannot_read_files(ctx, offline):
    ctx.request.channel = "feishu"
    response = await command.handle_analyze(ctx, "/private/data.jsonl")
    assert "仅支持 QwenPaw Console" in response.get_text_content()
    offline.assert_not_called()


async def test_raw_data_is_rejected(ctx, offline):
    source = store(
        ctx.workspace_dir / "raw.jsonl",
        [{"repo": "repo", "pr": 1}],
    )
    await command.handle_analyze(ctx, str(source))
    status = await finish(ctx)
    assert status["state"] == "failed"
    assert "预处理" in status["error"]
    offline.assert_not_called()
