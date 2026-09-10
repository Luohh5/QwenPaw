"""Paired scoring, lineage, interruption, and promotion regression tests."""

import json
import asyncio
from pathlib import Path

import pytest

from qwenpaw.config.config import AgentProfileConfig, ModelSlotConfig
from qwenpaw.selflearn import qa_evaluate as ev, qa_eval_scoring as scoring
from qwenpaw.selflearn.analyzer import write_json
from qwenpaw.selflearn.qa_command import options
from qwenpaw.selflearn.qa_eval_data import (
    AnswerScore,
    ComparisonNote,
    compare,
    match_rows,
    load_rows,
    random_sample,
    summarize,
    validate_score,
    file_hash,
)


def score(key, number, major=False):
    return AnswerScore(
        case_id=key,
        score=number,
        status="scored",
        failure_cause=None,
        point_assessments=[
            {"point_id": "P1", "status": "met", "reason": "核对要点"}
        ],
        major_error=major,
        major_error_clause="main_path" if major else None,
        major_error_reason="主要路径已证实错误" if major else "",
        deductions=(
            [
                {
                    "answer_quote": "",
                    "reason": "缺少必要细节",
                    "source_ids": ["S1"],
                    "impact": "无法完成全部步骤",
                }
            ]
            if number is not None and number < 4
            else []
        ),
        reason="按证据判分",
        uncertainties=[] if number is not None else ["证据不足"],
    )


@pytest.fixture
def inputs(tmp_path):
    cases = {
        f"c{i}": {
            "case_id": f"c{i}",
            "question": f"Q{i}",
            "topic": f"topic{i % 2}",
            "kind": "fact" if i % 2 else "operation",
        }
        for i in range(4)
    }
    refs = {
        k: {
            **r,
            "required_points": [{"id": "P1", "text": "x"}],
            "sources": [{"id": "S1", "claim": "x"}],
            "reference_version": "dev_reference_v1_2",
        }
        for k, r in cases.items()
    }
    return cases, refs


def answer_rows(cases, prefix):
    return {
        k: {
            **r,
            "new_answer": prefix,
            "status": "success",
            "duration_ms": 1000,
        }
        for k, r in cases.items()
    }


def test_guards_nulls_and_new_major_errors(inputs):
    cases, refs = inputs
    old = {k: score(k, 2).model_dump() for k in cases}
    new = {k: score(k, 4).model_dump() for k in cases}
    answers = answer_rows(cases, "answer")
    assert compare(cases, answers, answers, old, new)["eligible"]
    new["c0"] = score("c0", 1, True).model_dump()
    assert compare(cases, answers, answers, old, new)["delta"] > 0
    assert not compare(cases, answers, answers, old, new)["eligible"]
    new["c0"]["score"] = None
    result = compare(cases, answers, answers, old, new)
    assert result["delta"] is None and not result["eligible"]
    equal = compare(cases, answers, answers, old, old)
    assert equal["delta"] == 0 and not equal["eligible"]


def test_missing_duplicate_and_changed_questions_rejected(tmp_path, inputs):
    cases, _ = inputs
    answers = answer_rows(cases, "answer")
    del answers["c0"]
    with pytest.raises(ValueError, match="缺失"):
        match_rows(cases, answers)
    answers = answer_rows(cases, "answer")
    answers["c0"]["question"] = "changed"
    with pytest.raises(ValueError, match="问题内容"):
        match_rows(cases, answers)
    path = tmp_path / "rows.jsonl"
    path.write_text('{"case_id":"x"}\n{"case_id":"x"}\n')
    with pytest.raises(ValueError, match="重复"):
        load_rows(path)


def test_score_evidence_and_failure_contract(inputs):
    cases, refs = inputs
    answer = answer_rows(cases, "original")["c0"]
    value = score("c0", 2)
    validate_score(value, answer, refs["c0"])
    value.deductions[0].answer_quote = "invented"
    with pytest.raises(ValueError, match="连续原文"):
        validate_score(value, answer, refs["c0"])
    value = score("c0", 0)
    value.status = "run_failed"
    value.failure_cause = "unknown"
    validate_score(value, dict(answer, status="error"), refs["c0"])
    value.failure_cause = "evaluation_infrastructure"
    with pytest.raises(ValueError, match="失败分数"):
        validate_score(value, dict(answer, status="error"), refs["c0"])
    value.score = None
    validate_score(value, dict(answer, status="error"), refs["c0"])


def test_duration_excludes_unknown_and_bad_values(inputs):
    cases, _ = inputs
    answers = answer_rows(cases, "answer")
    answers["c0"]["duration_ms"] = -1
    answers["c1"]["duration_ms"] = None
    answers["c2"]["duration_ms"] = float("nan")
    scores = {k: score(k, 4).model_dump() for k in cases}
    result = summarize(cases, answers, scores)
    assert result["duration_count"] == 1
    assert result["mean_duration_seconds"] == 1
    assert len(random_sample(cases, 42)) == 2


@pytest.mark.asyncio
async def test_independent_paired_scoring_and_adjudication(
    tmp_path, monkeypatch, inputs
):
    cases, refs = inputs
    calls = []

    async def fake_stage(
        config, model, skill, task, schema, tools, folder, validate
    ):
        calls.append((Path(folder), task))
        assert (
            "old_answer" not in task
            and "model" not in task
            and "collection" not in task
        )
        n = 2 if Path(folder).parent.name == "first" else 3
        value = score(task["case_id"], n)
        validate(value)
        return value

    monkeypatch.setattr(scoring, "stage_call", fake_stage)
    answers = {
        "group_1": answer_rows(cases, "base"),
        "group_2": answer_rows(cases, "candidate"),
    }
    final, audit = await scoring.score_pair(
        cases, answers, refs, "rules", "prompt", "skill", None, None, tmp_path
    )
    assert audit["calibrated"]
    assert audit["agreement"]["group_1"]["one_point"] == 2
    second = [(p, t) for p, t in calls if p.parent.name == "second"]
    assert len(second) == 4
    assert all("reviews_to_reconcile" not in t for _, t in second)
    adjudications = [
        (p, t) for p, t in calls if p.parent.name == "adjudication"
    ]
    assert len(adjudications) == 4
    assert all(len(t["reviews_to_reconcile"]) == 2 for _, t in adjudications)
    assert all(
        final["group_1"][k]["double_scored"] for k in audit["random_ids"]
    )


@pytest.fixture
def configured(tmp_path, inputs):
    cases, refs = inputs
    alias = tmp_path / "alias"
    (alias / "eval").mkdir(parents=True)
    (alias / "eval/knowledge_eval.py").write_text("# stub")
    case_path = tmp_path / "cases.jsonl"
    ev.write_rows(case_path, cases)
    ref_dir = tmp_path / "references"
    ref_dir.mkdir()
    ev.write_rows(ref_dir / "references.jsonl", refs)
    (ref_dir / "SCORING_RULES.md").write_text("rules")
    (ref_dir / "SCORING_PROMPT.md").write_text("prompt")
    root = tmp_path / "work"
    ev.initialize(
        root / "eval", alias, "old", "qwen3.8-max", "false", case_path, ref_dir
    )
    addition = tmp_path / "A.txt"
    addition.write_text(
        "'create_time': '2026-09-09', 'question': 'Q', 'answer': 'A'"
    )
    return root, addition


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupt_commit", [False, True])
async def test_full_round_promotes_and_next_round_reuses_baseline(
    tmp_path, monkeypatch, configured, interrupt_commit
):
    root, addition = configured
    config = AgentProfileConfig(
        id="test",
        name="test",
        active_model=ModelSlotConfig(provider_id="test", model="judge"),
    )
    calls = []
    fingerprints = {"old": "old-fingerprint"}

    async def fake_backend(profile, mode, **kw):
        calls.append((mode, kw))
        if mode == "config":
            return {"model": profile["model"], "thinking": profile["thinking"]}
        if mode == "inspect":
            return {"fingerprint": fingerprints[kw["collection"]], "count": 4}
        if mode == "prepare":
            output = Path(kw["output"])
            output.mkdir()
            fingerprints[kw["target"]] = kw["target"]
            result = {
                "base_fingerprint": fingerprints[kw["base"]],
                "candidate_fingerprint": fingerprints[kw["target"]],
                "added_chunks": 1,
            }
            write_json(output / "result.json", result)
            return result
        if mode == "run":
            rows = ev.load_rows(kw["cases"])
            output = Path(kw["output"])
            ev.write_rows(
                output,
                answer_rows(
                    rows, "base" if kw["collection"] == "old" else "candidate"
                ),
            )
            manifest = {
                "execution": {
                    "model": profile["model"],
                    "thinking": profile["thinking"],
                },
                "collection": kw["collection"],
                "collection_fingerprint": fingerprints[kw["collection"]],
                "answers_sha256": file_hash(output),
            }
            write_json(output.with_suffix(".manifest.json"), manifest)
            return manifest
        raise AssertionError(mode)

    async def fake_stage(
        config, model, skill, task, schema, tools, folder, validate
    ):
        if schema is ComparisonNote:
            return ComparisonNote(
                improvement="本轮得分提高",
                regressions="无新增关键错误",
                limitations="仅离线固定题集",
            )
        n = 2 if task["new_answer"] == "base" else 3
        value = score(task["case_id"], n)
        validate(value)
        return value

    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "test-github")
    monkeypatch.setattr(ev, "backend", fake_backend)
    monkeypatch.setattr(ev, "stage_call", fake_stage)
    monkeypatch.setattr(scoring, "stage_call", fake_stage)
    publish = ev.finish_report
    if interrupt_commit:

        def interrupted(*args):
            raise OSError("simulated interruption after pointer update")

        monkeypatch.setattr(ev, "finish_report", interrupted)
    events = [
        json.loads(e)
        async for e in ev.run_evaluate(
            addition, root, config, model=object(), live=True
        )
    ]
    assert any("开始首评" in e.get("current", "") for e in events)
    assert any("完成独立复评" in e.get("current", "") for e in events)
    if interrupt_commit:
        assert events[-1]["state"] == "failed"
        interrupted_folder = Path(events[-1]["run_dir"])
        assert not (interrupted_folder / "REPORT.md").exists()
        assert (root / "eval/baseline.json").exists()
        monkeypatch.setattr(ev, "finish_report", publish)
        events = [
            json.loads(e)
            async for e in ev.run_evaluate(
                addition,
                root,
                config,
                resume=interrupted_folder,
                model=object(),
            )
        ]
    assert events[-1]["state"] == "completed", events[-1]
    baseline_path = root / "eval/baseline.json"
    first = ev.read_json(baseline_path)
    assert first["collection"].startswith("qa_eval_")
    folder = Path(events[-1]["run_dir"])
    assert ev.read_json(folder / "comparison.json")["promoted"]
    assert (folder / "REPORT.md").is_file()
    assert (folder / "A.txt").read_text() == addition.read_text()
    before = len([c for c in calls if c[0] == "run"])
    assert before == 2
    events2 = [
        json.loads(e)
        async for e in ev.run_evaluate(addition, root, config, model=object())
    ]
    assert events2[-1]["state"] == "completed", events2[-1]
    assert len([c for c in calls if c[0] == "run"]) == before + 1
    assert ev.read_json(baseline_path) == first  # equal scores never promote
    assert "test-secret" not in (folder / "manifest.json").read_text()
    calls_before_resume = len(calls)
    resumed = [
        json.loads(e)
        async for e in ev.run_evaluate(
            addition, root, config, resume=folder, model=object()
        )
    ]
    assert resumed[-1]["state"] == "completed"
    assert len(calls) == calls_before_resume + 1  # config read only


def test_cli_options_and_no_overwrite(configured):
    root, source = configured
    result = options(["evaluate", str(source), "--resume", "runs/x"], root)
    assert result["workflow"] == "evaluate"
    assert result["resume"] == root / "runs/x"
    assert result["background"] is False
    assert (
        options(["evaluate", str(source), "--background"], root)["background"]
        is True
    )
    config = ev.read_json(root / "eval/config.json")
    with pytest.raises(ValueError, match="配置已存在"):
        ev.initialize(
            root / "eval",
            config["alias_root"],
            "other",
            config["model"],
            config["thinking"],
            config["cases"],
            config["references"],
        )


@pytest.mark.asyncio
async def test_live_evaluate_stop_cancels_work_without_changing_baseline(
    configured, monkeypatch
):
    root, source = configured
    entered, cancelled = asyncio.Event(), asyncio.Event()
    baseline = root / "eval/baseline.json"
    write_json(baseline, {"collection": "old"})
    original = baseline.read_bytes()
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "test-github")

    async def backend(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(ev, "backend", backend)
    stream = ev.run_evaluate(
        source, root, AgentProfileConfig(id="test", name="Test"), live=True
    )
    assert json.loads(await anext(stream))["current"] == "检查配置"
    await asyncio.wait_for(entered.wait(), 2)
    await stream.aclose()
    assert cancelled.is_set()
    assert baseline.read_bytes() == original
    assert ev.read_json(root / "qa_status.json")["state"] == "stopped"
    with ev.eval_lock(root / "eval"):
        pass  # cancellation released the workspace lock


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_backend_reports_batch_count_before_exit_and_cleans_up(
    configured, monkeypatch, cancel
):
    root, _ = configured
    profile = ev.read_json(root / "eval/config.json")
    cases = ev.load_rows(profile["cases"])
    output = root / "answers.jsonl"
    ev.write_rows(
        output, dict(list(answer_rows(cases, "SECRET_ANSWER").items())[:1])
    )
    updates = []
    progress = asyncio.Event()
    finished = asyncio.Event()

    class Process:
        returncode = None
        terminated = False

        async def communicate(self):
            await finished.wait()
            self.returncode = self.returncode or 0
            return b'{"completed": true}', b""

        def terminate(self):
            self.terminated = True
            self.returncode = -15
            finished.set()

    process = Process()

    async def create(*args, **kwargs):
        return process

    def sink(event):
        updates.append(json.loads(event))
        progress.set()

    monkeypatch.setattr(ev.asyncio, "create_subprocess_exec", create)
    token = ev.PROGRESS.set(sink)
    try:
        task = asyncio.create_task(
            ev.backend(
                profile,
                "run",
                cases=profile["cases"],
                output=output,
            )
        )
        await asyncio.wait_for(progress.wait(), 2)
        assert not task.done()
        assert updates[0]["detail"] == "已生成 1/4 条回答。"
        assert "SECRET_ANSWER" not in json.dumps(updates)
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert process.terminated
        else:
            finished.set()
            assert await task == {"completed": True}
            assert not process.terminated
    finally:
        ev.PROGRESS.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("omit_nulls", [False, True])
async def test_real_qwenpaw_score_agent_loop_and_cache(
    tmp_path, inputs, omit_nulls
):
    from tests.unit.runtime.test_selflearn_qa import ScriptedModel

    cases, refs = inputs
    key = "c0"
    cases, refs = {key: cases[key]}, {key: refs[key]}
    config = AgentProfileConfig(
        id="test",
        name="Test",
        active_model=ModelSlotConfig(provider_id="test", model="offline-test"),
    )
    model = ScriptedModel(
        [
            (
                "GenerateStructuredOutput",
                score(key, 4).model_dump(exclude_none=omit_nulls),
            )
            for _ in range(4)
        ]
    )
    answers = {
        "group_1": answer_rows(cases, "a"),
        "group_2": answer_rows(cases, "b"),
    }
    final, audit = await scoring.score_pair(
        cases,
        answers,
        refs,
        "rules",
        "prompt",
        ev.read_skills()["qa-answer-score"],
        config,
        model,
        tmp_path,
    )
    assert model.calls == 4 and audit["calibrated"]
    for rows in final.values():
        assert rows[key]["failure_cause"] is None
        assert rows[key]["major_error_clause"] is None
    await scoring.score_pair(
        cases,
        answers,
        refs,
        "rules",
        "prompt",
        ev.read_skills()["qa-answer-score"],
        config,
        model,
        tmp_path,
    )
    assert model.calls == 4
    first_result = next((tmp_path / "group_1/first").glob("*/result.json"))
    first_result.write_text(
        first_result.read_text().replace("按证据判分", "edited")
    )
    with pytest.raises(ValueError, match="评分缓存"):
        await scoring.score_pair(
            cases,
            answers,
            refs,
            "rules",
            "prompt",
            "skill",
            config,
            model,
            tmp_path,
        )


@pytest.mark.parametrize(
    "status,cause,number",
    [
        ("scored", None, 4),
        ("needs_review", None, None),
        ("run_failed", "evaluation_infrastructure", None),
        ("run_failed", "bot_execution", 0),
    ],
)
def test_provider_schema_allows_valid_score(status, cause, number):
    from jsonschema import validate
    from qwenpaw.providers.openai_chat_model_compat import (
        _sanitize_nullable_tool_schemas,
    )

    value = dict(
        case_id="c0",
        status=status,
        point_assessments=[],
        major_error=False,
        major_error_reason="",
        deductions=[],
        reason="核对证据",
        uncertainties=["证据不足"] if status == "needs_review" else [],
    )
    if cause is not None:
        value["failure_cause"] = cause
    if number is not None:
        value["score"] = number
    tools = [
        {
            "type": "function",
            "function": {
                "name": "GenerateStructuredOutput",
                "parameters": AnswerScore.model_json_schema(),
            },
        }
    ]
    schema = _sanitize_nullable_tool_schemas(tools)[0]["function"][
        "parameters"
    ]
    validate(value, schema)
    result = AnswerScore.model_validate(value)
    validate_score(
        result,
        {
            "new_answer": "answer",
            "status": "success" if status != "run_failed" else "error",
        },
        {"case_id": "c0"},
    )
    assert result.major_error_clause is None
    assert result.failure_cause == cause
    assert result.score == number


@pytest.mark.parametrize(
    "field,match",
    [
        ("score", "status=scored"),
        ("failure_cause", "运行失败必须记录原因"),
        ("major_error_clause", "关键错误须有证据"),
    ],
)
def test_optional_wire_fields_keep_semantic_requirements(inputs, field, match):
    cases, refs = inputs
    value = score("c0", 0).model_dump()
    answer = answer_rows(cases, "original")["c0"]
    if field == "failure_cause":
        value["status"] = "run_failed"
        answer["status"] = "error"
    if field == "major_error_clause":
        value["major_error"] = True
        value["major_error_reason"] = "错误主路径"
        value["deductions"][0]["answer_quote"] = "original"
    value.pop(field)
    with pytest.raises(ValueError, match=match):
        validate_score(AnswerScore.model_validate(value), answer, refs["c0"])


@pytest.mark.parametrize(
    "field,value",
    [
        ("failure_cause", "unknown"),
        ("major_error_clause", "core_conclusion"),
    ],
)
def test_inconsistent_score_fields_are_rejected_with_repair_hint(
    inputs, field, value
):
    cases, refs = inputs
    result = score("c0", 4)
    setattr(result, field, value)
    with pytest.raises(ValueError, match=f"省略 {field}"):
        validate_score(result, answer_rows(cases, "answer")["c0"], refs["c0"])
