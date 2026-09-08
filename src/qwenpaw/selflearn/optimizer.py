# -*- coding: utf-8 -*-
"""Grounded optimization with lazy history and isolated worktrees."""

import asyncio
import json
from functools import wraps
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .analyzer import (
    build_readonly_agent,
    digest,
    instructions,
    json_text,
    now,
    pointer_value,
    write_json,
)
from .edit_scope import check_edit, harness_path
from .exploration import OptimizationSession
from .summarizer import (
    Proposal,
    Proposals,
    page,
    summary_tools,
    validate_proposals,
)
from .versions import HarnessStore, read_json


class Optimization(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["modified", "no_change", "needs_evidence", "needs_scope"]
    summary: str = Field(min_length=1)
    limitations: list[str]
    candidate_id: str | None = None


def load_task(source: Path, number=1, targets_path=None, also_targets=()):
    document = read_json(source)
    if document.get("schema_version") != 1:
        raise ValueError("请输入 Analyze 生成的 improvement_proposals.json")
    if not 1 <= number <= len(document["proposals"]):
        raise ValueError("--proposal 从 1 开始，不能超过提案数量")
    raw = document["proposals"][number - 1]
    proposal = Proposal.model_validate(
        {k: v for k, v in raw.items() if k != "independent_pr_count"},
    )
    if proposal.status != "propose":
        raise ValueError(f"该提案为 {proposal.status}，不会启动自动修改")
    snapshot = read_json(source.with_name("harness_snapshot.json"))
    cases = read_json(source.with_name("cases.json"))
    run = document["run"]
    if (
        digest(snapshot) != run["harness_hash"]
        or digest(cases) != run["cases_hash"]
    ):
        raise ValueError("案例或 Harness 快照已变化，与 proposal 不匹配")
    validate_proposals(
        Proposals(
            summary=document["summary"],
            proposals=[proposal],
            limitations=document["limitations"],
        ),
        cases,
        snapshot,
    )
    targets_path = targets_path or Path(run["targets_source"])
    spec = read_json(targets_path)
    configured = {t["id"]: t for t in spec["targets"]}
    targets = {}
    for key in dict.fromkeys([proposal.primary_target, *also_targets]):
        frozen, current = snapshot["targets"][key], configured[key]
        if any(
            frozen[k] != current[k]
            for k in (
                "path",
                "kind",
                "scope",
                "change_mode",
                "allowed_changes",
            )
        ):
            raise ValueError(f"目标范围已变化，请重新分析：{key}")
        if (
            not current.get("edit_rules")
            or current["change_mode"] == "engineering"
        ):
            raise ValueError(
                f"{key} 未开放自动编辑，请配置 edit_rules 或人工处理",
            )
        targets[key] = current
    refs = [
        ref.model_dump()
        for ref in proposal.supporting_findings + proposal.counter_evidence
    ]
    selected = {r["episode_id"]: cases[r["episode_id"]] for r in refs}
    task = {
        "source": str(source),
        "proposal_number": number,
        "source_hash": digest(document),
        "cases_hash": run["cases_hash"],
        "harness_hash": run["harness_hash"],
        "proposal": proposal.model_dump(),
        "limitations": document["limitations"],
        "independent_pr_count": raw.get("independent_pr_count"),
        "targets_source": str(targets_path),
        "base_targets": {
            key: configured[key]
            for key, frozen in snapshot["targets"].items()
            if key in configured
            and configured[key].get("edit_rules")
            and configured[key]["change_mode"] != "engineering"
            and all(
                frozen[k] == configured[key][k]
                for k in (
                    "path",
                    "kind",
                    "scope",
                    "change_mode",
                    "allowed_changes",
                )
            )
        },
    }
    return task, snapshot, selected, targets


def optimize_prompt(session) -> str:
    proposal = session.task["proposal"]
    return "根据证据生成一个最小候选修改，不评判最终效果、不发布。\n" + json_text(
        {
            "proposal": {
                k: v
                for k, v in proposal.items()
                if k not in {"alternatives", "status"}
            },
            "limitations": session.task["limitations"],
            "snapshot_revision": session.task["snapshot_revision"],
            "initial_base": session.task["initial_base"],
            "current_candidate": (
                session.describe(session.row) if session.row else None
            ),
            "binding": session.binding,
            "editable_targets": list(session.targets.values()),
            "readable_targets": [
                {"id": key, "path": t["path"], "exists": t["exists"]}
                for key, t in session.current["targets"].items()
            ],
        },
    )


# Tool closures deliberately share this run's evidence and current worktree.
# pylint: disable-next=too-many-statements
def optimizer_tools(session, cases, trace):
    from agentscope.tool import FunctionTool
    from ..utils.io_utils import run_sync_io

    store, current = session.store, session.current
    proposal = session.task["proposal"]
    required = {
        (r["episode_id"], r["finding_index"])
        for r in proposal["supporting_findings"] + proposal["counter_evidence"]
    }
    inspected = set()
    reads = trace["reads"]

    def require_evidence():
        if required - inspected:
            raise ValueError("编辑前请先 read_finding 读取全部支持项和反例")

    def read_finding(episode_id: str, finding_index: int) -> str:
        """Read a finding with its evidence and uncertainty."""
        if (episode_id, finding_index) not in required:
            raise ValueError("只能读取本提案引用的分析项")
        analysis = cases[episode_id]["analysis"]
        finding = {
            k: v
            for k, v in analysis["findings"][finding_index].items()
            if k != "signal_strength"
        }
        inspected.add((episode_id, finding_index))
        reads.append(
            {
                "tool": "read_finding",
                "episode_id": episode_id,
                "finding_index": finding_index,
            },
        )
        return json.dumps(
            {
                "finding": finding,
                "missing_evidence": analysis["missing_evidence"],
            },
            ensure_ascii=False,
            indent=2,
        )

    def read_evidence(
        episode_id: str,
        pointer: str,
        start_line: int = 1,
        search: str = "",
    ) -> str:
        """Read an original field by JSON Pointer, with paging/search."""
        value = pointer_value(cases[episode_id]["episode"], pointer)
        reads.append(
            {
                "tool": "read_evidence",
                "episode_id": episode_id,
                "pointer": pointer,
                "start_line": start_line,
                "search": search,
            },
        )
        return page(value, start_line, search)

    def read_alternatives() -> str:
        """Read the proposal's alternative hypotheses and approaches."""
        return json.dumps(
            proposal["alternatives"],
            ensure_ascii=False,
            indent=2,
        )

    def edit_harness(target_id: str, old_text: str, new_text: str) -> str:
        """Replace a unique literal SOURCE excerpt within an authorized target.

        Args:
            target_id: Editable target ID. No arbitrary paths.
            old_text: Exact source text (keep backslashes/braces). Empty only
                when creating an explicitly allowed add_file target.
            new_text: Replacement source text; must pass edit-scope checks.
        """
        row = session.draft()
        require_evidence()
        if not any(
            r.get("tool") == "read_harness"
            and r.get("target_id") == target_id
            and r.get("revision") == row["candidate_id"]
            for r in reads
        ):
            raise ValueError("编辑前请先 read_harness 核查所选起点的目标内容")
        target = row["targets"][target_id]
        path = harness_path(Path(row["worktree"]), target["path"])
        before = path.read_text(encoding="utf-8") if path.exists() else ""
        if path.exists():
            if not old_text or before.count(old_text) != 1:
                raise ValueError("old_text 必须在当前源文件中唯一匹配")
            after = before.replace(old_text, new_text, 1)
        elif target["change_mode"] == "add_file" and not old_text:
            after = new_text
        else:
            raise ValueError("该目标不允许新增文件")
        check_edit(
            target,
            store.content(row["base_revision"], target["path"]),
            after,
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(after, encoding="utf-8")
        for entry in current["targets"].values():
            if entry["path"] == target["path"]:
                entry.update(
                    content=after,
                    exists=True,
                    content_hash=digest(after),
                )
        trace["edits"].append(
            {
                "candidate_id": row["candidate_id"],
                "target_id": target_id,
                "old_text": old_text,
                "new_text": new_text,
            },
        )
        return "已修改候选文件，未 commit、未发布。" "用 inspect_candidate 查看差异和检查。"

    def inspect_candidate(start_line: int = 1) -> str:
        """Inspect diff and static checks, not the candidate's quality."""
        row = session.row
        if row is None:
            raise ValueError("尚未选择工作版本")
        try:
            checks = store.check(row)
        except ValueError as exc:
            checks = {"passed": False, "error": str(exc)}
        return json.dumps(
            {
                "candidate_id": row["candidate_id"],
                "checks": checks,
                "diff": json.loads(page(store.diff(row), start_line, "")),
            },
            ensure_ascii=False,
            indent=2,
        )

    def list_versions(
        target_id: str = "",
        search: str = "",
        offset: int = 0,
    ) -> str:
        """List up to ten local attempts, newest first (not quality-ranked).

        Args:
            target_id: Optional target filter, empty for all this Harness.
            search: Literal text in problem/summary/rejection reason.
            offset: Page offset. Use next_offset to continue.
        """
        rows = [
            row
            for row in store.records()
            if (not target_id or target_id in row["targets"])
            and search.casefold()
            in json_text(
                [
                    row["task"].get("proposal", {}).get("problem"),
                    row.get("summary"),
                    row.get("rejection_reason"),
                ],
            ).casefold()
        ]
        reads.append(
            {
                "tool": "list_versions",
                "target_id": target_id,
                "search": search,
                "offset": offset,
            },
        )
        return json_text(
            {
                "snapshot_revision": session.task["snapshot_revision"],
                "versions": [
                    {
                        k: v
                        for k, v in session.describe(row).items()
                        if k not in {"binding", "limitations", "worktree"}
                    }
                    for row in rows[offset : offset + 10]
                ],
                "next_offset": offset + 10
                if offset + 10 < len(rows)
                else None,
            },
        )

    def read_version(
        version: str,
        view: Literal["summary", "diff", "file"] = "summary",
        target_id: str = "",
        start_line: int = 1,
        search: str = "",
    ) -> str:
        """Read one saved version lazily; does not switch the current worktree.

        Args:
            version: Candidate ID, full SHA, or snapshot.
            view: summary (provenance), diff (from parent), or file.
            target_id: Required for file; optional diff path filter.
            start_line: Page start (1-based).
            search: Literal text filter.
        """
        revision = session.resolve(version)
        row = next(
            (
                r
                for r in store.records()
                if r["candidate_revision"] == revision
            ),
            None,
        )
        reads.append(
            {
                "tool": "read_version",
                "version": revision,
                "view": view,
                "target_id": target_id,
                "start_line": start_line,
                "search": search,
            },
        )
        if view == "summary":
            return json_text(
                {
                    "revision": revision,
                    "parents": store.git(
                        "show",
                        "-s",
                        "--format=%P",
                        revision,
                    ).split(),
                    "record": session.describe(row) if row else None,
                    "note": "历史经验须对照本轮模型、项目和时间重新核查；缺失信息为未知。",
                },
            )
        relative = (
            session.snapshot["targets"][target_id]["path"]
            if target_id
            else None
        )
        if view == "file":
            if relative is None:
                raise ValueError("读取历史文件需要 target_id")
            value = store.content(revision, relative)
        else:
            value = store.git(
                "diff",
                (
                    row["base_revision"]
                    if row
                    else session.task["snapshot_revision"]
                ),
                revision,
                *(["--", relative] if relative else []),
            )
        return page(value, start_line, search)

    def start_candidate(base: str, reason: str) -> str:
        """Choose snapshot, a saved ID or full SHA; create a worktree.

        Save or reject dirty work before switching. A saved checkpoint can be
        continued by starting its child. Explain relevance to this proposal.
        """
        return json_text(session.start(base, reason))

    def checkpoint_candidate(reason: str) -> str:
        """Commit an immutable checkpoint, not a final selection."""
        require_evidence()
        return json_text(session.checkpoint(reason))

    def reject_candidate(reason: str) -> str:
        """Abandon this run's current attempt; retain files and history."""
        return json_text(session.reject(reason))

    def integrate_candidate(
        action: Literal["merge", "pick", "revert"],
        source: str,
        reason: str,
    ) -> str:
        """Apply a saved candidate in the current clean draft; do not commit.

        merge combines branches; pick transfers only the source's own delta;
        revert reverses that delta. Inspect/resolve conflicts or reject, then
        checkpoint. Current editable_targets remain the only write scope.
        """
        require_evidence()
        return json_text(session.integrate(action, source, reason))

    harness_reader = summary_tools({}, current, reads)[1]

    def tool(fn, read_only):
        @wraps(fn)
        async def call(**kwargs):
            return await run_sync_io(fn, **kwargs)

        return FunctionTool(
            call,
            is_read_only=read_only,
            is_concurrency_safe=read_only,
        )

    return [
        harness_reader,
        *[
            tool(fn, True)
            for fn in (
                read_finding,
                read_evidence,
                read_alternatives,
                inspect_candidate,
                list_versions,
                read_version,
            )
        ],
        *[
            tool(fn, False)
            for fn in (
                edit_harness,
                start_candidate,
                checkpoint_candidate,
                reject_candidate,
                integrate_candidate,
            )
        ],
    ]


async def optimize_candidate(config, model, session, cases):
    from agentscope.message import Msg, TextBlock
    from ..utils.io_utils import run_sync_io

    folder = session.folder
    trace = {"reads": [], "edits": [], "replies": []}
    prompt = instructions("OPTIMIZE.md")
    agent = None

    def build():
        nonlocal agent
        agent = build_readonly_agent(
            config,
            model,
            prompt,
            folder.name,
            optimizer_tools(session, cases, trace),
            workspace_dir=folder / "context",
            max_iters=32,
            name="SelfLearnOptimizer",
        )

    message = optimize_prompt(session)
    try:
        await run_sync_io(build)
        write_json(
            folder / "input.json",
            # Record the effective prompt, including runtime recovery hints.
            # pylint: disable-next=protected-access
            {"system_prompt": agent._system_prompt, "task": message},
        )
        for attempt in range(2):
            response = await agent.reply(
                Msg(
                    name="user",
                    role="user",
                    content=[TextBlock(text=message)],
                ),
                structured_schema=Optimization,
            )
            trace["replies"].append(response.model_dump(mode="json"))
            if response.finished_reason == "interrupted":
                raise asyncio.CancelledError()
            try:
                result = Optimization.model_validate(
                    response.structured_output,
                )
                await run_sync_io(session.finish, result)
                return result
            except ValueError as exc:
                if attempt:
                    raise
                message = f"请修正结果或候选改动后重新返回完整结果：{exc}"
    finally:
        try:
            write_json(folder / "trace.json", trace)
        finally:
            if agent is not None:
                await agent.close()


async def run_optimization(
    source,
    root,
    config,
    number=1,
    base="auto",
    targets_path=None,
    also_targets=(),
):
    from ..runtime.builder import AgentBuilder
    from ..utils.io_utils import run_sync_io

    root.mkdir(parents=True, exist_ok=True)
    status_path = root / "optimize_status.json"
    status = {
        "state": "running",
        "source": str(source),
        "output": None,
        "error": None,
        "started_at": now(),
    }
    session = None
    write_json(status_path, status)
    try:
        task, snapshot, cases, targets = await run_sync_io(
            load_task,
            source,
            number,
            targets_path,
            also_targets,
        )
        store = HarnessStore.for_snapshot(root, snapshot)
        baseline = await run_sync_io(store.import_snapshot, snapshot)
        task["snapshot_revision"] = baseline
        task["initial_base"] = base
        folder = root / "optimization_runs" / uuid4().hex[:12]
        folder.mkdir(parents=True)
        write_json(folder / "cases.json", cases)
        write_json(folder / "harness_snapshot.json", snapshot)
        project_revision = await run_sync_io(
            store.git,
            "rev-parse",
            "--verify",
            "HEAD",
            cwd=snapshot["root"],
            check=False,
        )
        project_status = await run_sync_io(
            store.git,
            "status",
            "--porcelain",
            "--untracked-files=no",
            cwd=snapshot["root"],
            check=False,
        )
        binding = {
            "model": config.active_model.model_dump(mode="json"),
            "thinking_level": config.thinking_level,
            "project": {
                "root": snapshot["root"],
                "revision": project_revision.stdout.strip() or None,
                "tracked_changes": (
                    bool(project_status.stdout)
                    if project_status.returncode == 0
                    else None
                ),
            },
            "harness_hash": task["harness_hash"],
            "cases_hash": task["cases_hash"],
            "snapshot_revision": baseline,
            "created_at": now(),
        }
        write_json(
            folder / "manifest.json",
            {
                **binding,
                "context_config": (
                    config.running.light_context_config.model_dump()
                ),
                "code_hash": digest(
                    {
                        name: Path(__file__)
                        .with_name(name)
                        .read_text(encoding="utf-8")
                        for name in (
                            "optimizer.py",
                            "edit_scope.py",
                            "versions.py",
                            "exploration.py",
                            "analyzer.py",
                        )
                    },
                ),
                "instructions_hash": digest(instructions("OPTIMIZE.md")),
            },
        )

        def create_session():
            nonlocal session
            session = OptimizationSession(
                store,
                task,
                snapshot,
                targets,
                folder,
                binding,
            )
            if base != "auto":
                session.start(base, "用户指定的初始版本")

        await run_sync_io(create_session)
        yield json_text(session.state)
        model, _ = await run_sync_io(AgentBuilder().build_model, config)
        async with asyncio.timeout(1200):
            await optimize_candidate(config, model, session, cases)
    except (asyncio.CancelledError, GeneratorExit):
        if session:
            if session.state["state"] == "running":
                session.stop("stopped")
        else:
            status["state"] = "stopped"
        raise
    except Exception as exc:
        status.update(state="failed", error=f"{type(exc).__name__}: {exc}")
        if session:
            session.stop("failed", status["error"])
    finally:
        if session:
            session.persist()
        else:
            status["finished_at"] = now()
            write_json(status_path, status)
    yield json_text(session.state if session else status)
