"""Signal-gated FAQ generation and independent branch recovery."""

import asyncio
import json
from pathlib import Path

import pytest

from qwenpaw.selflearn import qa_compact as c, qa_pipeline as p
from qwenpaw.selflearn.qa_data import FAQDraft, HistoryEvidence, SourceEvidence
from qwenpaw.selflearn.qa_storage import ArtifactStore
from tests.unit.runtime.test_selflearn_workflow import setup as _setup
from tests.unit.runtime.test_selflearn_qa import (
    source_store as _sources,
    write_rows,
)
from tests.unit.runtime.test_selflearn_dual import histories, inputs as _inputs

inputs = _inputs
setup = _setup
source_store = _sources


def topic(key, rows, helpful=True):
    return c.Topic(
        question=f"问题 {key}",
        signal="bad_answer",
        reason="回答不完整",
        knowledge_helpful=helpful,
        evidence=[
            HistoryEvidence(
                record_id=key, pointer="/answer", quote=rows[key]["answer"]
            )
        ],
    )


def test_bounded_previews_preserve_raw_history():
    rows = histories(30)
    rows["r0"]["trace"] = [{"text": "x" * 100000}]
    original = json.dumps(rows)
    groups = c.batches(rows)
    assert sum(map(len, groups)) == 30
    assert all(len(g) <= 10 for g in groups)
    assert len(groups[0]["r0"]["trace_preview"]) == 6000
    assert json.dumps(rows) == original


def test_selection_rejects_missing_coverage_fake_quotes_and_single_frequency():
    rows = histories(2)
    value = c.Selection(reviewed_record_ids=["r0"], topics=[], limitations=[])
    with pytest.raises(ValueError, match="覆盖"):
        c.validate_selection(value, rows, rows)
    value.reviewed_record_ids = list(rows)
    t = topic("r0", rows)
    t.evidence[0].quote = "fabricated"
    value.topics = [t]
    with pytest.raises(ValueError, match="原文"):
        c.validate_selection(value, rows, rows)
    value.topics = [
        topic("r0", rows).model_copy(update={"signal": "frequent"})
    ]
    with pytest.raises(ValueError, match="两条"):
        c.validate_selection(value, rows, rows)


@pytest.mark.parametrize("helpful", [True, False])
async def test_signal_filter_single_generation_and_resume(
    setup, source_store, monkeypatch, tmp_path, helpful
):
    _, config, _, _, _, _ = setup
    rows = histories(3)
    source = tmp_path / "train.jsonl"
    write_rows(source, list(rows.values()))
    monkeypatch.setattr(c, "ResearchSources", lambda *a: source_store)
    calls = []

    async def stage(
        config, model, skill, task, schema, tools, path, validate, **limits
    ):
        calls.append(schema)
        if schema is c.Selection:
            assert len(task["records"]) == 3
            value = c.Selection(
                reviewed_record_ids=list(rows),
                topics=[topic("r0", rows, helpful)],
                limitations=[],
            )
        elif schema is FAQDraft:
            listing = next(t for t in tools if t.name == "list_history")
            history = json.loads((await listing()).content[0].text)
            assert {r["record_id"] for r in history["records"]} == {"r0"}
            read = next(t for t in tools if t.name == "read_source")
            sid = json.loads(
                (await read(path="website/public/docs/setup.zh.md"))
                .content[0]
                .text
            )["source_id"]
            value = FAQDraft(
                status="ready",
                question="配置",
                answer="X=2",
                applicability="v2",
                evidence=[SourceEvidence(source_id=sid, quote="X=2")],
                limitations=[],
            )
        else:
            raise AssertionError("must not create independent review")
        validate(value)
        return value

    monkeypatch.setattr(p, "stage_call", stage)
    out = tmp_path / "A.txt"
    for _ in range(2):
        store = ArtifactStore(
            tmp_path / "state.sqlite", tmp_path / "session.md"
        )
        with store.working():
            result = await c.generate(
                source,
                out,
                config,
                object(),
                store,
                semaphore=asyncio.Semaphore(3),
            )
    assert result["exported"] == int(helpful)
    assert calls == ([c.Selection, FAQDraft] if helpful else [c.Selection])
    assert out.read_text().count("'create_time':") == int(helpful)


async def test_partial_faq_export_retries_only_failed_topic(
    setup, source_store, monkeypatch, tmp_path
):
    _, config, _, _, _, _ = setup
    rows = histories(2)
    source = tmp_path / "train.jsonl"
    write_rows(source, list(rows.values()))
    monkeypatch.setattr(c, "ResearchSources", lambda *a: source_store)
    calls = []
    fail = True

    async def stage(
        config, model, skill, task, schema, tools, path, validate, **limits
    ):
        if schema is c.Selection:
            value = c.Selection(
                reviewed_record_ids=list(rows),
                topics=[topic(k, rows) for k in rows],
                limitations=[],
            )
        else:
            key = task["task"]["evidence"][0]["record_id"]
            calls.append(key)
            if key == "r1" and fail:
                raise TimeoutError("fixture")
            read = next(t for t in tools if t.name == "read_source")
            sid = json.loads(
                (await read(path="website/public/docs/setup.zh.md"))
                .content[0]
                .text
            )["source_id"]
            value = FAQDraft(
                status="ready",
                question=key,
                answer="X=2",
                applicability="v2",
                evidence=[SourceEvidence(source_id=sid, quote="X=2")],
                limitations=[],
            )
        validate(value)
        return value

    monkeypatch.setattr(p, "stage_call", stage)
    out = tmp_path / "A.txt"
    for count in (1, 2):
        store = ArtifactStore(
            tmp_path / "state.sqlite", tmp_path / "session.md"
        )
        with store.working():
            result = await c.generate(
                source,
                out,
                config,
                object(),
                store,
                semaphore=asyncio.Semaphore(2),
            )
        assert result["exported"] == count
        assert result["state"] == (
            "completed_with_errors" if count == 1 else "completed"
        )
        fail = False
    assert calls.count("r0") == 1 and calls.count("r1") == 2
    assert out.read_text().count("'create_time':") == 2


def test_reset_archives_all_batch_state_and_preserves_original(tmp_path):
    import importlib.util

    script = Path(__file__).parents[3] / "scripts/selflearn/reset-qa.py"
    spec = importlib.util.spec_from_file_location("reset_qa_test", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    workspace = tmp_path / "workspace"
    folder = workspace / "analyze/history_jsonl/A"
    folder.mkdir(parents=True)
    source = folder / "A.jsonl"
    original = b'{"original":"unchanged"}\n'
    source.write_bytes(original)
    (folder / "A_train.jsonl").write_text("train")
    state = workspace / "analyze/.state/A-dataset.sqlite"
    state.parent.mkdir(parents=True)
    state.write_bytes(b"checkpoint")
    other = state.with_name("B-dataset.sqlite")
    other.write_bytes(b"other batch")
    session = workspace / "analyze/sessions/A.md"
    session.parent.mkdir(parents=True)
    session.write_text("session")
    preview = module.reset(source)
    assert not preview["apply"] and state.exists()
    result = module.reset(source, apply=True)
    archive = Path(result["archive"])
    assert source.read_bytes() == original
    assert (archive / source.relative_to(workspace)).read_bytes() == original
    assert (
        archive / state.relative_to(workspace)
    ).read_bytes() == b"checkpoint"
    assert not state.exists() and not session.exists()
    assert other.read_bytes() == b"other batch"
    assert list(folder.iterdir()) == [source]
    assert module.reset(source, apply=True)["apply"] is False
    (folder / "round.json").write_text("{}")
    with pytest.raises(ValueError, match="关联记录"):
        module.reset(source, apply=True)


async def test_failed_training_keeps_completed_test_branch(setup, monkeypatch):
    from qwenpaw.selflearn import qa_rounds as rounds, qa_workflow as flow
    from qwenpaw.selflearn.qa_datasets import HistoryGroups

    root, config, _, _, _, _ = setup
    source = root.parent / "input.jsonl"
    write_rows(source, list(histories(5).values()))
    started = asyncio.Event()

    async def stage(*args, **kwargs):
        return HistoryGroups(links=[], limitations=[])

    async def training(*args, **kwargs):
        await asyncio.wait_for(started.wait(), 1)
        raise TimeoutError("training fixture")

    async def benchmark(folder, *args, **kwargs):
        started.set()
        dest = folder / "specialized"
        dest.mkdir()
        (dest / "manifest.json").write_text("{}")
        return dest, {"cases_file": "cases.jsonl"}

    monkeypatch.setattr(p, "stage_call", stage)
    monkeypatch.setattr(c, "generate", training)
    monkeypatch.setattr(rounds, "build_benchmark", benchmark)
    events = [
        json.loads(e)
        async for e in flow.run_workflow(
            root, config, source=source, model=object()
        )
    ]
    assert events[-1]["branches"] == {
        "txt": "failed",
        "benchmark": "completed",
    }
    assert events[-1]["ready_for_evaluation"] is False
    assert events[-1]["output"] is None
