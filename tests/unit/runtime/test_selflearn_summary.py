# -*- coding: utf-8 -*-
# Pytest injects named fixtures, including fixtures used for setup only.
# pylint: disable=redefined-outer-name,unused-argument
"""Second-stage data isolation, grounding, command and native-loop tests."""

import asyncio
import json
import logging
from copy import deepcopy
from pathlib import Path

import pytest
from agentscope.message import ToolCallBlock
from agentscope.model import ChatResponse

from qwenpaw.selflearn import analyzer, command, summarizer
from tests.unit.runtime import test_selflearn_analyze as first

config = first.config
ctx = first.ctx
episode = first.episode
offline = first.offline
result = first.result
store = first.store
finish = first.finish


@pytest.fixture
def inputs(tmp_path, episode, result):
    episodes, rows = [], []
    for pr, review in [(1, 1), (1, 2), (2, 1)]:
        item = deepcopy(episode)
        item.update(
            episode_id=f"repo#{pr}:comment-{review}",
            subject={"repo": "repo", "pr_number": pr},
        )
        episodes.append(item)
        rows.append(
            {
                "episode_id": item["episode_id"],
                **result.model_dump(),
                "run": {"input_hash": analyzer.digest(item)},
            },
        )
    original = store(tmp_path / "episodes.jsonl", episodes)
    batch = tmp_path / "batch"
    batch.mkdir()
    source = store(batch / "episode_analyses.jsonl", rows)
    analyzer.write_json(batch / "manifest.json", {"source": str(original)})
    harness = tmp_path / "harness.md"
    harness.write_text("Trace callers at the reviewed revision.\n")
    targets = tmp_path / "targets.json"
    analyzer.write_json(
        targets,
        {
            "name": "Review",
            "root": ".",
            "targets": [
                {
                    "id": "review.prompt",
                    "kind": "prompt",
                    "path": "harness.md",
                    "scope": "review",
                    "change_mode": "text_only",
                    "allowed_changes": (
                        "Only review instructions, not safety policy"
                    ),
                },
            ],
        },
    )
    return source, targets


@pytest.fixture
def proposal():
    return summarizer.Proposals(
        summary="提出一个需要验证的调查流程改进。",
        proposals=[
            summarizer.Proposal(
                problem="可能遗漏跨文件影响",
                supporting_findings=[
                    summarizer.FindingRef(
                        episode_id=episode_id,
                        finding_index=0,
                    )
                    for episode_id in ["repo#1:comment-1", "repo#1:comment-2"]
                ],
                counter_evidence=[],
                root_cause_hypothesis="已有调用方核查要求，需先确认是否实际落实。",
                primary_target="review.prompt",
                harness_evidence=[
                    summarizer.HarnessEvidence(
                        target_id="review.prompt",
                        quote="Trace callers at the reviewed revision.",
                    ),
                ],
                change_intent="要求记录对删除符号的核查证据。",
                alternatives=["工具返回的版本可能不正确。"],
                verification_plan=["补充历史工具记录，再做等预算重放。"],
                status="needs_evidence",
            ),
        ],
        limitations=["没有历史工具调用记录。"],
    )


def test_strength_is_ignored_and_latest_analysis_wins(inputs):
    source, targets = inputs
    cases, coverage = summarizer.load_cases(source)
    rows = analyzer.read_jsonl(source)
    for row in rows:
        row["findings"][0]["signal_strength"] = "not even a valid label"
    store(source, rows)
    updated, _ = summarizer.load_cases(source)
    assert updated == cases
    assert analyzer.digest(updated) == analyzer.digest(cases)
    assert "signal_strength" not in analyzer.json_text(updated)
    prompt = summarizer.summary_prompt(
        cases,
        summarizer.snapshot_harness(targets),
        coverage,
    )
    assert "signal_strength" not in prompt
    rows.append(deepcopy(rows[0]))
    rows[-1]["summary"] = "Newest analysis"
    store(source, rows)
    updated, coverage = summarizer.load_cases(source)
    assert len(updated) == 3 and coverage["independent_prs"] == 2
    latest = updated[rows[0]["episode_id"]]["analysis"]
    assert latest["summary"] == "Newest analysis"


async def test_tools_cannot_recover_strength(inputs):
    cases, _ = summarizer.load_cases(inputs[0])
    tools = summarizer.summary_tools(
        cases,
        summarizer.snapshot_harness(inputs[1]),
        [],
    )
    with pytest.raises(KeyError):
        await tools[0].call(
            episode_id="repo#1:comment-1",
            pointer="/analysis/findings/0/signal_strength",
        )
    chunk = await tools[0].call(episode_id="repo#1:comment-1")
    assert "signal_strength" not in chunk.content[0].text


def test_positive_contradicted_and_empty_results_are_kept(inputs):
    source, _ = inputs
    rows = analyzer.read_jsonl(source)
    rows[0]["findings"][0].update(stance="support", ai_assessment="correct")
    rows[1]["findings"][0]["feedback_reliability"] = "contradicted"
    rows[2]["findings"] = []
    store(source, rows)
    cases, _ = summarizer.load_cases(source)
    assert len(cases) == 3
    finding = cases[rows[0]["episode_id"]]["analysis"]["findings"][0]
    assert finding["stance"] == "support"
    assert cases[rows[2]["episode_id"]]["analysis"]["findings"] == []


def test_changed_episode_requires_first_stage_rerun(inputs):
    source, _ = inputs
    original = source.parent.parent / "episodes.jsonl"
    episodes = analyzer.read_jsonl(original)
    episodes[0]["agent_output"]["text"] = "Changed"
    store(original, episodes)
    with pytest.raises(ValueError, match="原始案例已变化"):
        summarizer.load_cases(source)


def test_fabricated_first_stage_quote_is_rejected(inputs):
    source, _ = inputs
    rows = analyzer.read_jsonl(source)
    rows[0]["findings"][0]["evidence"][0]["quote"] = "Not in episode"
    store(source, rows)
    with pytest.raises(ValueError, match="第一阶段引用"):
        summarizer.load_cases(source)


async def test_harness_snapshot_is_frozen_and_allowlisted(inputs):
    source, targets = inputs
    snapshot = summarizer.snapshot_harness(targets)
    (targets.parent / "harness.md").write_text("Changed after snapshot")
    cases, _ = summarizer.load_cases(source)
    tool = summarizer.summary_tools(cases, snapshot, [])[1]
    chunk = await tool.call(target_id="review.prompt")
    assert "Trace callers" in chunk.content[0].text
    with pytest.raises(KeyError):
        await tool.call(target_id="unlisted.file")


def test_target_path_escape_is_rejected(inputs):
    targets = inputs[1]
    spec = json.loads(targets.read_text())
    spec["targets"][0]["path"] = "../outside.md"
    analyzer.write_json(targets, spec)
    with pytest.raises(ValueError, match="root 内"):
        summarizer.snapshot_harness(targets)


def test_missing_file_requires_explicit_add_file(inputs):
    targets = inputs[1]
    spec = json.loads(targets.read_text())
    spec["targets"][0]["path"] = "new_skill.md"
    analyzer.write_json(targets, spec)
    with pytest.raises(ValueError, match="找不到 Harness"):
        summarizer.snapshot_harness(targets)
    spec["targets"][0]["change_mode"] = "add_file"
    analyzer.write_json(targets, spec)
    snapshot = summarizer.snapshot_harness(targets)
    assert not snapshot["targets"]["review.prompt"]["exists"]
    assert not (targets.parent / "new_skill.md").exists()


def test_pr_counts_are_not_review_or_vote_counts(inputs, proposal):
    cases, _ = summarizer.load_cases(inputs[0])
    snapshot = summarizer.snapshot_harness(inputs[1])
    summarizer.validate_proposals(proposal, cases, snapshot)
    row = summarizer.proposal_rows(proposal, cases)[0]
    assert row["independent_pr_count"] == 1
    proposal.proposals[0].supporting_findings.append(
        summarizer.FindingRef(
            episode_id="repo#2:comment-1",
            finding_index=0,
        ),
    )
    row = summarizer.proposal_rows(proposal, cases)[0]
    assert row["independent_pr_count"] == 2


@pytest.mark.parametrize("field", ["target", "quote", "finding", "plan"])
def test_invalid_proposals_are_rejected(inputs, proposal, field):
    cases, _ = summarizer.load_cases(inputs[0])
    snapshot = summarizer.snapshot_harness(inputs[1])
    p = proposal.proposals[0]
    if field == "target":
        p.primary_target = "arbitrary.source"
    elif field == "quote":
        p.harness_evidence[0].quote = "Not in file"
    elif field == "finding":
        p.supporting_findings[0].finding_index = 100
    else:
        p.status, p.verification_plan = "propose", []
    with pytest.raises(ValueError):
        summarizer.validate_proposals(proposal, cases, snapshot)


class SummaryModel(first.OfflineToolModel):
    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            name, value = "read_case", {"episode_id": "repo#1:comment-1"}
        elif len(self.calls) == 2:
            name, value = "read_harness", {"target_id": "review.prompt"}
        else:
            name, value = "GenerateStructuredOutput", self.result.model_dump()
            if 3 <= len(self.calls) < 3 + int(self.repair):
                value["proposals"][0]["primary_target"] = "unlisted"

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
@pytest.mark.parametrize("timing", [False, True])
async def test_native_summary_loop(
    inputs,
    proposal,
    config,
    repair,
    timing,
    monkeypatch,
    caplog,
):
    monkeypatch.setenv("QWENPAW_LLM_TIMING_LOG", "1" if timing else "0")
    caplog.set_level(logging.INFO)
    source, targets = inputs
    cases, coverage = summarizer.load_cases(source)
    snapshot = summarizer.snapshot_harness(targets)
    model = SummaryModel(proposal, repair=repair)
    prompt = summarizer.instructions("SUMMARIZE.md")
    result = await summarizer.summarize(
        config,
        model,
        cases,
        snapshot,
        coverage,
        source.parent,
        prompt,
    )
    assert result == proposal
    trace = json.loads((source.parent / "trace.json").read_text())
    assert [r["tool"] for r in trace["reads"]] == ["read_case", "read_harness"]
    assert len(trace["replies"]) == (2 if repair else 1)
    names = {t["function"]["name"] for t in model.calls[0]["tools"]}
    assert names == {
        "read_case",
        "read_harness",
        "read_file",
        "recall_history",
        "GenerateStructuredOutput",
    }
    entries = [
        json.loads(r.getMessage().removeprefix("llm_timing "))
        for r in caplog.records
        if r.getMessage().startswith("llm_timing ")
    ]
    if timing:
        starts = [e for e in entries if e["layer"] == "tool_start"]
        ends = [e for e in entries if e["layer"] == "tool_end"]
        assert [e["call_id"] for e in starts] == [e["call_id"] for e in ends]
        assert ends[-1]["name"] == "GenerateStructuredOutput"
        assert ends[-1]["outcome"] == "success"
        assert all(e["elapsed_ms"] >= 0 for e in ends)
    else:
        assert entries == []


@pytest.mark.parametrize("failures", [3, 4])
async def test_three_corrections_are_allowed(
    inputs,
    proposal,
    config,
    failures,
):
    source, targets = inputs
    cases, coverage = summarizer.load_cases(source)
    model = SummaryModel(proposal, repair=failures)
    job = summarizer.summarize(
        config,
        model,
        cases,
        summarizer.snapshot_harness(targets),
        coverage,
        source.parent,
        "Summarize",
    )
    if failures == 3:
        assert await job == proposal
    else:
        with pytest.raises(ValueError, match="未开放"):
            await job
    trace = json.loads((source.parent / "trace.json").read_text())
    assert len(trace["replies"]) == len(trace["inputs"]) == 4
    assert "第 3/3 次修正" in str(trace["inputs"][-1])


async def test_command_writes_proposals_and_snapshots(
    ctx,
    offline,
    inputs,
    proposal,
    monkeypatch,
):
    source, targets = inputs

    async def output(*args):
        yield proposal

    monkeypatch.setattr(summarizer, "summary_events", output)
    response = await command.handle_analyze(
        ctx,
        f'summarize "{source}" --targets "{targets}"',
    )
    assert isinstance(response, command.CommandStream)
    messages = [event async for event in response.events]
    assert "第二阶段" in messages[-1].get_text_content()
    status = await finish(ctx)
    assert status["state"] == "completed" and status["proposal_count"] == 1
    output = Path(status["output"])
    data = json.loads(output.read_text())
    assert data["proposals"][0]["independent_pr_count"] == 1
    assert data["coverage"]["analyzed_episodes"] == 3
    assert (output.parent / "harness_snapshot.json").exists()
    manifest = json.loads((output.parent / "manifest.json").read_text())
    assert manifest["instructions"] == summarizer.instructions("SUMMARIZE.md")
    assert "signal_strength" not in (output.parent / "cases.json").read_text()
    assert "signal_strength" in source.read_text()
    status_message = await command.handle_analyze(ctx, "status")
    assert "第二阶段" in status_message.get_text_content()


async def test_summary_can_finish_without_proposals(
    ctx,
    offline,
    inputs,
    monkeypatch,
):
    async def output(*args):
        yield summarizer.Proposals(
            summary="没有可归纳的问题",
            proposals=[],
            limitations=[],
        )

    monkeypatch.setattr(summarizer, "summary_events", output)
    response = await command.handle_analyze(
        ctx,
        f'summarize "{inputs[0]}" --targets "{inputs[1]}"',
    )
    _ = [event async for event in response.events]
    status = await finish(ctx)
    assert status["state"] == "completed" and status["proposal_count"] == 0
    assert json.loads(Path(status["output"]).read_text())["proposals"] == []


async def test_stop_does_not_publish_partial_proposals(
    ctx,
    offline,
    inputs,
    monkeypatch,
):
    entered = asyncio.Event()

    async def pause(*args):
        entered.set()
        await asyncio.Event().wait()
        yield  # Make this a streaming agent stub.

    monkeypatch.setattr(summarizer, "summary_events", pause)
    response = await command.handle_analyze(
        ctx,
        f'summarize "{inputs[0]}" --targets "{inputs[1]}"',
    )

    async def stream(_):
        async for event in response.events:
            yield str(event)

    await ctx.workspace.task_tracker.attach_or_start("chat-test", None, stream)
    await asyncio.wait_for(entered.wait(), 5)
    await command.handle_analyze(ctx, "stop")
    status = await finish(ctx)
    assert status["state"] == "stopped"
    assert not Path(status["output"]).exists()


@pytest.mark.parametrize("state", ["completed", "running"])
async def test_stop_after_summary_does_not_cancel_later_chat(ctx, state):
    root = ctx.workspace_dir / "selflearn"
    root.mkdir()
    summarizer.write_json(
        root / "status.json",
        {
            "state": state,
            "phase": "summarize",
            "run_key": "chat-test",
            "task_started_at": "previous run",
        },
    )
    entered = asyncio.Event()

    async def chat(_):
        entered.set()
        await asyncio.Event().wait()
        yield "ordinary chat"

    tracker = ctx.workspace.task_tracker
    await tracker.attach_or_start("chat-test", None, chat)
    try:
        await asyncio.wait_for(entered.wait(), 5)
        reply = await command.handle_analyze(ctx, "stop")
        assert "没有正在运行的分析" in reply.get_text_content()
        assert await tracker.get_status("chat-test") == "running"
    finally:
        await tracker.request_stop("chat-test")


def test_repository_target_template_is_valid():
    path = Path(__file__).resolve().parents[3] / (
        "scripts/selflearn/harness_targets.json"
    )
    snapshot = summarizer.snapshot_harness(path)
    assert len(snapshot["targets"]) == 6
    assert snapshot["targets"]["review.task_prompt"]["exists"]
    assert not snapshot["targets"]["review.skill"]["exists"]


def test_shared_background_and_stage_instructions():
    root = Path(summarizer.__file__).parent
    background = (root / "BACKGROUND.md").read_text()
    stage = (root / "SUMMARIZE.md").read_text()
    prompt = summarizer.instructions("SUMMARIZE.md")
    assert prompt == background + "\n\n" + stage
    assert analyzer.instructions().startswith(background + "\n\n")
    assert prompt.count("# Self-Learn: Shared Context") == 1
    assert "# Stage: Analyze / Summarize" in prompt
    assert "signal_strength" not in prompt
    steps = [line for line in stage.splitlines() if line[:1].isdigit()]
    assert [line.split(".", 1)[0] for line in steps] == list("12345678")


async def test_harness_ranges_search_and_reread(inputs):
    snapshot = summarizer.snapshot_harness(inputs[1])
    target = snapshot["targets"]["review.prompt"]
    target["content"] = "\n".join(f"line {i}" for i in range(1, 251))
    target["content_hash"] = analyzer.digest(target["content"])
    snapshot["targets"]["review.alias"] = dict(target, id="review.alias")
    reads = []
    tool = summarizer.summary_tools({}, snapshot, reads)[1]
    first = await tool.call(target_id="review.prompt")
    data = json.loads(first.content[0].text)
    assert len(data["lines"]) == 60 and data["next_start_line"] == 61
    duplicate = await tool.call(target_id="review.alias")
    assert "reread=true" in duplicate.content[0].text
    assert reads[-1]["duplicate"]
    repeated = await tool.call(target_id="review.alias", reread=True)
    assert repeated.content[0].text == first.content[0].text
    assert not reads[-1]["duplicate"]
    expanded = await tool.call(
        target_id="review.prompt",
        start_line=61,
        end_line=63,
    )
    assert json.loads(expanded.content[0].text)["lines"] == [
        [61, "line 61"],
        [62, "line 62"],
        [63, "line 63"],
    ]
    searched = await tool.call(
        target_id="review.prompt",
        search="line 125",
        context_lines=1,
    )
    assert json.loads(searched.content[0].text)["lines"] == [
        [124, "line 124"],
        [125, "line 125"],
        [126, "line 126"],
    ]
    # A new run never suppresses an earlier run's reads.
    fresh = summarizer.summary_tools({}, snapshot, [])[1]
    restored = await fresh.call(target_id="review.prompt")
    assert restored.content[0].text == first.content[0].text


def test_search_pages_preserve_overlapping_context():
    text = "one\nhit\nthree\nhit\nfive\nsix"
    start, result = 1, []
    while start is not None:
        data = json.loads(
            summarizer.page(
                text,
                start,
                "hit",
                context_lines=1,
                limit=2,
            ),
        )
        result.extend(data["lines"])
        start = data["next_start_line"]
    assert result == list(map(list, enumerate(text.splitlines()[:5], 1)))
    empty = json.loads(summarizer.page(text, 1, "absent", context_lines=3))
    assert empty["lines"] == [] and empty["next_start_line"] is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"start_line": 0},
        {"start_line": 3, "end_line": 2},
        {"context_lines": -1},
    ],
)
def test_invalid_harness_range_is_rejected(kwargs):
    args = {"value": "a\nb", "start_line": 1, "search": ""} | kwargs
    with pytest.raises(ValueError, match="读取范围"):
        summarizer.page(**args)
