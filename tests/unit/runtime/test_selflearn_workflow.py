"""User-facing layout, scoring resumption, and baseline lineage."""

import asyncio
import json
from pathlib import Path

import pytest
from qwenpaw.config.config import AgentProfileConfig, ModelSlotConfig
from qwenpaw.selflearn import qa_workflow as flow, qa_eval_scoring as scoring
from qwenpaw.selflearn.qa_storage import ArtifactStore, write_json, period_name
from qwenpaw.selflearn.qa_eval_data import file_hash, validate_score
from tests.unit.runtime.test_selflearn_evaluate import (
    inputs as _inputs,
    score,
    answer_rows,
)


from tests.unit.runtime.test_selflearn_qa import (
    rows as _rows,
    source_store as _source_store,
    draft_for,
    analysis,
    write_rows,
    ScriptedModel,
    KnowledgePlan,
    KnowledgeTask,
    qa_pipeline,
)

rows = _rows
source_store = _source_store

inputs = _inputs


@pytest.fixture
def setup(tmp_path, inputs, monkeypatch):
    cases, refs = inputs
    root = tmp_path / "workspace/selflearn"
    root.mkdir(parents=True)
    references = tmp_path / "references"
    references.mkdir()
    flow.legacy.write_rows(references / "references.jsonl", refs)
    for name in ["SCORING_RULES.md", "SCORING_PROMPT.md"]:
        (references / name).write_text("评分规则")
    cases_path = tmp_path / "cases_dev.jsonl"
    flow.legacy.write_rows(cases_path, cases)
    profile = {
        "alias_root": "unused",
        "initial_collection": "qwenpaw_faq_old",
        "model": "test-model",
        "thinking": "false",
        "cases": str(cases_path),
        "references": str(references),
        "timeout": 600,
        "max_iters": 30,
        "seed": 42,
        "repo": None,
    }
    write_json(root / "config.json", profile)
    config = AgentProfileConfig(
        id="test",
        name="Test",
        active_model=ModelSlotConfig(provider_id="test", model="judge"),
    )
    backend_calls = []
    stage_calls = []
    fingerprints = {"qwenpaw_faq_old": "old-fingerprint"}

    async def backend(profile, mode, **kw):
        backend_calls.append((mode, kw))
        if mode == "config":
            return {"model": profile["model"], "thinking": profile["thinking"]}
        if mode == "inspect":
            return {"fingerprint": fingerprints[kw["collection"]]}
        if mode == "prepare":
            out = Path(kw["output"])
            out.mkdir()
            fingerprints[kw["target"]] = kw["target"]
            result = {
                "base": kw["base"],
                "candidate": kw["target"],
                "base_fingerprint": fingerprints[kw["base"]],
                "candidate_fingerprint": fingerprints[kw["target"]],
                "added_chunks": 1,
            }
            write_json(out / "result.json", result)
            return result
        if mode == "run":
            out = Path(kw["output"])
            out.parent.mkdir(parents=True, exist_ok=True)
            flow.legacy.write_rows(
                out,
                answer_rows(
                    cases,
                    "old" if kw["collection"] == "qwenpaw_faq_old" else "new",
                ),
            )
            meta = {
                "collection": kw["collection"],
                "collection_fingerprint": fingerprints[kw["collection"]],
                "execution": {
                    "model": profile["model"],
                    "thinking": profile["thinking"],
                },
                "answers_sha256": file_hash(out),
                "cases_sha256": file_hash(cases_path),
            }
            write_json(out.with_suffix(".manifest.json"), meta)
            return meta
        raise AssertionError(mode)

    async def stage(
        config, model, skill, task, schema, tools, folder, validate
    ):
        stage_calls.append(
            (task["case_id"], task["new_answer"], folder.parent.name)
        )
        result = score(
            task["case_id"], 2 if task["new_answer"] == "old" else 3
        )
        validate(result)
        return result

    monkeypatch.setattr(flow.legacy, "backend", backend)
    monkeypatch.setattr(scoring, "stage_call", stage)
    return root, config, profile, backend_calls, stage_calls, fingerprints


def test_store_restores_only_artifacts_and_single_transcript(tmp_path):
    db = tmp_path / "check.sqlite"
    session = tmp_path / "session.md"
    with ArtifactStore(db, session).working() as work:
        write_json(work / "first/c0/result.json", {"score": 3})
        write_json(work / "first/c0/context/duplicate.json", {"unused": True})
        flow.transcript_event("问题", "实际分析内容")
    assert not work.exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "check.sqlite",
        "session.md",
    ]
    with ArtifactStore(db, session).working() as restored:
        assert flow.read(restored / "first/c0/result.json") == {"score": 3}
        assert not (restored / "first/c0/context").exists()
    assert "实际分析内容" in session.read_text()


def test_date_names_and_unknown_time_rejected(tmp_path):
    p = tmp_path / "history.jsonl"
    p.write_text(
        '{"received_at":"2026-08-20T12:00:00+08:00"}\n'
        '{"received_at":"2026-08-26T12:00:00+08:00"}\n'
    )
    assert period_name(p) == "2026-08-20_2026-08-26"
    assert period_name(p, "2026-08-20_2026-08-26_v2").endswith("_v2")
    with pytest.raises(ValueError):
        period_name(p, "../escape")
    p.write_text("{}\n")
    with pytest.raises(ValueError, match="无法确定"):
        period_name(p)


@pytest.mark.asyncio
async def test_two_rounds_reuse_promoted_baseline_without_rescoring(setup):
    root, config, profile, calls, stages, _ = setup
    a = root.parent / "2026-08-20.txt"
    a.write_text("FAQ one")
    result = await flow._evaluate_single_suite(root, a, config, model=object())
    assert flow.read(result)["promoted"]
    baseline = flow.read(root / "baseline.json")
    assert baseline["collection"] == "qwenpaw_faq_2026-08-20"
    assert len([x for x in calls if x[0] == "run"]) == 2
    assert len([x for x in stages if x[2] == "adjudication"]) == 0
    before = len(stages)
    b = root.parent / "2026-08-21.txt"
    b.write_text("FAQ two")
    result = await flow._evaluate_single_suite(root, b, config, model=object())
    assert not flow.read(result)["promoted"]
    assert flow.read(root / "baseline.json") == baseline
    assert len([x for x in calls if x[0] == "run"]) == 3
    assert len(stages) - before == 6  # 4 first + 2 independent topic reviews
    assert all(p.suffix == ".jsonl" for p in (root.parent / "eval").iterdir())
    for f in (root / "score").iterdir():
        if f.is_dir():
            assert not list(f.rglob("result.meta.json"))
    before = len(stages)
    await flow._evaluate_single_suite(root, b, config, model=object())
    assert len(stages) == before


@pytest.mark.asyncio
async def test_scoring_failure_preserves_other_cases_and_resume(
    setup, monkeypatch
):
    root, config, profile, calls, stages, _ = setup
    source = await flow.publish_answers(root, profile, "qwenpaw_faq_old")
    original = scoring.stage_call
    failures = {"c0"}
    active = 0
    peak = 0

    async def stage(*args):
        nonlocal active, peak
        task = args[3]
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.01)
            if task["case_id"] in failures:
                raise ValueError("暂时失败")
            return await original(*args)
        finally:
            active -= 1

    monkeypatch.setattr(scoring, "stage_call", stage)
    with pytest.raises(ValueError, match="其他结果已保存"):
        await flow.score_answers(
            root,
            source,
            profile["references"],
            config,
            model=object(),
            concurrency=3,
        )
    assert peak == 3
    assert len(stages) == 3
    assert flow.registry(root)["scores"][source.stem]["status"] == "incomplete"
    failures.clear()
    result = await flow.score_answers(
        root,
        source,
        profile["references"],
        config,
        model=object(),
        concurrency=3,
    )
    assert result["status"] == "completed"
    assert len(stages) == 6  # all 4 first scores plus 2 independent reviews
    before = len(stages)
    await flow.score_answers(
        root, source, profile["references"], config, model=object()
    )
    assert len(stages) == before
    (Path(profile["references"]) / "SCORING_RULES.md").write_text("changed")
    with pytest.raises(ValueError, match="不同的回答或评分标准"):
        await flow.score_answers(
            root, source, profile["references"], config, model=object()
        )
    await flow.score_answers(
        root,
        source,
        profile["references"],
        config,
        model=object(),
        name=source.stem + "_v2",
    )
    assert len(flow.registry(root)["scores"]) == 2


def test_markdown_quote_restoration_keeps_literal_evidence(inputs):
    cases, refs = inputs
    result = score("c0", 3)
    result.deductions[0].answer_quote = "请设置 X=1。"
    answer = {"new_answer": "建议：请设置 `X=1`。", "status": "success"}
    validate_score(result, answer, refs["c0"])
    assert result.deductions[0].answer_quote == "请设置 `X=1`。"
    result.deductions[0].answer_quote = "请设置 Y=2。"
    with pytest.raises(ValueError, match="逐字复制"):
        validate_score(result, answer, refs["c0"])


@pytest.mark.asyncio
async def test_failed_promotion_commit_resumes_without_rebuilding(
    setup, monkeypatch
):
    root, config, profile, calls, stages, _ = setup
    source = root.parent / "2026-08-20.txt"
    source.write_text("FAQ")
    original = flow.write_json

    def fail_report(path, value):
        if Path(path).name == "comparison.json":
            raise OSError("interrupt after pointer update")
        original(path, value)

    monkeypatch.setattr(flow, "write_json", fail_report)
    with pytest.raises(OSError):
        await flow._evaluate_single_suite(root, source, config, model=object())
    assert (
        flow.read(root / "baseline.json")["collection"]
        == "qwenpaw_faq_2026-08-20"
    )
    before = len(stages)
    monkeypatch.setattr(flow, "write_json", original)
    path = await flow._evaluate_single_suite(
        root, source, config, model=object()
    )
    assert flow.read(path)["promoted"]
    assert len(stages) == before
    assert len([c for c in calls if c[0] == "prepare"]) == 1


@pytest.mark.asyncio
async def test_lower_score_retains_original_baseline(setup, monkeypatch):
    root, config, profile, calls, stages, _ = setup

    async def stage(
        config, model, skill, task, schema, tools, folder, validate
    ):
        value = score(task["case_id"], 3 if task["new_answer"] == "old" else 2)
        validate(value)
        return value

    monkeypatch.setattr(scoring, "stage_call", stage)
    source = root.parent / "2026-08-20.txt"
    source.write_text("FAQ")
    path = await flow._evaluate_single_suite(
        root, source, config, model=object()
    )
    assert flow.read(path)["delta"] < 0
    assert flow.read(root / "baseline.json")["collection"] == "qwenpaw_faq_old"
    assert len(flow.registry(root)["scores"]) == 2


@pytest.mark.asyncio
async def test_real_agent_scoring_writes_readable_single_session(
    setup, monkeypatch
):
    from tests.unit.runtime.test_selflearn_qa import ScriptedModel
    from qwenpaw.selflearn.qa_pipeline import stage_call

    root, config, profile, calls, stages, _ = setup
    monkeypatch.setattr(scoring, "stage_call", stage_call)
    source = await flow.publish_answers(root, profile, "qwenpaw_faq_old")
    cases = flow.load_rows(source)
    from qwenpaw.selflearn.qa_eval_data import random_sample

    keys = list(cases) + random_sample(cases, 42)
    model = ScriptedModel(
        [
            (
                "GenerateStructuredOutput",
                score(k, 4).model_dump(exclude_none=True),
            )
            for k in keys
        ]
    )
    events = [
        json.loads(e)
        async for e in flow.run_workflow(
            root,
            config,
            workflow="score",
            source=source,
            references=profile["references"],
            model=model,
            concurrency=1,
        )
    ]
    assert events[-1]["state"] == "completed", events[-1]
    assert any(
        e.get("event") == "session" and "**分数**" in e.get("text", "")
        for e in events
    )
    folder = root / "score" / source.stem
    assert "**分数**" in (folder / "session.md").read_text()
    assert sorted(p.name for p in folder.iterdir()) == [
        ".checkpoint.sqlite",
        "REPORT.md",
        "score.json",
        "session.md",
    ]
    assert model.calls == 6


@pytest.mark.asyncio
async def test_named_analysis_real_agent_loop_then_resume(
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
    root = tmp_path / "workspace/selflearn"
    events = [
        json.loads(e)
        async for e in qa_pipeline.stream_progress(
            flow._analyze_training(
                root,
                config=config,
                source=path,
                name="2026-08-20",
                model=model,
                offline=True,
            ),
            live=True,
        )
    ]
    assert events[-1]["state"] == "completed", events[-1]
    assert events[-1]["exported"] == 1
    visible_results = [
        e
        for e in events
        if e.get("event") == "session"
        and e.get("text", "").startswith("### 分析结果 ·")
        and e.get("display") is not False
    ]
    assert len(visible_results) == 1
    assert "需要确认版本" in visible_results[0]["text"]
    assert any(e.get("display") is False for e in events)
    txt = Path(events[-1]["output"]).read_text()
    assert "X=2" in txt
    assert model.calls == 8
    second = [
        json.loads(e)
        async for e in qa_pipeline.stream_progress(
            flow._analyze_training(
                root,
                config=config,
                source=path,
                name="2026-08-20",
                model=model,
                offline=True,
            ),
            live=True,
        )
    ]
    assert second[-1]["state"] == "completed"
    assert second[-1]["reused"] == 1 and model.calls == 8
    assert (
        sum(
            e.get("text", "").startswith("### 分析结果（复用）")
            for e in second
        )
        == 1
    )
    assert Path(second[-1]["output"]).read_text() == txt

    assert sorted(p.name for p in (root.parent / "analyze/txt").iterdir()) == [
        "2026-08-20.txt"
    ]
    transcript = root.parent / "analyze/sessions/2026-08-20.md"
    assert (
        "X=2" in transcript.read_text()
        and "read_source" in transcript.read_text()
    )
    assert not list(root.parent.rglob("input.json"))


async def test_empty_timeout_has_visible_workflow_error(tmp_path, monkeypatch):
    async def failing(*args, **kwargs):
        raise TimeoutError()
        yield

    monkeypatch.setattr(flow, "analyze", failing)
    events = [
        json.loads(e)
        async for e in flow.run_workflow(root=tmp_path, config=None)
    ]
    assert events[-1]["state"] == "failed"
    assert events[-1]["error"]
    assert events[-1]["error_type"] == "TimeoutError"
    from qwenpaw.selflearn.qa_command import format_status

    assert "超时" in format_status(events[-1])
