# -*- coding: utf-8 -*-
# Pytest injects named fixtures, including fixtures used for setup only.
# pylint: disable=redefined-outer-name,unused-argument
"""Real local Git + offline agent loop; no source checkout or model writes."""

import asyncio
import json
from pathlib import Path

import pytest
from agentscope.message import ToolCallBlock
from agentscope.model import ChatResponse

from qwenpaw.selflearn import (
    optimizer,
    optimize_command as command,
    summarizer,
)
from qwenpaw.selflearn.analyzer import digest, write_json
from qwenpaw.selflearn.edit_scope import check_edit, harness_path
from qwenpaw.selflearn.exploration import OptimizationSession
from qwenpaw.selflearn.versions import HarnessStore, read_json
from tests.unit.runtime import test_selflearn_summary as summary

config = summary.config
ctx = summary.ctx
episode = summary.episode
result = summary.result
inputs = summary.inputs
proposal = summary.proposal
offline = summary.offline


@pytest.fixture
def bundle(inputs, proposal):
    source, targets_path = inputs
    spec = read_json(targets_path)
    spec["targets"][0]["edit_rules"] = {"sections": ["*"]}
    write_json(targets_path, spec)
    snapshot = summarizer.snapshot_harness(targets_path)
    cases, coverage = summarizer.load_cases(source)
    proposal.proposals[0].status = "propose"
    proposal.proposals[0].counter_evidence = [
        summarizer.FindingRef(
            episode_id="repo#2:comment-1",
            finding_index=0,
        ),
    ]
    folder = source.parent / "proposal"
    folder.mkdir()
    write_json(folder / "cases.json", cases)
    write_json(folder / "harness_snapshot.json", snapshot)
    output = folder / "improvement_proposals.json"
    write_json(
        output,
        {
            "schema_version": 1,
            **proposal.model_dump(),
            "coverage": coverage,
            "run": {
                "cases_hash": digest(cases),
                "harness_hash": digest(snapshot),
                "targets_source": str(targets_path),
            },
        },
    )
    return output


@pytest.fixture
def candidate(bundle, tmp_path):
    task, snapshot, cases, targets = optimizer.load_task(bundle)
    store = HarnessStore.for_snapshot(tmp_path / "selflearn", snapshot)
    base = store.import_snapshot(snapshot)
    task["snapshot_revision"] = base
    row = store.create(base, task, targets)
    current = store.snapshot(base, snapshot["targets"])
    return store, row, cases, current


@pytest.fixture
def session(candidate):
    store, row, _, current = candidate
    row["task"]["initial_base"] = "auto"
    folder = store.root.parents[1] / "optimization_runs" / "test-run"
    folder.mkdir(parents=True)
    return OptimizationSession(
        store,
        row["task"],
        current,
        row["targets"],
        folder,
        {},
    )


def test_task_projection_and_provenance(bundle, candidate, session):
    _, _, cases, _ = candidate
    prompt = optimizer.optimize_prompt(session)
    assert "signal_strength" not in prompt
    assert "alternatives" not in prompt and "没有历史工具调用记录" in prompt
    assert "content_hash" not in prompt and "-import old" not in prompt
    assert len(cases) == 3
    document = read_json(bundle)
    document["proposals"][0]["status"] = "needs_evidence"
    write_json(bundle, document)
    with pytest.raises(ValueError, match="不会启动"):
        optimizer.load_task(bundle)
    document["proposals"][0]["status"] = "propose"
    document["run"]["cases_hash"] = "stale"
    write_json(bundle, document)
    with pytest.raises(ValueError, match="不匹配"):
        optimizer.load_task(bundle)


def test_edit_scope_preserves_code_and_protected_sections():
    source = (
        'def prompt(repo):\n    return f"Review {repo}."\n'
        'AGENTS = """## Tools\nRead only.\n## Review\nCheck.\n"""\n'
        'LIMIT = int(os.environ.get("LIMIT", "20"))\n'
    )
    target = {
        "path": "harness.py",
        "change_mode": "text_only",
        "edit_rules": {
            "function_returns": ["prompt"],
            "string_sections": {"AGENTS": ["Review"]},
            "env_defaults": ["LIMIT"],
        },
    }
    check_edit(
        target,
        source,
        source.replace("Review {repo}.", "核查 {repo} carefully.")
        .replace("Check.", "Check evidence.")
        .replace('"20"', '"30"'),
    )
    for old, new in [
        ("{repo}", "{repo.upper()}"),
        ("Read only.", "Write freely."),
        ('"20"', '"0"'),
        ('"LIMIT"', '"OTHER"'),
        ("def prompt", "# unrelated\ndef prompt"),
    ]:
        with pytest.raises(ValueError):
            check_edit(target, source, source.replace(old, new))


def test_paths_and_unknown_rules_are_not_editable(tmp_path):
    for relative in ["../outside.md", ".git/config", "/outside.md"]:
        with pytest.raises(ValueError):
            harness_path(tmp_path, relative)
    (tmp_path / "linked.md").symlink_to(tmp_path / "other.md")
    with pytest.raises(ValueError, match="符号链接"):
        harness_path(tmp_path, "linked.md")
    with pytest.raises(ValueError, match="未开放"):
        check_edit({"path": "a.md", "change_mode": "engineering"}, "a", "b")


async def test_tools_require_evidence_and_read_current_revision(
    candidate,
    session,
):
    store, _, cases, _ = candidate
    session.start("snapshot", "初始尝试")
    row = session.row
    trace = {"reads": [], "edits": []}
    tools = {
        t.name: t for t in optimizer.optimizer_tools(session, cases, trace)
    }
    target = "review.prompt"
    with pytest.raises(ValueError, match="全部支持项和反例"):
        await tools["edit_harness"].call(
            target_id=target,
            old_text="Trace",
            new_text="Verify",
        )
    for ref in row["task"]["proposal"]["supporting_findings"]:
        await tools["read_finding"].call(**ref)
    with pytest.raises(ValueError, match="反例"):
        await tools["edit_harness"].call(
            target_id=target,
            old_text="Trace",
            new_text="Verify",
        )
    for ref in row["task"]["proposal"]["counter_evidence"]:
        await tools["read_finding"].call(**ref)
    await tools["read_harness"].call(target_id=target)
    await tools["edit_harness"].call(
        target_id=target,
        old_text="Trace",
        new_text="Verify",
    )
    text = await tools["read_harness"].call(target_id=target)
    assert "Verify" in text.content[0].text
    text = await tools["read_harness"].call(target_id=target)
    assert "相同片段" in text.content[0].text
    assert store.check(row)["changed_files"] == ["harness.md"]
    store.commit(row, "Verify callers")
    child = store.create(
        store.resolve(row["candidate_id"]),
        row["task"],
        row["targets"],
    )
    assert "Verify" in (Path(child["worktree"]) / "harness.md").read_text()
    assert (
        "Trace"
        in (
            Path(row["task"]["targets_source"]).parent / "harness.md"
        ).read_text()
    )
    assert not (Path(row["task"]["targets_source"]).parent / ".git").exists()
    with pytest.raises(ValueError, match="已保存"):
        store.commit(row, "Do not rewrite")


def test_real_git_siblings_merge_revert_and_clean(candidate):
    store, row, _, _ = candidate
    base = row["base_revision"]
    # Long enough separation for Git to merge independent changes in one file.
    original = "\n".join(str(i) for i in range(20)) + "\n"
    path = Path(row["worktree"]) / "harness.md"
    path.write_text(original)
    store.commit(row, "Seed")
    a = store.create(row["candidate_revision"], {}, row["targets"])
    b = store.create(row["candidate_revision"], {}, row["targets"])
    (Path(a["worktree"]) / "harness.md").write_text(
        original.replace("0\n", "zero\n", 1),
    )
    (Path(b["worktree"]) / "harness.md").write_text(
        original.replace("19\n", "nineteen\n"),
    )
    store.commit(a, "A")
    store.commit(b, "B")
    merged = store.integrate(a, b)
    assert merged["status"] == "ready_for_evaluation", merged["error"]
    assert (
        len(
            store.git(
                "rev-list",
                "--parents",
                "-n",
                "1",
                merged["candidate_revision"],
            ).split(),
        )
        == 3
    )
    reverted = store.integrate(merged, a, operation="revert")
    assert reverted["status"] == "ready_for_evaluation", reverted["error"]
    assert store.content(
        reverted["candidate_revision"],
        "harness.md",
    ) == store.content(
        b["candidate_revision"],
        "harness.md",
    )
    assert store.resolve(base) == base  # Original anchor is still reachable.
    picked = store.integrate(row, b, operation="pick")
    assert picked["status"] == "ready_for_evaluation", picked["error"]
    assert store.content(
        picked["candidate_revision"],
        "harness.md",
    ) == store.content(
        b["candidate_revision"],
        "harness.md",
    )
    store.reject(a)
    store.clean(a)
    assert not Path(a["worktree"]).exists()
    assert store.resolve(a["candidate_id"]) == a["candidate_revision"]
    assert store.diff(a)


def test_conflicts_and_out_of_scope_files_preserve_drafts(candidate):
    store, a, _, _ = candidate
    b = store.create(a["base_revision"], {}, a["targets"])
    for row, content in [(a, "A\n"), (b, "B\n")]:
        (Path(row["worktree"]) / "harness.md").write_text(content)
        store.commit(row, content)
    merged = store.integrate(a, b)
    assert merged["status"] == "conflict" and not merged["candidate_revision"]
    with pytest.raises(ValueError):
        store.clean(merged)
    outside = Path(merged["worktree"]) / "unexpected.md"
    outside.write_text("Do not commit me")
    with pytest.raises(ValueError, match="未授权"):
        store.commit(merged, "Invalid")
    outside.unlink()
    (Path(merged["worktree"]) / "harness.md").write_text("Resolved\n")
    store.commit(merged, "Resolve conflict")
    assert (
        merged["status"] == "ready_for_evaluation" and merged["error"] is None
    )


def test_explicit_new_markdown_is_versioned(candidate):
    store, parent, _, _ = candidate
    target = {
        "id": "skill",
        "path": "skills/review/SKILL.md",
        "change_mode": "add_file",
        "edit_rules": {"sections": ["*"]},
    }
    row = store.create(parent["base_revision"], {}, {"skill": target})
    path = Path(row["worktree"]) / target["path"]
    path.parent.mkdir(parents=True)
    path.write_text("# Review\nVerify evidence.\n")
    assert "Verify evidence" in store.diff(row)
    store.commit(row, "Candidate skill, loading still needs evaluation")
    assert (
        store.content(row["candidate_revision"], target["path"])
        == path.read_text()
    )


class OptimizeModel(summary.first.OfflineToolModel):
    def __init__(self, proposal):
        super().__init__(None)
        self.steps = [
            ("read_finding", ref)
            for ref in proposal["supporting_findings"]
            + proposal["counter_evidence"]
        ]
        self.steps += [
            ("list_versions", {}),
            (
                "start_candidate",
                {"base": "snapshot", "reason": "无相关历史修改"},
            ),
            ("read_harness", {"target_id": "review.prompt"}),
            (
                "edit_harness",
                {
                    "target_id": "review.prompt",
                    "old_text": "Trace",
                    "new_text": "Verify",
                },
            ),
            ("inspect_candidate", {}),
            (
                "GenerateStructuredOutput",
                {
                    "status": "modified",
                    "summary": "核查调用方",
                    "limitations": ["效果尚未评测"],
                },
            ),
        ]

    async def __call__(self, **kwargs):
        index = len(self.calls)
        self.calls.append(kwargs)
        name, args = self.steps[index]

        async def stream():
            yield ChatResponse(
                content=[
                    ToolCallBlock(
                        id=str(index),
                        name=name,
                        input=json.dumps(args),
                    ),
                ],
                is_last=True,
            )

        return stream()


@pytest.mark.parametrize("base", ["auto", "snapshot"])
async def test_console_background_real_agent_and_git(
    bundle,
    ctx,
    config,
    offline,
    monkeypatch,
    base,
):
    model = OptimizeModel(read_json(bundle)["proposals"][0])
    if base == "snapshot":
        model.steps = [s for s in model.steps if s[0] != "start_candidate"]
    monkeypatch.setattr(
        "qwenpaw.runtime.builder.AgentBuilder.build_model",
        lambda *a: (model, None),
    )
    response = await command.handle_optimize(ctx, f'"{bundle}" --base {base}')
    assert "已启动" in response.get_text_content()
    assert await ctx.workspace.task_tracker.wait_all_done(timeout=15)
    root = ctx.workspace_dir / "selflearn"
    status = read_json(root / "optimize_status.json")
    assert status["state"] == "ready_for_evaluation", status
    row = read_json(Path(status["output"]))
    assert row["checks"]["passed"] and row["candidate_revision"]
    run = read_json(Path(status["run_output"]))
    assert run["selected_candidate_id"] == row["candidate_id"]
    assert len(run["attempts"]) == 1
    assert Path(status["run_output"]).with_name("trace.json").exists()
    names = {t["function"]["name"] for t in model.calls[0]["tools"]}
    assert "edit_harness" in names and "recall_history" in names
    assert not names & {"Bash", "execute_shell_command", "git", "write_file"}
    for text in [
        "list",
        "status",
        f"show {row['candidate_id']}",
        f"diff {row['candidate_id']}",
    ]:
        response = await command.handle_optimize(ctx, text)
        assert response.get_text_content()
    assert not (
        root / "harnesses" / row["harness_id"] / "active.json"
    ).exists()


@pytest.mark.parametrize("fail", [False, True])
async def test_stop_or_failure_preserves_draft(
    bundle,
    ctx,
    offline,
    monkeypatch,
    fail,
):
    entered = asyncio.Event()

    async def wait(config, model, session, cases):
        session.start("snapshot", "中断测试")
        row = session.row
        (Path(row["worktree"]) / "harness.md").write_text("Draft\n")
        entered.set()
        if fail:
            raise TimeoutError("Disconnected")
        await asyncio.Event().wait()

    monkeypatch.setattr(optimizer, "optimize_candidate", wait)
    await command.handle_optimize(ctx, f'"{bundle}"')
    await asyncio.wait_for(entered.wait(), 10)
    if not fail:
        await command.handle_optimize(ctx, "stop")
    assert await ctx.workspace.task_tracker.wait_all_done(timeout=10)
    status = read_json(ctx.workspace_dir / "selflearn/optimize_status.json")
    assert status["state"] == ("failed" if fail else "stopped")
    row = read_json(Path(status["output"]))
    assert not row["candidate_revision"]
    assert (Path(row["worktree"]) / "harness.md").read_text() == "Draft\n"


async def test_command_registration_and_channel_boundary(ctx):
    from qwenpaw.runtime.builtin_commands import collect_builtin_command_specs

    spec = next(
        s for s in collect_builtin_command_specs() if s.name == "optimize"
    )
    assert spec.handler is command.handle_optimize
    ctx.request.channel = "group"
    response = await spec.handler(ctx, "list")
    assert "仅支持" in response.get_text_content()


async def test_switching_versions_resets_reads_and_selects_earlier_checkpoint(
    session,
    candidate,
):
    store, _, cases, _ = candidate
    trace = {"reads": [], "edits": []}
    tools = {
        t.name: t for t in optimizer.optimizer_tools(session, cases, trace)
    }

    async def call(name, **kwargs):
        return await tools[name].call(**kwargs)

    for ref in (
        session.task["proposal"]["supporting_findings"]
        + session.task["proposal"]["counter_evidence"]
    ):
        await call("read_finding", **ref)
    await call("start_candidate", base="snapshot", reason="方案 A")
    a = session.row
    await call("read_harness", target_id="review.prompt")
    await call(
        "edit_harness",
        target_id="review.prompt",
        old_text="Trace",
        new_text="Verify",
    )
    with pytest.raises(ValueError, match="切换前"):
        await call("start_candidate", base="snapshot", reason="不能丢弃")
    await call("checkpoint_candidate", reason="保留 A")
    with pytest.raises(ValueError, match="已保存"):
        await call(
            "edit_harness",
            target_id="review.prompt",
            old_text="Verify",
            new_text="Wrong",
        )
    await call("start_candidate", base="snapshot", reason="对照方案 B")
    b = session.row
    with pytest.raises(ValueError, match="read_harness"):
        await call(
            "edit_harness",
            target_id="review.prompt",
            old_text="Trace",
            new_text="Inspect",
        )
    fresh = await call("read_harness", target_id="review.prompt")
    assert (
        "Trace" in fresh.content[0].text
        and "相同片段" not in fresh.content[0].text
    )
    await call(
        "edit_harness",
        target_id="review.prompt",
        old_text="Trace",
        new_text="Inspect",
    )
    await call("reject_candidate", reason="与原有要求重复")
    session.finish(
        optimizer.Optimization(
            status="modified",
            summary="选较小改动 A",
            limitations=["尚未评测"],
            candidate_id=a["candidate_id"],
        ),
    )
    assert session.state["selected_candidate_id"] == a["candidate_id"]
    assert store.record(b["candidate_id"])["status"] == "rejected"
    assert "Inspect" in (Path(b["worktree"]) / "harness.md").read_text()
    assert "Verify" in store.content(a["candidate_revision"], "harness.md")
    assert len(session.state["attempts"]) == 2
    historical = await call(
        "read_version",
        version=a["candidate_id"],
        view="file",
        target_id="review.prompt",
    )
    assert "Verify" in historical.content[0].text


async def test_agent_integration_keeps_scope_and_conflicts_editable(
    session,
    candidate,
):
    store, historical, cases, _ = candidate
    (Path(historical["worktree"]) / "harness.md").write_text("A\n")
    store.commit(historical, "Historical A")
    session.start("snapshot", "方案 B")
    (Path(session.row["worktree"]) / "harness.md").write_text("B\n")
    b = session.checkpoint("保留 B")
    session.start(b["candidate_id"], "合并 A 和 B")
    merged = session.integrate("merge", historical["candidate_id"], "核查冲突")
    assert merged["status"] == "conflict"
    assert not session.row["candidate_revision"]
    assert "<<<<<<<" in session.current["targets"]["review.prompt"]["content"]
    tools = {
        t.name: t
        for t in optimizer.optimizer_tools(
            session,
            cases,
            {"reads": [], "edits": []},
        )
    }
    inspected = await tools["inspect_candidate"].call()
    report = json.loads(inspected.content[0].text)
    assert not report["checks"]["passed"] and report["diff"]["lines"]
    for ref in (
        session.task["proposal"]["supporting_findings"]
        + session.task["proposal"]["counter_evidence"]
    ):
        await tools["read_finding"].call(**ref)
    await tools["read_harness"].call(target_id="review.prompt")
    await tools["edit_harness"].call(
        target_id="review.prompt",
        old_text=session.current["targets"]["review.prompt"]["content"],
        new_text="A and B\n",
    )
    await tools["checkpoint_candidate"].call(reason="已核查并解决冲突")
    assert (
        store.content(session.row["candidate_revision"], "harness.md")
        == "A and B\n"
    )
    assert (
        len(
            store.git(
                "show",
                "-s",
                "--format=%P",
                session.row["candidate_revision"],
            ).split(),
        )
        == 2
    )
    assert (
        store.record(historical["candidate_id"])["status"]
        == "ready_for_evaluation"
    )

    extra = {
        "id": "extra",
        "path": "other.md",
        "change_mode": "add_file",
        "edit_rules": {"sections": ["*"]},
    }
    source = store.create(historical["base_revision"], {}, {"extra": extra})
    (Path(source["worktree"]) / "other.md").write_text("Unexpected\n")
    store.commit(source, "Other target")
    with pytest.raises(ValueError, match="范围外"):
        session.start(source["candidate_id"], "禁止通过换起点扩大权限")
    session.start("snapshot", "只开放本轮目标")
    session.integrate("pick", source["candidate_id"], "禁止继承历史权限")
    assert session.row["status"] == "conflict"
    assert "extra" not in session.row["targets"]
    with pytest.raises(ValueError, match="未授权"):
        session.checkpoint("不能保存")


@pytest.mark.parametrize("action", ["pick", "revert"])
def test_agent_can_pick_and_revert_without_auto_commit(
    session,
    candidate,
    action,
):
    store, source, _, _ = candidate
    (Path(source["worktree"]) / "harness.md").write_text("Verified\n")
    store.commit(source, "Source")
    session.start(
        source["candidate_id"] if action == "revert" else "snapshot",
        action,
    )
    session.integrate(action, source["candidate_id"], "独立检查差异")
    assert not session.row["candidate_revision"]
    assert session.row["status"] == "running"
    session.checkpoint(action)
    expected = (
        store.content(source["base_revision"], "harness.md")
        if action == "revert"
        else "Verified\n"
    )
    assert (
        store.content(session.row["candidate_revision"], "harness.md")
        == expected
    )


def test_no_change_can_reuse_history_without_relabeling_it(session, candidate):
    store, historical, _, _ = candidate
    (Path(historical["worktree"]) / "harness.md").write_text(
        "Already solved\n",
    )
    store.commit(historical, "Existing solution")
    session.finish(
        optimizer.Optimization(
            status="no_change",
            summary="已有规则覆盖该问题",
            limitations=[],
            candidate_id=historical["candidate_id"],
        ),
    )
    assert session.state["attempts"] == []
    assert session.state["selected_candidate_id"] == historical["candidate_id"]
    assert store.record(historical["candidate_id"]) == historical
