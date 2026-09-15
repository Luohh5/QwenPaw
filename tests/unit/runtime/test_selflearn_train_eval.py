"""Training diagnostics use train rows and never promote a baseline."""

import json
from pathlib import Path
import pytest
from qwenpaw.selflearn import qa_workflow as flow
from qwenpaw.selflearn.qa_command import options
from qwenpaw.selflearn.qa_train_eval import train_evaluate
from tests.unit.runtime.test_selflearn_dual import (
    ready_round as _ready_round,
    setup as _setup,
    source_store as _source_store,
    inputs as _inputs,
)

ready_round = _ready_round
setup = _setup
source_store = _source_store
inputs = _inputs


async def test_training_after_promotion_uses_original_pair_and_is_read_only(
    ready_round,
):
    root, config, profile, txt, calls, stages, _ = ready_round
    await flow.evaluate(root, txt, config, model=object())
    baseline = (root / "baseline.json").read_bytes()
    batch = root.parent / "analyze/history_jsonl/A"
    protected = {
        p: p.read_bytes()
        for p in [
            batch / "round.json",
            batch / "split.json",
            batch / "specialized/manifest.json",
            txt,
        ]
    }
    runs = sum(c[0] == "run" for c in calls)
    result_path = await train_evaluate(root, txt, config, model=object())
    result = flow.read(result_path)
    assert result["suite"] == "train" and result["diagnostic_only"]
    assert not result["promoted"]
    assert result["binding"]["collections"] == {
        "old": "qwenpaw_faq_old",
        "new": "qwenpaw_faq_A",
    }
    assert (
        result["baseline"]["count"] == 8 and result["candidate"]["count"] == 8
    )
    assert sum(c[0] == "run" for c in calls) - runs == 2
    refs = [
        json.loads(line)
        for line in (Path(result["references"]) / "references.jsonl")
        .read_text()
        .splitlines()
    ]
    split = flow.read(batch / "split.json")
    assert {r["history_record_id"] for r in refs} == set(
        split["train"]["record_ids"]
    )
    assert not {r["history_record_id"] for r in refs} & set(
        split["test"]["record_ids"]
    )
    assert all(r["case_id"].startswith("train-") for r in refs)
    assert (root / "baseline.json").read_bytes() == baseline
    assert all(p.read_bytes() == data for p, data in protected.items())
    count = len(stages)
    assert (
        await train_evaluate(root, txt, config, model=object()) == result_path
    )
    assert len(stages) == count
    assert (root / "baseline.json").read_bytes() == baseline
    Path(result["new_score_file"]).write_text("{}")
    with pytest.raises(ValueError, match="产物已改变"):
        await train_evaluate(root, txt, config, model=object())


async def test_explicit_pair_before_dual_and_command_dispatch(ready_round):
    root, config, profile, txt, calls, _, _ = ready_round
    await flow.prepare_collection(root, txt, profile, "A")
    with pytest.raises(ValueError, match="同时指定"):
        await train_evaluate(root, txt, config, model=object())
    before = flow.read(root / "baseline.json")
    events = [
        json.loads(e)
        async for e in flow.run_workflow(
            root,
            config,
            workflow="train_eval",
            source=txt,
            old_collection="qwenpaw_faq_old",
            new_collection="qwenpaw_faq_A",
            model=object(),
        )
    ]
    assert events[-1]["state"] == "completed", events[-1]
    assert flow.read(root / "baseline.json") == before
    assert not (root / "comparisons/A/comparison.json").exists()


def test_train_command_arguments(tmp_path):
    args = options(["train_eval", "A.txt"], tmp_path)
    assert args["workflow"] == "train_eval" and args["concurrency"] == 4
    assert args["source"] == tmp_path / "A.txt"
    for tail in [["--old-collection", "old"], ["--concurrency", "0"]]:
        with pytest.raises(ValueError):
            options(["train_eval", "A.txt", *tail], tmp_path)
