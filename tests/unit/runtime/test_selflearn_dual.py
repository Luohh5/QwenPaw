"""Real filesystem round, held-out isolation and dual-suite promotion tests."""

import hashlib
import json
from pathlib import Path

import pytest

from qwenpaw.selflearn import qa_workflow as flow, qa_rounds as rounds
from qwenpaw.selflearn import qa_benchmark as bench, qa_pipeline as pipeline
from qwenpaw.selflearn import qa_eval_scoring as scoring, qa_compact as compact
from qwenpaw.selflearn.qa_datasets import (
    grouped_split,
    prepare_split,
    check_split,
    HistoryGroups,
    RecordLink,
)
from qwenpaw.selflearn.qa_data import (
    HistoryEvidence,
    SourceEvidence,
    QAAnalysis,
    KnowledgePlan,
    KnowledgeTask,
    FAQDraft,
    FAQReview,
    load_episodes,
)
from qwenpaw.selflearn.qa_storage import ArtifactStore, write_json
from qwenpaw.selflearn.qa_eval_data import file_hash, load_rows
from tests.unit.runtime.test_selflearn_qa import (
    source_store as _source_store,
    analysis,
    draft_for,
    write_rows,
)
from tests.unit.runtime.test_selflearn_workflow import setup as _setup
from tests.unit.runtime.test_selflearn_evaluate import (
    inputs as _inputs,
    score,
    answer_rows,
)

source_store = _source_store
setup = _setup
inputs = _inputs


def histories(n=10):
    return {
        f"r{i}": {
            "record_id": f"r{i}",
            "input": {"question": f"配置问题{i}"},
            "answer": f"历史答案{i}",
            "trace": [],
        }
        for i in range(n)
    }


def test_grouped_split_reproducible_and_never_separates_context():
    rows = histories()
    rows["r0"]["session_id"] = rows["r1"]["session_id"] = "session"
    rows["r2"]["message_id"] = "m2"
    rows["r3"]["reply_to_message_id"] = "m2"
    rows["r4"]["input"]["question"] = rows["r5"]["input"]["question"]
    rows["r6"]["input"]["messages"] = [
        {"role": "user", "content": rows["r7"]["input"]["question"]}
    ]
    train, test, groups = grouped_split(rows)
    assert len(test) == 2 and len(train) == 8
    for a, b in [("r0", "r1"), ("r2", "r3"), ("r4", "r5"), ("r6", "r7")]:
        assert (a in test) == (b in test)
    assert set(grouped_split(dict(reversed(list(rows.items()))))[1]) == set(
        test
    )
    assert not set(train) & set(test)
    for row in rows.values():
        row["session_id"] = "same"
    with pytest.raises(ValueError, match="两个独立"):
        grouped_split(rows)


async def test_split_layout_preserves_original_and_freezes_membership(
    tmp_path, monkeypatch
):
    root = tmp_path / "workspace/selflearn"
    source = root.parent / "analyze/history_jsonl/A/A.jsonl"
    source.parent.mkdir(parents=True)
    write_rows(source, list(histories().values()))
    original = source.read_bytes()
    calls = []

    async def stage(*args, **kwargs):
        calls.append(1)
        result = HistoryGroups(links=[], limitations=[])
        args[-1](result)
        return result

    monkeypatch.setattr(pipeline, "stage_call", stage)
    for _ in range(2):
        store = ArtifactStore(
            tmp_path / "state.sqlite", tmp_path / "session.md"
        )
        with store.working():
            folder, manifest = await prepare_split(
                root, source, None, None, store, "A"
            )
    assert calls == [1]
    assert source.read_bytes() == original
    assert (folder / "A_train.jsonl").is_file()
    assert (folder / "A_test.jsonl").is_file()
    assert len(manifest["test"]["record_ids"]) == 2
    (folder / "A_test.jsonl").write_text("{}\n")
    with pytest.raises(ValueError, match="改变"):
        check_split(folder)


@pytest.fixture
async def ready_round(setup, source_store, monkeypatch):
    root, config, profile, backend_calls, stages, fingerprints = setup
    raw = histories()
    source = root.parent / "analyze/history_jsonl/A/A.jsonl"
    source.parent.mkdir(parents=True)
    write_rows(source, list(raw.values()))
    monkeypatch.setattr(bench, "ResearchSources", lambda *args: source_store)
    monkeypatch.setattr(
        pipeline, "ResearchSources", lambda *args: source_store
    )
    monkeypatch.setattr(compact, "ResearchSources", lambda *args: source_store)
    trained, seen_test = [], []
    stage_calls = []

    async def stage(
        config, model, skill, task, schema, tools, folder, validate, **limits
    ):
        stage_calls.append(schema.__name__)
        if schema is HistoryGroups:
            value = HistoryGroups(links=[], limitations=[])
        elif schema is bench.TestReference:
            key = task["record_id"]
            seen_test.append(key)
            reader = next(t for t in tools if t.name == "read_source")
            result = await reader(path="website/public/docs/setup.zh.md")
            sid = json.loads(result.content[0].text)["source_id"]
            value = bench.TestReference(
                status="ready",
                question=task["question"],
                question_evidence=[
                    HistoryEvidence(
                        record_id=key,
                        pointer="/input/question",
                        quote=task["question"],
                    )
                ],
                topic="配置",
                kind="fact",
                reference_answer="v2 使用 X=2",
                required_points=[
                    bench.RequiredPoint(
                        id="P1", text="X=2", essential=True, source_ids=[sid]
                    )
                ],
                acceptable_alternatives=[],
                major_errors=[],
                evidence=[SourceEvidence(source_id=sid, quote="v2 使用 X=2")],
                limitations=[],
            )
        elif schema is compact.Selection:
            ids = [r["record_id"] for r in task["records"]]
            trained.extend(ids)
            value = compact.Selection(
                reviewed_record_ids=ids,
                limitations=[],
                topics=[
                    compact.Topic(
                        question="v2 如何配置？",
                        signal="bad_answer",
                        reason="答案不完整",
                        knowledge_helpful=True,
                        evidence=[
                            HistoryEvidence(
                                record_id=ids[0],
                                pointer="/answer",
                                quote=raw[ids[0]]["answer"],
                            )
                        ],
                    )
                ],
            )
        elif schema is FAQDraft:
            reader = next(t for t in tools if t.name == "read_source")
            result = await reader(path="website/public/docs/setup.zh.md")
            sid = json.loads(result.content[0].text)["source_id"]
            value = FAQDraft(
                status="ready",
                question="v2 如何配置？",
                answer="v2 使用 X=2",
                applicability="v2",
                evidence=[SourceEvidence(source_id=sid, quote="v2 使用 X=2")],
                limitations=[],
            )
        else:
            raise AssertionError(schema)
        validate(value)
        return value

    monkeypatch.setattr(pipeline, "stage_call", stage)
    events = [
        json.loads(e)
        async for e in flow.run_workflow(
            root, config, source=source, model=object(), offline=True
        )
    ]
    assert events[-1]["state"] == "completed", events[-1]
    assert len(trained) == 8 and len(seen_test) == 2
    assert not set(trained) & set(seen_test)
    txt = Path(events[-1]["output"])
    assert txt.name == "A.txt" and "X=2" in txt.read_text()
    assert "FAQReview" not in stage_calls
    assert stage_calls.count("Selection") == 1

    # The backend mock honors each supplied cases file, including specialized.
    async def backend(p, mode, **kw):
        backend_calls.append((mode, dict(kw)))
        if mode == "config":
            return {"model": p["model"], "thinking": p["thinking"]}
        if mode == "inspect":
            return {"fingerprint": fingerprints[kw["collection"]]}
        if mode == "prepare":
            out = Path(kw["output"])
            out.mkdir()
            fingerprints[kw["target"]] = kw["target"]
            value = {
                "base": kw["base"],
                "candidate": kw["target"],
                "base_fingerprint": fingerprints[kw["base"]],
                "candidate_fingerprint": fingerprints[kw["target"]],
                "added_chunks": 1,
            }
            write_json(out / "result.json", value)
            return value
        if mode == "run":
            out = Path(kw["output"])
            out.parent.mkdir(parents=True, exist_ok=True)
            cases = load_rows(kw["cases"])
            flow.legacy.write_rows(
                out,
                answer_rows(
                    cases,
                    "old" if kw["collection"] == "qwenpaw_faq_old" else "new",
                ),
            )
            value = {
                "collection": kw["collection"],
                "collection_fingerprint": fingerprints[kw["collection"]],
                "execution": {"model": p["model"], "thinking": p["thinking"]},
                "answers_sha256": file_hash(out),
                "cases_sha256": file_hash(kw["cases"]),
            }
            write_json(out.with_suffix(".manifest.json"), value)
            return value
        raise AssertionError(mode)

    async def same_score(
        config, model, skill, task, schema, tools, folder, validate, **limits
    ):
        stages.append(task["case_id"])
        value = score(task["case_id"], 3)
        # Use the actual per-question evidence IDs.
        for deduction in value.deductions:
            deduction.source_ids = [task["reference"]["sources"][0]["id"]]
        assert not any(
            "snapshot_text" in s for s in task["reference"]["sources"]
        )
        validate(value)
        return value

    monkeypatch.setattr(flow.legacy, "backend", backend)
    monkeypatch.setattr(scoring, "stage_call", same_score)
    return root, config, profile, txt, backend_calls, stages, source_store


async def test_dual_equality_promotes_small_test_and_reuses_completed(
    ready_round,
):
    root, config, profile, txt, calls, stages, _ = ready_round
    # Reuse an already valid generalized baseline: only three new runs remain.
    old = await flow.publish_answers(root, profile, "qwenpaw_faq_old")
    await flow.score_answers(
        root, old, profile["references"], config, model=object()
    )
    initial_runs = sum(c[0] == "run" for c in calls)
    result_path = await flow.evaluate(root, txt, config, model=object())
    result = flow.read(result_path)
    assert result["promoted"]
    assert all(s["delta"] == 0 for s in result["suites"].values())
    assert result["suites"]["specialized"]["baseline"]["count"] == 2
    assert sum(c[0] == "run" for c in calls) - initial_runs == 3
    baseline = flow.read(root / "baseline.json")
    assert baseline["collection"] == "qwenpaw_faq_A"
    assert Path(baseline["answers"]).name.startswith("cases_dev--")
    count = len(stages)
    assert (
        await flow.evaluate(root, txt, config, model=object()) == result_path
    )
    assert len(stages) == count
    assert set(v["suite"] for v in flow.registry(root)["scores"].values()) == {
        "generalized",
        "specialized",
    }


@pytest.mark.parametrize("suite", ["generalized", "specialized"])
async def test_either_suite_regression_retains_baseline(
    ready_round, monkeypatch, suite
):
    root, config, _, txt, _, _, _ = ready_round
    original = scoring.stage_call

    async def stage(*args, **kwargs):
        task = args[3]
        value = await original(*args)
        is_special = task["case_id"].startswith("specialized-")
        if (
            is_special == (suite == "specialized")
            and task["new_answer"] == "new"
        ):
            value.score = 2
        args[-1](value)
        return value

    monkeypatch.setattr(scoring, "stage_call", stage)
    path = await flow.evaluate(root, txt, config, model=object())
    result = flow.read(path)
    assert not result["promoted"] and result["suites"][suite]["delta"] < 0
    assert flow.read(root / "baseline.json")["collection"] == "qwenpaw_faq_old"


async def test_resume_promotion_and_tamper_detection(ready_round, monkeypatch):
    root, config, _, txt, _, stages, _ = ready_round
    original = flow.write_json

    def interrupt(path, value):
        if Path(path).name == "comparison.json":
            raise OSError("after baseline commit")
        original(path, value)

    monkeypatch.setattr(flow, "write_json", interrupt)
    with pytest.raises(OSError):
        await flow.evaluate(root, txt, config, model=object())
    before = len(stages)
    monkeypatch.setattr(flow, "write_json", original)
    path = await flow.evaluate(root, txt, config, model=object())
    assert flow.read(path)["promoted"] and len(stages) == before
    txt.write_text("changed")
    with pytest.raises(ValueError, match="TXT 已改变"):
        await flow.evaluate(root, txt, config, model=object())


async def test_frozen_reference_source_is_read_without_network(
    tmp_path, monkeypatch
):
    text = "version-specific evidence"
    reference = {
        "sources": [
            {
                "id": "S1",
                "snapshot_text": text,
                "file_sha256": hashlib.sha256(text.encode()).hexdigest(),
            }
        ]
    }

    async def forbidden(*args):
        raise AssertionError("must not fetch live sources")

    monkeypatch.setattr(scoring, "fetch_source", forbidden)
    tool = scoring.source_tool(reference, None, tmp_path)
    result = await tool(source_id="S1")
    assert text in result.content[0].text


def test_dual_gate_requires_complete_scores_and_no_new_major_errors():
    ok = {"baseline": {"count": 1}, "delta": 0, "new_major_errors": []}
    for bad in (
        {**ok, "delta": None},
        {**ok, "new_major_errors": ["x"]},
        {**ok, "baseline": {"count": 0}},
    ):
        assert not rounds.dual_decision(
            {"generalized": ok, "specialized": bad}, 1
        )
    assert not rounds.dual_decision({"generalized": ok, "specialized": ok}, 0)


async def test_new_round_uses_promoted_baseline_and_new_specialized_cases(
    ready_round,
):
    root, config, _, txt, calls, stages, _ = ready_round
    await flow.evaluate(root, txt, config, model=object())
    before = sum(c[0] == "run" for c in calls)
    source = root.parent / "analyze/history_jsonl/A/A.jsonl"
    events = [
        json.loads(e)
        async for e in flow.run_workflow(
            root, config, source=source, name="B", model=object(), offline=True
        )
    ]
    assert events[-1]["state"] == "completed", events[-1]
    path = await flow.evaluate(
        root, events[-1]["output"], config, model=object()
    )
    assert flow.read(path)["database"]["base"] == "qwenpaw_faq_A"
    assert flow.read(root / "baseline.json")["collection"] == "qwenpaw_faq_B"
    assert sum(c[0] == "run" for c in calls) - before == 3
    assert any(
        c[0] == "run" and str(c[1]["cases"]).endswith("B_specialized.jsonl")
        for c in calls
    )


async def test_completed_qa_round_reuses_frozen_files_without_model(
    ready_round, monkeypatch
):
    root, config, _, txt, _, _, _ = ready_round
    before = txt.read_bytes()

    async def forbidden(*args, **kwargs):
        raise AssertionError("must not regenerate a completed round")

    monkeypatch.setattr(pipeline, "stage_call", forbidden)
    source = root.parent / "analyze/history_jsonl/A/A.jsonl"
    events = [
        json.loads(e)
        async for e in flow.run_workflow(
            root, config, source=source, model=object()
        )
    ]
    assert events[-1]["state"] == "completed" and events[-1]["exported"] == 1
    assert txt.read_bytes() == before


async def test_real_agent_builds_reference_without_independent_review(
    setup, source_store, monkeypatch, tmp_path
):
    from tests.unit.runtime.test_selflearn_qa import ScriptedModel

    root, config, profile, _, _, _ = setup
    folder = root.parent / "analyze/history_jsonl/C"
    folder.mkdir(parents=True)
    raw = histories(1)["r0"]
    write_rows(folder / "C_test.jsonl", [raw])
    write_json(folder / "split.json", {"version": 1})
    split = {"name": "C", "test": {"file": "C_test.jsonl"}}
    sid = source_store.read_local("website/public/docs/setup.zh.md")[
        "source_id"
    ]
    source_store.loaded = {}
    value = bench.TestReference(
        status="ready",
        question=raw["input"]["question"],
        question_evidence=[
            HistoryEvidence(
                record_id="r0",
                pointer="/input/question",
                quote=raw["input"]["question"],
            )
        ],
        topic="配置",
        kind="fact",
        reference_answer="v2 使用 X=2",
        required_points=[
            bench.RequiredPoint(
                id="P1", text="X=2", essential=True, source_ids=[sid]
            )
        ],
        acceptable_alternatives=[],
        major_errors=[],
        evidence=[SourceEvidence(source_id=sid, quote="v2 使用 X=2")],
        limitations=[],
    )
    model = ScriptedModel(
        [
            ("read_history", {"record_id": "r0"}),
            ("read_source", {"path": "website/public/docs/setup.zh.md"}),
            ("GenerateStructuredOutput", value.model_dump()),
            ("read_captured_source", {"source_id": sid}),
            (
                "GenerateStructuredOutput",
                {
                    "decision": "accept",
                    "reason": "独立核对来源",
                    "evidence": [e.model_dump() for e in value.evidence],
                },
            ),
        ]
    )
    monkeypatch.setattr(bench, "ResearchSources", lambda *a: source_store)
    store = ArtifactStore(tmp_path / "refs.sqlite", tmp_path / "refs.md")
    with store.working():
        dest, manifest = await bench.build_benchmark(
            folder, split, profile, config, model, store, offline=True
        )
    assert model.calls == 3 and manifest["count"] == 1
    refs = load_rows(dest / "references.jsonl")
    assert next(iter(refs.values()))["sources"][0]["snapshot_text"]
    assert bench.check_benchmark(dest)["count"] == 1


async def test_failed_reference_does_not_block_txt(setup, monkeypatch):
    import asyncio

    root, config, _, _, _, _ = setup
    source = root.parent / "history.jsonl"
    write_rows(source, list(histories(5).values()))
    started = asyncio.Event()

    async def stage(*args, **kwargs):
        return HistoryGroups(links=[], limitations=[])

    async def training(source, output, *args, **kwargs):
        started.set()
        pipeline.save_export(output, "FAQ")
        return {"state": "completed", "output": str(output), "exported": 1}

    async def benchmark(*args, **kwargs):
        await asyncio.wait_for(started.wait(), timeout=1)
        raise TimeoutError("test fixture timeout")

    monkeypatch.setattr(pipeline, "stage_call", stage)
    monkeypatch.setattr(compact, "generate", training)
    monkeypatch.setattr(rounds, "build_benchmark", benchmark)
    events = [
        json.loads(e)
        async for e in flow.run_workflow(
            root, config, source=source, model=object()
        )
    ]
    assert events[-1]["state"] == "completed_with_errors"
    assert Path(events[-1]["output"]).read_text() == "FAQ"
    assert events[-1]["branches"] == {
        "txt": "completed",
        "benchmark": "failed",
    }
    with pytest.raises(ValueError, match="尚未完成"):
        rounds.checked_round(root, events[-1]["output"])


def test_split_balances_supplied_topics_without_breaking_groups():
    rows = histories(20)
    for i, row in enumerate(rows.values()):
        row["topic"] = "install" if i < 10 else "tools"
    train, test, _ = grouped_split(rows)
    assert len(train) == 16 and len(test) == 4
    assert sum(rows[k]["topic"] == "install" for k in test) == 2


async def test_grouping_gets_context_once_without_reading_execution_traces(
    tmp_path, monkeypatch
):
    root = tmp_path / "workspace/selflearn"
    source = root.parent / "analyze/history_jsonl/A/A.jsonl"
    source.parent.mkdir(parents=True)
    rows = histories()
    rows["r0"]["trace"] = [{"tool_result": "large execution trace"}]
    rows["r1"]["input"]["messages"] = [
        {"role": "user", "content": rows["r0"]["input"]["question"]}
    ]
    rows["r1"]["feedback"] = {"text": "还是不行", "target": "r0"}
    write_rows(source, list(rows.values()))
    calls = []

    async def stage(
        config, model, skill, task, schema, tools, folder, validate, **limits
    ):
        assert tools == []  # No paginated reads during grouping.
        supplied = {r["record_id"]: r for r in task["records"]}
        assert supplied["r1"]["input"] == rows["r1"]["input"]
        assert supplied["r1"]["feedback"] == rows["r1"]["feedback"]
        assert supplied["r0"]["answer"] == rows["r0"]["answer"]
        assert all("trace" not in r for r in supplied.values())
        calls.append(task)
        return HistoryGroups(links=[], limitations=[])

    monkeypatch.setattr(pipeline, "stage_call", stage)
    store = ArtifactStore(tmp_path / "checkpoint.sqlite", tmp_path / "s.md")
    with store.working():
        folder, manifest = await prepare_split(
            root, source, None, None, store, "A"
        )
    assert len(calls) == 1
    assert any({"r0", "r1"} <= set(g) for g in manifest["groups"])
    original, _ = load_episodes(source)
    assert original["r0"]["trace"] == rows["r0"]["trace"]


async def test_failed_grouping_retries_same_batch_with_real_agent(tmp_path):
    from qwenpaw.config.config import AgentProfileConfig, ModelSlotConfig
    from tests.unit.runtime.test_selflearn_qa import ScriptedModel

    root = tmp_path / "workspace/selflearn"
    source = root.parent / "analyze/history_jsonl/A/A.jsonl"
    source.parent.mkdir(parents=True)
    raw = histories()
    write_rows(source, list(raw.values()))
    checkpoint = tmp_path / "state.sqlite"
    transcript = tmp_path / "session.md"
    store = ArtifactStore(checkpoint, transcript)
    with store.working():
        store.set(
            "split_binding",
            {
                "source": file_hash(source),
                "skill": "old skill",
                "version": 1,
            },
        )
        write_json(
            store.work / "grouping/trace.json",
            {
                "elapsed_seconds": 1200.021,
                "replies": [],
                "corrections": [],
            },
        )
    model = ScriptedModel(
        [
            ("GenerateStructuredOutput", {"links": [], "limitations": []}),
        ]
    )
    config = AgentProfileConfig(
        id="test",
        name="Test",
        active_model=ModelSlotConfig(provider_id="test", model="offline-test"),
    )
    store = ArtifactStore(checkpoint, transcript)
    with store.working():
        _, manifest = await prepare_split(
            root,
            source,
            config,
            model,
            store,
            "A",
        )
        assert store.get("split_binding")["version"] == 2
    assert model.calls == 1
    assert len(manifest["train"]["record_ids"]) == 8
    assert len(manifest["test"]["record_ids"]) == 2


async def test_failed_grouping_cannot_retry_on_changed_source(tmp_path):
    root = tmp_path / "workspace/selflearn"
    source = root.parent / "analyze/history_jsonl/A/A.jsonl"
    source.parent.mkdir(parents=True)
    write_rows(source, list(histories().values()))
    store = ArtifactStore(tmp_path / "state.sqlite", tmp_path / "session.md")
    with store.working():
        store.set(
            "split_binding",
            {
                "source": "different",
                "skill": "old",
                "version": 1,
            },
        )
        with pytest.raises(ValueError, match="新批次名称"):
            await prepare_split(root, source, None, None, store, "A")


@pytest.mark.parametrize("failures", [1, 2])
async def test_grouping_retries_interrupted_stream_once(
    tmp_path, monkeypatch, failures
):
    from qwenpaw.providers.retry_chat_model import StreamIdleTimeoutError

    root = tmp_path / "workspace/selflearn"
    source = root.parent / "analyze/history_jsonl/A/A.jsonl"
    source.parent.mkdir(parents=True)
    write_rows(source, list(histories().values()))
    calls = []

    async def stage(*args, **kwargs):
        calls.append(args[-2])
        if len(calls) <= failures:
            raise TimeoutError("输出中断") from StreamIdleTimeoutError(
                "m", 120
            )
        return HistoryGroups(links=[], limitations=[])

    monkeypatch.setattr(pipeline, "stage_call", stage)
    store = ArtifactStore(tmp_path / "s.sqlite", tmp_path / "s.md")
    with store.working():
        if failures == 2:
            with pytest.raises(TimeoutError):
                await prepare_split(root, source, None, None, store, "A")
            assert store.get("groups") is None
            assert not (source.parent / "A_train.jsonl").exists()
        else:
            await prepare_split(root, source, None, None, store, "A")
            assert (source.parent / "A_train.jsonl").exists()
    assert len(calls) == 2
    assert calls[0] != calls[1]


def test_grouping_stream_timeout_is_scoped_to_new_wrapper():
    from qwenpaw.providers.retry_chat_model import RetryChatModel
    from tests.unit.runtime.test_selflearn_qa import ScriptedModel

    original = RetryChatModel(
        ScriptedModel([]),
        stream_first_content_timeout=30,
        stream_idle_timeout=30,
    )
    scoped = original.with_minimum_stream_timeouts(120)
    assert scoped is not original
    assert original._stream_idle_timeout == 30
    assert scoped._stream_idle_timeout == 120
    assert scoped._stream_first_content_timeout == 120
    disabled = RetryChatModel(
        ScriptedModel([]),
        stream_first_content_timeout=0,
        stream_idle_timeout=600,
    ).with_minimum_stream_timeouts(120)
    assert disabled._stream_first_content_timeout == 0
    assert disabled._stream_idle_timeout == 600


def test_grouping_validation_names_missing_evidence():
    from qwenpaw.selflearn.qa_datasets import validate_groups

    rows = histories(3)
    value = HistoryGroups(
        links=[
            RecordLink(
                record_ids=["r0", "r1", "r2"],
                reason="关联",
                evidence=[
                    HistoryEvidence(
                        record_id=k,
                        pointer="/input/question",
                        quote=rows[k]["input"]["question"],
                    )
                    for k in ("r0", "r1")
                ],
            )
        ],
        limitations=[],
    )
    with pytest.raises(ValueError, match="缺少证据的记录：.*r2"):
        validate_groups(value, rows)


async def test_real_agent_restarts_after_partial_grouping_output(tmp_path):
    from agentscope.message import ToolCallBlock
    from agentscope.model import ChatResponse
    from qwenpaw.config.config import AgentProfileConfig, ModelSlotConfig
    from qwenpaw.providers.retry_chat_model import StreamIdleTimeoutError
    from tests.unit.runtime.test_selflearn_qa import ScriptedModel

    class InterruptedModel(ScriptedModel):
        async def __call__(self, **kwargs):
            if self.calls:
                return await super().__call__(**kwargs)
            self.calls += 1

            async def stream():
                yield ChatResponse(
                    content=[
                        ToolCallBlock(
                            id="partial",
                            name="GenerateStructuredOutput",
                            input='{ "links": [',
                        )
                    ],
                    is_last=False,
                )
                raise StreamIdleTimeoutError("test", 120)

            return stream()

    model = InterruptedModel(
        [
            ("GenerateStructuredOutput", {"links": [], "limitations": []}),
        ]
    )
    root = tmp_path / "workspace/selflearn"
    source = root.parent / "analyze/history_jsonl/A/A.jsonl"
    source.parent.mkdir(parents=True)
    write_rows(source, list(histories().values()))
    config = AgentProfileConfig(
        id="test",
        name="Test",
        active_model=ModelSlotConfig(provider_id="test", model="offline-test"),
    )
    store = ArtifactStore(tmp_path / "s.sqlite", tmp_path / "s.md")
    events = []
    token = pipeline.PROGRESS.set(events.append)
    try:
        with store.working():
            _, manifest = await prepare_split(
                root,
                source,
                config,
                model,
                store,
                "A",
            )
            first = json.loads(
                (store.work / "grouping/trace.json").read_text()
            )
            retry = json.loads(
                (store.work / "grouping/retry-1/trace.json").read_text()
            )
            assert first["exception"]["type"] == "StreamIdleTimeoutError"
            assert not first["tool_progress"][0]["arguments_complete"]
            assert retry["tool_progress"][0]["result_complete"]
    finally:
        pipeline.PROGRESS.reset(token)
    assert model.calls == 2
    assert len(manifest["test"]["record_ids"]) == 2
    assert sum("模型输出中断，正在重试" in e for e in events) == 1


async def test_benchmark_parallel_isolated_and_resumes_only_failed_build(
    setup, source_store, monkeypatch, tmp_path
):
    import asyncio

    root, config, profile, _, _, _ = setup
    folder = root.parent / "analyze/history_jsonl/parallel"
    folder.mkdir(parents=True)
    raw = histories(6)
    for key in raw:
        path = f"website/public/docs/{key}.md"
        source_store.files[path] = {
            "path": path,
            "text": f"{key} 独立来源\nv2 使用 X=2",
            "kind": "local_source",
        }
    write_rows(folder / "parallel_test.jsonl", list(raw.values()))
    write_json(folder / "split.json", {"name": "parallel"})
    split = {"name": "parallel", "test": {"file": "parallel_test.jsonl"}}
    monkeypatch.setattr(bench, "ResearchSources", lambda *a: source_store)
    active = peak = 0
    calls = []
    fail_build = True

    async def stage(
        config, model, skill, task, schema, tools, path, validate, **limits
    ):
        nonlocal active, peak
        key = task["record_id"]
        calls.append((key, task["mode"]))
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.01)
            if task["mode"] == "build":
                assert limits == {
                    "timeout": 420,
                    "max_iters": 12,
                    "max_tool_calls": 20,
                }
                if key == "r1" and fail_build:
                    raise TimeoutError("build stalled")
                assert task["record"]["input"] == raw[key]["input"]
                read_tool = next(t for t in tools if t.name == "read_source")
                result = await read_tool(path=f"website/public/docs/{key}.md")
                sid = json.loads(result.content[0].text)["source_id"]
                value = bench.TestReference(
                    status="ready",
                    question=task["question"],
                    question_evidence=[
                        HistoryEvidence(
                            record_id=key,
                            pointer="/input/question",
                            quote=task["question"],
                        )
                    ],
                    topic="配置",
                    kind="fact",
                    reference_answer="v2 使用 X=2",
                    required_points=[
                        bench.RequiredPoint(
                            id="P1",
                            text="X=2",
                            essential=True,
                            source_ids=[sid],
                        )
                    ],
                    acceptable_alternatives=[],
                    major_errors=[],
                    evidence=[
                        SourceEvidence(source_id=sid, quote="v2 使用 X=2")
                    ],
                    limitations=[],
                )
            else:
                raise AssertionError("no review stage")
            validate(value)
            return value
        finally:
            active -= 1

    monkeypatch.setattr(pipeline, "stage_call", stage)
    db = tmp_path / "p.sqlite"
    transcript = tmp_path / "p.md"
    store = ArtifactStore(db, transcript)
    with store.working():
        with pytest.raises(ValueError, match="执行失败"):
            await bench.build_benchmark(
                folder, split, profile, config, object(), store
            )
    assert source_store.loaded == {}
    assert peak == 3
    assert len([c for c in calls if c[1] == "build"]) == 6
    calls.clear()
    fail_build = False
    store = ArtifactStore(db, transcript)
    with store.working():
        dest, manifest = await bench.build_benchmark(
            folder, split, profile, config, object(), store
        )
    assert calls == [("r1", "build")]
    assert manifest["count"] == 6
    assert [
        r["history_record_id"]
        for r in load_rows(dest / "references.jsonl").values()
    ] == list(raw)
    assert "6/6" in transcript.read_text()

    # Skill-only changes preserve finished references and their provenance.
    skills = tmp_path / "skills/qa-build-specialized-eval"
    skills.mkdir(parents=True)
    skill = (
        pipeline.SKILLS / "qa-build-specialized-eval/SKILL.md"
    ).read_text()
    (skills / "SKILL.md").write_text(skill + "\n生成简洁标准。\n")
    (dest / "manifest.json").unlink()
    calls.clear()
    store = ArtifactStore(db, transcript)
    with store.working():
        _, resumed = await bench.build_benchmark(
            folder,
            split,
            profile,
            config,
            object(),
            store,
            skills_dir=skills.parent,
        )
        assert resumed["binding"] != manifest["binding"]
        assert resumed["reference_bindings"] == manifest["reference_bindings"]
        assert store.get("method_binding:" + manifest["binding"])
    assert calls == []


async def test_benchmark_cancellation_joins_all_workers(
    setup, source_store, monkeypatch, tmp_path
):
    import asyncio

    root, config, profile, _, _, _ = setup
    folder = tmp_path / "cancel"
    folder.mkdir()
    write_rows(folder / "test.jsonl", list(histories(6).values()))
    write_json(folder / "split.json", {"name": "cancel"})
    monkeypatch.setattr(bench, "ResearchSources", lambda *a: source_store)
    started = asyncio.Event()
    active = 0

    async def stage(*args, **kwargs):
        nonlocal active
        active += 1
        if active == 3:
            started.set()
        try:
            await asyncio.Event().wait()
        finally:
            active -= 1

    monkeypatch.setattr(pipeline, "stage_call", stage)
    store = ArtifactStore(tmp_path / "cancel.sqlite", tmp_path / "session.md")
    with store.working():
        task = asyncio.create_task(
            bench.build_benchmark(
                folder,
                {"name": "cancel", "test": {"file": "test.jsonl"}},
                profile,
                config,
                object(),
                store,
            )
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert active == 0
    assert not (folder / "specialized/manifest.json").exists()


@pytest.mark.parametrize("decision", ["accept", "needs_evidence"])
@pytest.mark.parametrize("blocked", [True, False])
async def test_real_agent_can_finish_after_research_budget(
    setup, tmp_path, decision, blocked
):
    from agentscope.tool import FunctionTool
    from tests.unit.runtime.test_selflearn_qa import ScriptedModel

    _, config, _, _, _, _ = setup
    calls = []

    async def read_source(path: str):
        calls.append(path)
        return "原文证据"

    review = {"decision": decision, "reason": "核验", "evidence": []}
    model = ScriptedModel(
        [
            ("read_source", {"path": "a"}),
            *([("read_source", {"path": "b"})] if blocked else []),
            *[("GenerateStructuredOutput", review)] * 3,
        ]
    )
    coroutine = pipeline.stage_call(
        config,
        model,
        "使用已有原文收尾",
        {},
        FAQReview,
        [FunctionTool(read_source, is_read_only=True)],
        tmp_path / decision,
        lambda value: None,
        max_tool_calls=1,
    )
    if decision == "accept" or not blocked:
        value = await coroutine
        assert value.decision == decision
    else:
        with pytest.raises(ValueError, match="不能据此排除测试题"):
            await coroutine
    assert calls == ["a"]
