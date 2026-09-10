"""QA workflow contracts, real agent/tool loop, provenance and export."""

import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from agentscope.message import ToolCallBlock
from agentscope.model import ChatResponse

from qwenpaw.config.config import AgentProfileConfig, ModelSlotConfig
from qwenpaw.selflearn import qa_command, qa_pipeline
from qwenpaw.selflearn.analyzer import digest
from qwenpaw.selflearn.qa_data import (
    FAQDraft,
    FeedbackLink,
    HistoryEvidence,
    KnowledgePlan,
    KnowledgeTask,
    QAAnalysis,
    export_text,
    load_episodes,
    task_record,
    validate_analysis,
    validate_draft,
    validate_plan,
)
from qwenpaw.selflearn.qa_sources import ResearchSources
from tests.unit.runtime.test_selflearn_analyze import ctx as _ctx

ctx = _ctx


@pytest.fixture
def rows():
    return {
        "a": {
            "record_id": "a",
            "session_id": None,
            "input": {"question": "如何配置？", "messages": []},
            "answer": "设置 X=1",
            "trace": [{"type": "tool_result", "content": "X=1 仅限版本 v1"}],
            "extra": {"untouched": True},
        },
        "b": {
            "record_id": "b",
            "session_id": None,
            "input": {"question": "你说的 X=1 没用，我是 v2"},
            "answer": "",
            "trace": [],
            "feedback": {"text": "没用"},
        },
    }


def analysis():
    return QAAnalysis(
        topic="配置",
        summary="需要确认版本",
        answer_quality="uncertain",
        feedback_links=[],
        findings=[],
        missing_evidence=["实际版本"],
    )


def write_rows(path, rows):
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
        encoding="utf-8",
    )


def test_input_preserves_extras_deduplicates_and_rejects_conflicts(
    tmp_path, rows
):
    path = tmp_path / "input.jsonl"
    write_rows(path, [rows["a"], rows["a"], rows["b"]])
    actual, duplicates = load_episodes(path)
    assert actual == rows and duplicates == 1
    write_rows(path, [rows["a"], dict(rows["a"], answer="different")])
    with pytest.raises(ValueError, match="重复但内容不同"):
        load_episodes(path)
    write_rows(path, [{"input": {"question": "Q"}, "trace": []}])
    first, _ = load_episodes(path)
    assert next(iter(first)).startswith("derived-")
    assert load_episodes(path)[0] == first


def test_offline_feedback_inference_requires_both_ends_not_session_id(rows):
    result = analysis()
    result.feedback_links = [
        FeedbackLink(
            target_record_id="a",
            relation="unresolved",
            confidence="medium",
            reason="具体引用旧答案，但缺少会话 ID",
            evidence=[
                HistoryEvidence(
                    record_id="b", pointer="/input/question", quote="X=1 没用"
                ),
                HistoryEvidence(record_id="a", pointer="/answer", quote="X=1"),
            ],
        )
    ]
    validate_analysis(result, "b", rows)
    result.feedback_links[0].evidence.pop()
    with pytest.raises(ValueError, match="两端|当前记录和目标"):
        validate_analysis(result, "b", rows)
    result.feedback_links[0].evidence[0].quote = "invented"
    with pytest.raises(ValueError, match="连续原文"):
        validate_analysis(result, "b", rows)


def test_repeated_original_request_id_keeps_distinct_turns(tmp_path):
    path = tmp_path / "episodes.jsonl"
    records = [
        {
            "request_id": "shared",
            "message_id": "m1",
            "input": {"question": "Q1"},
            "answer": "A1",
            "trace": [],
        },
        {
            "request_id": "shared",
            "message_id": "m2",
            "input": {"question": "Q2"},
            "answer": "A2",
            "trace": [],
        },
    ]
    write_rows(path, [*records, records[0]])
    parsed, duplicates = load_episodes(path)
    assert list(parsed.values()) == records and duplicates == 1
    assert len(parsed) == 2
    write_rows(path, list(reversed(records)))
    assert set(load_episodes(path)[0]) == set(parsed)


def test_counts_are_records_not_users_and_plan_references_are_checked(rows):
    task = KnowledgeTask(
        question="配置方法",
        problem="版本区别",
        kind="knowledge_gap",
        action="research",
        reason="可查证",
        related_record_ids=["a", "a", "b"],
        supporting_findings=[],
        research_questions=["v2 如何配置"],
    )
    row = task_record(task, rows)
    assert row["occurrence_count"] == 2
    assert row["known_session_count"] == 0
    assert row["unknown_session_records"] == 2
    plan = KnowledgePlan(summary="配置", tasks=[task], limitations=[])
    validate_plan(plan, {k: analysis().model_dump() for k in rows})
    task.related_record_ids.append("missing")
    with pytest.raises(ValueError, match="未成功分析"):
        validate_plan(plan, {k: analysis().model_dump() for k in rows})


@pytest.fixture
def source_store(tmp_path):
    repo = tmp_path / "repo"
    docs = repo / "website/public/docs"
    docs.mkdir(parents=True)
    (docs / "setup.zh.md").write_text(
        "# 配置\nv2 使用 X=2\n", encoding="utf-8"
    )
    sources = ResearchSources(repo, offline=True)
    sources.files["website/public/docs/setup.zh.md"]["url"] = (
        "https://github.com/agentscope-ai/QwenPaw/blob/abc/"
        "website/public/docs/setup.zh.md"
    )
    return sources


def draft_for(sources):
    sid = sources.read_local("website/public/docs/setup.zh.md")["source_id"]
    return FAQDraft(
        status="ready",
        question="v2 如何配置？",
        answer="设置 X=2。",
        applicability="v2",
        evidence=[{"source_id": sid, "quote": "v2 使用 X=2"}],
        limitations=[],
    )


def test_source_capture_is_frozen_and_paths_are_not_arbitrary(source_store):
    captured = source_store.read_local("website/public/docs/setup.zh.md")
    (source_store.repo / "website/public/docs/setup.zh.md").write_text(
        "changed"
    )
    assert (
        source_store.read_local("website/public/docs/setup.zh.md") == captured
    )
    with pytest.raises(ValueError, match="只允许读取"):
        source_store.read_local("../../secret")
    assert "v2 使用" in source_store.loaded[captured["source_id"]]["text"]


@pytest.mark.parametrize(
    "url",
    [
        "http://qwenpaw.agentscope.io/docs",
        "https://evil.test/a",
        "https://github.com/attacker/repo/blob/main/x",
        "https://qwenpaw.agentscope.io:1234/",
        "https://a:b@qwenpaw.agentscope.io/docs",
        "https://raw.githubusercontent.com/agentscope-ai/"
        "QwenPaw/../../private",
    ],
)
def test_network_scope(url):
    with pytest.raises(ValueError):
        ResearchSources.check_url(url)


async def test_offline_does_not_make_http_requests(source_store):
    with pytest.raises(ValueError, match="offline"):
        await source_store.fetch(
            "https://qwenpaw.agentscope.io/docs/quickstart"
        )


def test_export_checks_citations_and_preserves_importer_boundaries(
    source_store,
):
    draft = draft_for(source_store)
    text, count = export_text(
        [(draft, source_store.loaded), (draft, source_store.loaded)],
        "2026-09-08",
    )
    assert count == 1
    assert text.startswith("'create_time': '2026-09-08', 'question': '")
    assert "\n适用范围：v2\n参考：https://github.com/" in text
    draft.evidence[0].quote = "v2 使用 X=3"
    with pytest.raises(ValueError, match="引用不匹配"):
        validate_draft(draft, source_store.loaded)
    draft = draft_for(source_store)
    draft.answer += " 'create_time': '2026-09-08'"
    with pytest.raises(ValueError, match="记录头"):
        export_text([(draft, source_store.loaded)], "2026-09-08")


class ScriptedModel:
    """Only the remote model is replaced; real QwenPaw agents execute tools."""

    model = "offline-test"
    context_size = 1000000
    formatter = SimpleNamespace(supported_input_media_types=[])

    def __init__(self, steps):
        self.steps = iter(steps)
        self.calls = 0

    async def count_tokens(self, *args, **kwargs):
        return 100

    async def __call__(self, **kwargs):
        name, value = next(self.steps)
        self.calls += 1

        async def stream():
            response = ChatResponse(
                content=[
                    ToolCallBlock(
                        id=f"call-{self.calls}",
                        name=name,
                        input=json.dumps(value),
                    )
                ],
                is_last=True,
            )
            # SDK emits tool-start events from deltas, then consumes the
            # final accumulated response to execute the tool.
            yield ChatResponse(content=response.content, is_last=False)
            yield response

        return stream()


async def test_whole_pipeline_real_agent_loop_then_resume(
    tmp_path, rows, source_store, monkeypatch
):
    path = tmp_path / "episodes.jsonl"
    write_rows(path, [rows["a"]])
    config = AgentProfileConfig(
        id="test",
        name="Test",
        active_model=ModelSlotConfig(provider_id="test", model="offline-test"),
    )
    draft = draft_for(source_store)
    sid = draft.evidence[0].source_id
    source_store.loaded = {}
    monkeypatch.setattr(
        qa_pipeline, "ResearchSources", lambda *a: source_store
    )
    plan = KnowledgePlan(
        summary="版本问题",
        tasks=[
            KnowledgeTask(
                question="v2 如何配置？",
                problem="需区分版本",
                kind="knowledge_gap",
                action="research",
                reason="明确需求",
                related_record_ids=["a"],
                supporting_findings=[],
                research_questions=["v2 配置是什么"],
            )
        ],
        limitations=[],
    )
    model = ScriptedModel(
        [
            ("read_history", {"record_id": "a", "pointer": "/trace"}),
            ("GenerateStructuredOutput", analysis().model_dump()),
            ("list_analyses", {}),
            ("GenerateStructuredOutput", plan.model_dump()),
            ("read_source", {"path": "website/public/docs/setup.zh.md"}),
            ("GenerateStructuredOutput", draft.model_dump()),
            ("read_captured_source", {"source_id": sid}),
            (
                "GenerateStructuredOutput",
                {
                    "decision": "accept",
                    "reason": "源码支持版本限定",
                    "evidence": [e.model_dump() for e in draft.evidence],
                },
            ),
        ]
    )
    root = tmp_path / "runs"
    events = [
        json.loads(e)
        async for e in qa_pipeline.run_qa(
            path, root, config, model=model, offline=True
        )
    ]
    assert events[-1]["state"] == "completed", events[-1]
    assert events[-1]["exported"] == 1
    txt = Path(events[-1]["output"]).read_text()
    assert "X=2" in txt
    assert model.calls == 8
    second = [
        json.loads(e)
        async for e in qa_pipeline.run_qa(
            path, root, config, model=model, offline=True
        )
    ]
    assert second[-1]["state"] == "completed"
    assert second[-1]["reused"] == 1 and model.calls == 8
    assert Path(second[-1]["output"]).read_text() == txt


async def test_analysis_failure_is_retried_not_counted_as_no_feedback(
    tmp_path, rows, monkeypatch
):
    path = tmp_path / "episodes.jsonl"
    write_rows(path, list(rows.values()))
    config = AgentProfileConfig(id="test", name="Test")
    failed = True

    async def run_stage(
        config, model, skill, task, schema, tools, folder, validate, **kwargs
    ):
        if schema is QAAnalysis:
            if task["record_id"] == "a" and failed:
                raise RuntimeError("model unavailable")
            return analysis()
        if schema is KnowledgePlan:
            return KnowledgePlan(summary="无新增", tasks=[], limitations=[])
        pytest.fail("No research expected")

    monkeypatch.setattr(qa_pipeline, "stage_call", run_stage)
    root = tmp_path / "runs"
    first = [
        json.loads(e)
        async for e in qa_pipeline.run_qa(
            path, root, config, model=object(), offline=True
        )
    ]
    assert (
        first[-1]["failed"] == 1
        and first[-1]["state"] == "completed_with_errors"
    )
    failed = False
    second = [
        json.loads(e)
        async for e in qa_pipeline.run_qa(
            path, root, config, model=object(), offline=True
        )
    ]
    assert second[-1]["completed"] == 1 and second[-1]["reused"] == 1
    assert second[-1]["state"] == "completed"


async def test_cancel_preserves_status_and_no_final_export(
    tmp_path, rows, monkeypatch
):
    path = tmp_path / "episodes.jsonl"
    write_rows(path, list(rows.values()))

    async def cancelled(*args, **kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(qa_pipeline, "stage_call", cancelled)
    with pytest.raises(asyncio.CancelledError):
        async for _ in qa_pipeline.run_qa(
            path,
            tmp_path / "runs",
            AgentProfileConfig(id="test", name="Test"),
            model=object(),
            offline=True,
        ):
            pass
    assert (
        json.loads((tmp_path / "runs/qa_status.json").read_text())["state"]
        == "stopped"
    )
    assert not list((tmp_path / "runs").rglob("knowledge_additions.txt"))


def test_export_never_overwrites_existing_input(tmp_path):
    path = tmp_path / "knowledge.txt"
    path.write_text("keep original")
    with pytest.raises(ValueError, match="已经存在"):
        qa_pipeline.save_export(path, "replacement")
    assert path.read_text() == "keep original"


async def test_console_command_boundaries_and_parser(ctx, tmp_path):
    ctx.request.channel = "dingtalk"
    reply = await qa_command.handle_selflearn(ctx, 'qa "/secret.jsonl"')
    assert "仅支持" in reply.get_text_content()
    ctx.request.channel = "console"
    assert (
        "尚未运行"
        in (
            await qa_command.handle_selflearn(ctx, "status")
        ).get_text_content()
    )
    path = tmp_path / "episodes.jsonl"
    path.write_text("")
    args = qa_command.options(
        ["qa", str(path), "--limit", "1", "--offline"], tmp_path
    )
    assert args["source"] == path and args["offline"]
    with pytest.raises(ValueError, match="正整数"):
        qa_command.options(["qa", str(path), "--limit", "0"], tmp_path)


async def test_redirect_cannot_leave_official_hosts(monkeypatch):
    import httpx

    calls = []
    real_client = httpx.AsyncClient

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://evil.test/"})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real_client(transport=transport, **kw),
    )
    with pytest.raises(ValueError, match="仅允许"):
        await ResearchSources().fetch("https://qwenpaw.agentscope.io/docs/a")
    assert calls == ["https://qwenpaw.agentscope.io/docs/a"]


def test_pinned_source_link_matches_git_blob_even_in_selflearn_dir(tmp_path):
    repo = tmp_path / "selflearn"
    docs = repo / "website/public/docs"
    docs.mkdir(parents=True)
    file = docs / "setup.zh.md"
    file.write_text("verified content\n")

    def git(*args):
        return subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "commit.gpgsign=false",
                *args,
            ],
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()

    git("init")
    git("add", ".")
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@localhost",
        "commit",
        "-m",
        "fixture",
    )
    git("remote", "add", "origin", "https://github.com/example/QwenPaw.git")
    sources = ResearchSources(repo, offline=True)
    path = "website/public/docs/setup.zh.md"
    assert sources.files[path]["url"].startswith(
        "https://github.com/example/QwenPaw/blob/" + git("rev-parse", "HEAD")
    )
    file.write_text("uncommitted new content\n")
    assert ResearchSources(repo, offline=True).files[path]["url"] is None


async def test_failed_verification_never_exports_candidate(
    tmp_path, rows, source_store, monkeypatch
):
    path = tmp_path / "input.jsonl"
    write_rows(path, [rows["a"]])
    draft = draft_for(source_store)
    monkeypatch.setattr(
        qa_pipeline, "ResearchSources", lambda *a: source_store
    )

    async def stage(
        config, model, skill, task, schema, tools, folder, validate
    ):
        if schema is QAAnalysis:
            return analysis()
        if schema is KnowledgePlan:
            return KnowledgePlan(
                summary="研究",
                tasks=[
                    KnowledgeTask(
                        question="配置",
                        problem="缺口",
                        kind="knowledge_gap",
                        action="research",
                        reason="核实",
                        related_record_ids=["a"],
                        supporting_findings=[],
                        research_questions=["配置方式"],
                    )
                ],
                limitations=[],
            )
        if schema is FAQDraft:
            source_store.read_local("website/public/docs/setup.zh.md")
            return draft
        from qwenpaw.selflearn.qa_data import FAQReview

        review = FAQReview(
            decision="accept", reason="未经回读就接受", evidence=draft.evidence
        )
        validate(review)
        return review

    monkeypatch.setattr(qa_pipeline, "stage_call", stage)
    events = [
        json.loads(e)
        async for e in qa_pipeline.run_qa(
            path,
            tmp_path / "runs",
            AgentProfileConfig(id="test", name="Test"),
            model=object(),
            offline=True,
        )
    ]
    assert events[-1]["state"] == "completed_with_errors"
    assert events[-1]["exported"] == 0
    assert Path(events[-1]["output"]).read_text() == ""


async def test_two_process_style_runs_cannot_share_status(tmp_path, rows):
    path = tmp_path / "input.jsonl"
    write_rows(path, [rows["a"]])
    root = tmp_path / "runs"
    cfg = AgentProfileConfig(id="test", name="Test")
    first = qa_pipeline.run_qa(path, root, cfg, model=object(), offline=True)
    await anext(first)
    before = (root / "qa_status.json").read_text()
    try:
        second = qa_pipeline.run_qa(
            path, root, cfg, model=object(), offline=True
        )
        with pytest.raises(ValueError, match="已有答疑学习运行"):
            await anext(second)
        assert (root / "qa_status.json").read_text() == before
    finally:
        await first.aclose()
    assert (
        json.loads((root / "qa_status.json").read_text())["state"] == "stopped"
    )


async def test_console_starts_background_job_and_reports_completion(
    ctx, tmp_path, monkeypatch
):
    from qwenpaw.runtime.builtin_commands import collect_builtin_command_specs
    from qwenpaw.runtime.slash_command_registry import SlashCommandRegistry

    path = tmp_path / "episodes.jsonl"
    write_rows(path, [{"input": {"question": "Q"}, "trace": []}])
    cfg = AgentProfileConfig(
        id="test",
        name="Test",
        project_dir=str(tmp_path),
        active_model=ModelSlotConfig(provider_id="test", model="offline"),
    )
    monkeypatch.setattr(
        "qwenpaw.config.config.load_agent_config", lambda _: cfg
    )
    entered, released = asyncio.Event(), asyncio.Event()
    received = []

    async def pipeline(**kwargs):
        received.append(kwargs)
        entered.set()
        await released.wait()
        status = {"state": "completed", "phase": "done", "exported": 0}
        root = kwargs["root"]
        root.mkdir(parents=True, exist_ok=True)
        (root / "qa_status.json").write_text(json.dumps(status))
        yield json.dumps(status)

    monkeypatch.setattr(qa_command, "run_qa", pipeline)
    registry = SlashCommandRegistry()
    for spec in collect_builtin_command_specs():
        registry.register(spec)
    response = await registry.dispatch(
        '/selflearn qa "episodes.jsonl" --background', ctx
    )
    assert "已启动" in response.get_text_content()
    await entered.wait()
    try:
        second = await registry.dispatch('/selflearn qa "episodes.jsonl"', ctx)
        assert "运行中" in second.get_text_content()
        assert len(received) == 1 and received[0]["source"] == path
    finally:
        released.set()
    assert await ctx.workspace.task_tracker.wait_all_done(timeout=5)
    status = await registry.dispatch("/selflearn status", ctx)
    assert "completed" in status.get_text_content()


async def test_qa_runs_in_current_chat_without_status_polling(
    ctx, tmp_path, monkeypatch
):
    from qwenpaw.runtime.slash_command_registry import CommandStream

    path = tmp_path / "episodes.jsonl"
    write_rows(path, [{"input": {"question": "Q"}, "trace": []}])
    cfg = AgentProfileConfig(
        id="test",
        name="Test",
        project_dir=str(tmp_path),
        active_model=ModelSlotConfig(provider_id="test", model="offline"),
    )
    monkeypatch.setattr(
        "qwenpaw.config.config.load_agent_config", lambda _: cfg
    )

    async def pipeline(**kwargs):
        yield json.dumps(
            {
                "state": "running",
                "phase": "analyze",
                "current": "Q",
                "completed": 0,
            }
        )
        yield json.dumps(
            {"state": "completed", "phase": "done", "exported": 0}
        )

    monkeypatch.setattr(qa_command, "run_qa", pipeline)
    from unittest.mock import AsyncMock

    recorder = SimpleNamespace(observe=AsyncMock())
    monkeypatch.setattr(
        qa_command, "_make_chat_recorder", AsyncMock(return_value=recorder)
    )
    response = await qa_command.handle_selflearn(ctx, 'qa "episodes.jsonl"')
    try:
        assert isinstance(response, CommandStream)
        messages = [event async for event in response.events]
        assert any("Q" in event.get_text_content() for event in messages)
    finally:
        await ctx.workspace.task_tracker.wait_all_done(timeout=2)


@pytest.mark.parametrize("concurrency", [1, 3])
async def test_independent_history_analysis_overlaps(
    tmp_path, rows, source_store, monkeypatch, concurrency
):
    path = tmp_path / "episodes.jsonl"
    write_rows(path, [dict(rows["a"], record_id=f"row-{i}") for i in range(6)])
    monkeypatch.setattr(
        qa_pipeline, "ResearchSources", lambda *args: source_store
    )
    active = peak = 0

    async def stage(
        config, model, skill, task, schema, tools, folder, validate
    ):
        nonlocal active, peak
        if schema is not QAAnalysis:
            raise ValueError("measurement finished")
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.025)
        active -= 1
        return analysis()

    monkeypatch.setattr(qa_pipeline, "stage_call", stage)
    events = [
        json.loads(e)
        async for e in qa_pipeline.run_qa(
            path,
            tmp_path / "work",
            AgentProfileConfig(id="test", name="Test"),
            model=object(),
            offline=True,
            concurrency=concurrency,
        )
    ]
    assert events[-1]["completed"] == 6
    assert peak == concurrency


async def test_live_stage_reports_real_tools_and_validation(rows, tmp_path):
    cfg = AgentProfileConfig(
        id="test",
        name="Test",
        active_model=ModelSlotConfig(provider_id="test", model="offline-test"),
    )
    model = ScriptedModel(
        [
            ("read_history", {"record_id": "a", "pointer": "/trace"}),
            ("GenerateStructuredOutput", analysis().model_dump()),
        ]
    )
    updates = []
    token = qa_pipeline.PROGRESS.set(updates.append)
    try:
        await qa_pipeline.stage_call(
            cfg,
            model,
            "Analyze",
            {"record_id": "a"},
            QAAnalysis,
            qa_pipeline.history_tools(rows),
            tmp_path / "stage",
            lambda r: validate_analysis(r, "a", rows),
        )
    finally:
        qa_pipeline.PROGRESS.reset(token)
    events = [json.loads(e) for e in updates]
    assert any(
        e["activity"] == "调用工具" and e["detail"] == "read_history"
        for e in events
    ), events
    assert any(
        e["activity"] == "工具完成" and e["detail"] == "read_history"
        for e in events
    )
    assert events[-1]["activity"] == "完成"
    assert (
        json.loads((tmp_path / "stage/trace.json").read_text())[
            "elapsed_seconds"
        ]
        >= 0
    )


async def test_chat_progress_is_saved_in_session(ctx, monkeypatch):
    from agentscope.message import Msg, TextBlock
    from qwenpaw.runtime.builder import AgentBuilder
    from qwenpaw.hooks.session.session_hook import SessionSaveHook

    cfg = AgentProfileConfig(
        id="test",
        name="Test",
        active_model=ModelSlotConfig(provider_id="test", model="offline-test"),
    )
    monkeypatch.setattr(
        AgentBuilder, "build_model", lambda *args: (ScriptedModel([]), None)
    )
    recorder = await qa_command._make_chat_recorder(ctx, cfg)
    try:
        await recorder.observe(
            Msg(
                name="assistant",
                role="assistant",
                content=[TextBlock(text="已完成第 1 条分析")],
            )
        )
        await SessionSaveHook().run(ctx)
        state = ctx.workspace.session.save_session_state.call_args.kwargs[
            "agent"
        ].state_dict()
        assert "已完成第 1 条分析" in json.dumps(state, ensure_ascii=False)
    finally:
        await recorder.close()


async def test_history_tool_object_quotes_match_validator(rows):
    from qwenpaw.selflearn.qa_data import validate_history

    tool = next(
        t for t in qa_pipeline.history_tools(rows) if t.name == "read_history"
    )
    result = await tool(record_id="a", pointer="/trace/0")
    page = json.loads(result.content[0].text)
    quote = page["lines"][0][1]
    validate_history(
        [HistoryEvidence(record_id="a", pointer="/trace/0", quote=quote)],
        rows,
    )


async def test_closing_foreground_cancels_parallel_workers(
    ctx, tmp_path, rows, source_store, monkeypatch
):
    from unittest.mock import AsyncMock

    path = tmp_path / "episodes.jsonl"
    write_rows(path, [dict(rows["a"], record_id=f"row-{i}") for i in range(6)])
    monkeypatch.setattr(
        qa_pipeline, "ResearchSources", lambda *args: source_store
    )
    monkeypatch.setattr(
        qa_command,
        "_make_chat_recorder",
        AsyncMock(return_value=SimpleNamespace(observe=AsyncMock())),
    )
    entered = asyncio.Event()
    active = cancelled = 0

    async def stage(*args):
        nonlocal active, cancelled
        active += 1
        if active == 3:
            entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled += 1
            active -= 1

    monkeypatch.setattr(qa_pipeline, "stage_call", stage)
    root = tmp_path / "work"
    cfg = AgentProfileConfig(id="test", name="Test")

    async def stream(_):
        async for event in qa_pipeline.run_qa(
            path, root, cfg, model=object(), offline=True, live=True
        ):
            yield f"data: {event}\n\n"

    tracker = ctx.workspace.task_tracker
    queue, _ = await tracker.attach_or_start(
        qa_command.RUN_KEY, None, stream, owner=ctx.workspace
    )
    chat = qa_command._qa_in_chat(ctx, cfg, tracker, queue)
    await anext(chat)
    await asyncio.wait_for(entered.wait(), 2)
    await chat.aclose()
    assert active == 0 and cancelled == 3
    assert await tracker.get_status(qa_command.RUN_KEY) != "running"
    assert (
        json.loads((root / "qa_status.json").read_text())["state"] == "stopped"
    )


@pytest.mark.parametrize("workflow", ["qa", "evaluate"])
async def test_runtime_streams_and_saves_qa_progress(
    ctx, tmp_path, monkeypatch, workflow
):
    from agentscope.message import Msg, TextBlock
    from agentscope.state import AgentState
    from qwenpaw.hooks.session.session_hook import SessionSaveHook
    from qwenpaw.runtime.hooks import HookRegistry
    from qwenpaw.runtime.runtime import Runtime
    from qwenpaw.runtime.slash_command_registry import SlashCommandRegistry

    path = tmp_path / ("episodes.jsonl" if workflow == "qa" else "A.txt")
    write_rows(path, [{"input": {"question": "Q"}, "trace": []}])
    cfg = AgentProfileConfig(
        id="test",
        name="Test",
        project_dir=str(tmp_path),
        active_model=ModelSlotConfig(provider_id="test", model="offline"),
    )
    monkeypatch.setattr(
        "qwenpaw.config.config.load_agent_config", lambda _: cfg
    )
    monkeypatch.setattr(
        "qwenpaw.runtime.builder.AgentBuilder.build_model",
        lambda *args: (ScriptedModel([]), None),
    )

    async def pipeline(**kwargs):
        assert kwargs["live"] is True
        yield json.dumps(
            {
                "event": "activity",
                "activity": "调用工具",
                "label": "问题Q",
                "detail": "read_history",
                "elapsed_seconds": 1,
            }
        )
        yield json.dumps(
            {
                "state": "completed",
                "phase": "done" if workflow == "qa" else "evaluate",
                "exported": 1,
                "output": "/example/additions.txt",
            }
        )

    monkeypatch.setattr(qa_command, "run_qa", pipeline)
    monkeypatch.setattr("qwenpaw.selflearn.qa_evaluate.run_evaluate", pipeline)
    monkeypatch.setattr(qa_command, "run_workflow", pipeline)
    prior = {
        "state": AgentState(
            context=[
                Msg(
                    name="user",
                    role="user",
                    content=[TextBlock(text="EARLIER_CHAT")],
                )
            ]
        ).model_dump(mode="json")
    }

    async def load(**kwargs):
        kwargs["agent"].load_state_dict(prior)

    ctx.workspace.session.load_session_state.side_effect = load
    ctx.input_msgs = [
        Msg(
            name="user",
            role="user",
            content=[TextBlock(text=f'/selflearn {workflow} "{path}"')],
        )
    ]
    registry = SlashCommandRegistry()
    registry.register(qa_command.selflearn_command_spec())
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
    events = [
        ev.model_dump(mode="json") async for ev in runtime.run(ctx.request)
    ]
    assert events[-1]["status"] == "completed"
    rendered = json.dumps(events, ensure_ascii=False)
    assert "read_history" in rendered and "/example/additions.txt" in rendered
    saved = ctx.workspace.session.save_session_state.call_args.kwargs[
        "agent"
    ].data
    saved_text = json.dumps(saved, ensure_ascii=False)
    for text in (
        "EARLIER_CHAT",
        f"/selflearn {workflow}",
        "read_history",
        "additions.txt",
    ):
        assert text in saved_text
