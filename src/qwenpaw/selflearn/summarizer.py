# -*- coding: utf-8 -*-
"""Cross-episode proposals, grounded in a frozen, allowlisted harness."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .analyzer import (
    build_readonly_agent,
    digest,
    field_text,
    instructions,
    json_text,
    now,
    pointer_value,
    read_jsonl,
    write_json,
)

FINDING_FIELDS = (
    "ai_claim",
    "relation",
    "stance",
    "feedback_reliability",
    "ai_assessment",
    "evidence",
    "reason",
    "problem",
)


class FindingRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    episode_id: str
    finding_index: int = Field(ge=0)


class HarnessEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_id: str
    quote: str = Field(min_length=1)


class Proposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    problem: str = Field(min_length=1)
    supporting_findings: list[FindingRef] = Field(min_length=1)
    counter_evidence: list[FindingRef]
    root_cause_hypothesis: str = Field(min_length=1)
    primary_target: str | None
    harness_evidence: list[HarnessEvidence]
    change_intent: str | None
    alternatives: list[str]
    verification_plan: list[str]
    status: Literal["propose", "needs_evidence", "no_change"]


class Proposals(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(min_length=1)
    proposals: list[Proposal]
    limitations: list[str]


class HarnessTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    kind: Literal[
        "prompt",
        "skill",
        "knowledge",
        "context",
        "tools",
        "verification",
    ]
    path: str
    scope: str
    change_mode: Literal["text_only", "config_only", "add_file", "engineering"]
    allowed_changes: str
    edit_rules: dict = Field(default_factory=dict)


def load_cases(source: Path):
    manifest = json.loads(
        source.with_name("manifest.json").read_text(encoding="utf-8"),
    )
    episodes_path = (source.parent / manifest["source"]).resolve()
    episodes = {e["episode_id"]: e for e in read_jsonl(episodes_path)}
    latest = {row["episode_id"]: row for row in read_jsonl(source)}
    if not latest:
        raise ValueError("第一阶段还没有成功的分析结果")
    cases = {}
    for episode_id, row in latest.items():
        episode = episodes[episode_id]
        if row["run"]["input_hash"] != digest(episode):
            raise ValueError(f"原始案例已变化，请先重新分析：{episode_id}")
        findings = [
            {key: finding[key] for key in FINDING_FIELDS}
            for finding in row["findings"]
        ]
        for finding in findings:
            for evidence in finding["evidence"]:
                value = pointer_value(episode, evidence["pointer"])
                if not evidence["quote"] or evidence[
                    "quote"
                ] not in field_text(value):
                    raise ValueError(f"第一阶段引用与原文不符：{episode_id}")
        subject = episode["subject"]
        cases[episode_id] = {
            "group_id": f"{subject['repo']}#{subject['pr_number']}",
            "analysis": {
                "summary": row["summary"],
                "findings": findings,
                "missing_evidence": row["missing_evidence"],
            },
            "episode": episode,
        }
    return cases, {
        "episodes_source": str(episodes_path),
        "analyzed_episodes": len(cases),
        "available_episodes": len(episodes),
        "independent_prs": len({c["group_id"] for c in cases.values()}),
    }


def snapshot_harness(path: Path) -> dict:
    spec = json.loads(path.read_text(encoding="utf-8"))
    root = (path.parent / spec["root"]).resolve()
    targets = {}
    for raw in spec["targets"]:
        target = HarnessTarget.model_validate(raw)
        file = (root / target.path).resolve()
        if not file.is_relative_to(root):
            raise ValueError(f"目标必须位于指定 root 内：{target.path}")
        target = target.model_copy(
            update={
                "path": file.relative_to(root).as_posix(),
            },
        )
        if target.id in targets:
            raise ValueError(f"重复的目标 ID：{target.id}")
        exists = file.is_file()
        if not exists and target.change_mode != "add_file":
            raise ValueError(f"找不到 Harness 文件：{file}")
        content = file.read_text(encoding="utf-8") if exists else ""
        targets[target.id] = {
            **target.model_dump(),
            "exists": exists,
            "content": content,
            "content_hash": digest(content) if exists else None,
        }
    if not targets:
        raise ValueError("可修改的 Harness 目标清单不能为空")
    return {"name": spec["name"], "root": str(root), "targets": targets}


def page(
    value,
    start_line,
    search,
    end_line=None,
    context_lines=0,
    limit=200,
) -> str:
    text = (
        value
        if isinstance(value, str)
        else json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
        )
    )
    lines = text.splitlines()
    if (
        start_line < 1
        or context_lines < 0
        or (end_line is not None and end_line < start_line)
    ):
        raise ValueError("无效的读取范围")
    start, stop = start_line - 1, min(end_line or len(lines), len(lines))
    indices = (
        sorted(
            {
                j
                for i in range(stop)
                if search in lines[i]
                for j in range(
                    max(start, i - context_lines),
                    min(stop, i + context_lines + 1),
                )
            },
        )
        if search
        else list(range(start, stop))
    )
    # Multiline JSON lets QwenPaw's line-aware pruner retain recovery hints.
    return json.dumps(
        {
            "total_lines": len(lines),
            "lines": [(i + 1, lines[i]) for i in indices[:limit]],
            "next_start_line": indices[limit] + 1
            if len(indices) > limit
            else None,
        },
        ensure_ascii=False,
        indent=2,
    )


def summary_tools(cases, snapshot, reads):
    from agentscope.tool import FunctionTool

    seen = set()

    def read_case(
        episode_id: str,
        pointer: str = "/analysis",
        start_line: int = 1,
        search: str = "",
    ) -> str:
        """Read analysis or original evidence, up to 200 lines.

        Args:
            episode_id: An ID from the case index.
            pointer: /analysis or /episode/... JSON Pointer.
            start_line: First line (1-based), also pages search results.
            search: Optional literal substring filter.
        """
        value = pointer_value(cases[episode_id], pointer)
        reads.append(
            {
                "tool": "read_case",
                "episode_id": episode_id,
                "pointer": pointer,
                "start_line": start_line,
                "search": search,
            },
        )
        return page(value, start_line, search)

    def read_harness(
        target_id: str,
        start_line: int = 1,
        search: str = "",
        end_line: int | None = None,
        context_lines: int = 3,
        reread: bool = False,
    ) -> str:
        """Search a frozen harness first, then expand specific line ranges.

        Args:
            target_id: An ID from the harness target list.
            start_line: First line (1-based), also pages search results.
            search: Optional literal substring filter.
            end_line: Last line (inclusive). Without search or end_line,
                return only 60 lines; otherwise up to 200. Use next_start_line.
            context_lines: Include this many lines around search hits.
            reread: Return even an identical previously read excerpt, e.g.
                when the earlier output was pruned or scrolled out.
        """
        target = snapshot["targets"][target_id]
        read = {
            "tool": "read_harness",
            "target_id": target_id,
            "start_line": start_line,
            "search": search,
            "end_line": end_line,
            "context_lines": context_lines,
            "reread": reread,
        }
        if "revision" in snapshot:
            read["revision"] = snapshot["revision"]
        reads.append(read)
        if not target["exists"]:
            return "该文件在快照中不存在；新增后还需要接入加载/触发流程。"
        result = page(
            target["content"],
            start_line,
            search,
            end_line,
            context_lines,
            limit=200 if search or end_line is not None else 60,
        )
        key = (
            snapshot.get("revision"),
            target["path"],
            target["content_hash"],
            digest(result),
        )
        read["duplicate"] = key in seen and not reread
        if read["duplicate"]:
            return "同一文件的相同片段此前已返回；如需原文，请用相同参数加 reread=true。"
        seen.add(key)
        return result

    return [
        FunctionTool(fn, is_read_only=True) for fn in (read_case, read_harness)
    ]


def summary_prompt(cases, snapshot, coverage) -> str:
    index = []
    for episode_id, case in cases.items():
        analysis, episode = case["analysis"], case["episode"]
        index.append(
            {
                "episode_id": episode_id,
                "group_id": case["group_id"],
                "summary": analysis["summary"],
                "missing_evidence": analysis["missing_evidence"],
                "quality_flags": episode.get("quality_flags", []),
                "window": episode.get("window", {}),
                "reviewed_code": {
                    k: v
                    for k, v in episode.get(
                        "reviewed_code",
                        {},
                    ).items()
                    if k != "diff"
                },
                "findings": [
                    {
                        "finding_index": i,
                        **{
                            k: v
                            for k, v in f.items()
                            if k not in {"evidence", "reason"}
                        },
                    }
                    for i, f in enumerate(analysis["findings"])
                ],
            },
        )
    targets = [
        {k: v for k, v in t.items() if k != "content"}
        for t in snapshot["targets"].values()
    ]
    return (
        "Inspect evidence and harness, then propose improvements.\n"
        + json_text(
            {
                "coverage": coverage,
                "cases": index,
                "harness_targets": targets,
            },
        )
    )


def validate_proposals(result: Proposals, cases, snapshot) -> None:
    targets = snapshot["targets"]
    for proposal in result.proposals:
        for ref in proposal.supporting_findings + proposal.counter_evidence:
            if ref.episode_id not in cases or ref.finding_index >= len(
                cases[ref.episode_id]["analysis"]["findings"],
            ):
                raise ValueError(f"未知的案例分析项：{ref}")
        if (
            proposal.primary_target is not None
            and proposal.primary_target not in targets
        ):
            raise ValueError(f"未开放的修改目标：{proposal.primary_target}")
        for evidence in proposal.harness_evidence:
            target = targets.get(evidence.target_id)
            if target is None or evidence.quote not in target["content"]:
                raise ValueError(f"Harness 引用与快照不符：{evidence.target_id}")
        if proposal.status == "propose" and not all(
            (
                proposal.primary_target,
                proposal.change_intent,
                proposal.harness_evidence,
                proposal.verification_plan,
            ),
        ):
            raise ValueError("propose 必须包含目标、修改意图、文件证据和验证计划")
        if (
            proposal.status == "propose"
            and targets[proposal.primary_target]["exists"]
            and proposal.primary_target
            not in {e.target_id for e in proposal.harness_evidence}
        ):
            raise ValueError("需要引用首选修改目标的文件内容")


def proposal_rows(result, cases):
    return [
        {
            **p.model_dump(mode="json"),
            "independent_pr_count": len(
                {
                    cases[ref.episode_id]["group_id"]
                    for ref in p.supporting_findings
                },
            ),
        }
        for p in result.proposals
    ]


MAX_CORRECTIONS = 3


# One lifecycle supports both isolated runs and an attached debugging chat.
# pylint: disable-next=too-many-branches
async def summary_events(
    config,
    model,
    cases,
    snapshot,
    coverage,
    output_dir,
    prompt,
    chat=None,
):
    from agentscope.message import Msg, TextBlock
    from ..utils.io_utils import run_sync_io

    trace = {"reads": [], "inputs": [], "replies": []}
    if chat is not None:
        from ..hooks.session.session_hook import SessionLoadHook

        await SessionLoadHook().run(chat)
    agent = await run_sync_io(
        build_readonly_agent,
        config,
        model,
        prompt,
        chat.session_id if chat else output_dir.name,
        summary_tools(cases, snapshot, trace["reads"]),
        workspace_dir=chat.workspace_dir if chat else output_dir / "context",
        max_iters=32,
        isolate=chat is None,
    )
    if chat is not None:
        chat.agent = agent  # Runtime persists and closes this chat's agent.
        chat.agent_config = config
        if chat.session_state:
            agent.load_state_dict(chat.session_state)
        agent.state.session_id = chat.session_id
        await agent.observe(chat.input_msgs)
    message = summary_prompt(cases, snapshot, coverage)
    try:
        write_json(
            output_dir / "input.json",
            {
                # pylint: disable-next=protected-access
                "system_prompt": agent._system_prompt,
                "task": message,
                "session_before": chat.session_state if chat else None,
            },
        )
        for attempt in range(MAX_CORRECTIONS + 1):
            request = Msg(
                name="user",
                role="user",
                content=[TextBlock(text=message)],
            )
            trace["inputs"].append(request.model_dump(mode="json"))
            if chat is not None:
                yield request
            response = None
            async for event in agent.reply_stream(
                inputs=request,
                structured_schema=Proposals,
                yield_final_msg=True,
            ):
                if isinstance(event, Msg):
                    response = event
                else:
                    yield event
            if response is None:
                raise RuntimeError("模型未返回最终回复")
            trace["replies"].append(response.model_dump(mode="json"))
            if response.finished_reason == "interrupted":
                raise asyncio.CancelledError()
            try:
                result = Proposals.model_validate(response.structured_output)
                validate_proposals(result, cases, snapshot)
            except ValueError as exc:
                if attempt == MAX_CORRECTIONS:
                    raise
                message = (
                    f"任务单校验失败，第 {attempt + 1}/{MAX_CORRECTIONS} 次修正。"
                    f"请回查证据后返回完整结构化结果：{exc}"
                )
            else:
                yield result
                return
    finally:
        try:
            write_json(output_dir / "trace.json", trace)
        finally:
            if chat is None:
                await agent.close()


async def summarize(
    config,
    model,
    cases,
    snapshot,
    coverage,
    output_dir,
    prompt,
):
    async for event in summary_events(
        config,
        model,
        cases,
        snapshot,
        coverage,
        output_dir,
        prompt,
    ):
        if isinstance(event, Proposals):
            result = event
    return result


async def run_summary(source, targets_path, root, config, chat=None):
    from ..runtime.builder import AgentBuilder
    from ..utils.io_utils import run_sync_io

    output_dir = root / "proposals" / uuid4().hex
    output_dir.mkdir(parents=True)
    output = output_dir / "improvement_proposals.json"
    status_path = root / "status.json"
    status = {
        "phase": "summarize",
        "state": "running",
        "source": str(source),
        "total": 0,
        "proposal_count": 0,
        "output": str(output),
        "error": None,
        "started_at": now(),
    }
    if chat is not None:
        status["run_key"] = chat.extras["selflearn_run_key"]
        status[
            "task_started_at"
        ] = await chat.workspace.task_tracker.get_run_started_at(
            status["run_key"],
        )
    write_json(status_path, status)
    try:
        cases, coverage = await run_sync_io(load_cases, source)
        snapshot = await run_sync_io(snapshot_harness, targets_path)
        prompt = instructions("SUMMARIZE.md")
        code_hash = digest(
            {
                name: Path(__file__)
                .with_name(name)
                .read_text(
                    encoding="utf-8",
                )
                for name in ("summarizer.py", "analyzer.py")
            },
        )
        status["total"] = len(cases)
        write_json(status_path, status)
        write_json(output_dir / "cases.json", cases)
        write_json(output_dir / "harness_snapshot.json", snapshot)
        write_json(
            output_dir / "manifest.json",
            {
                "instructions": prompt,
                "coverage": coverage,
                "model": config.active_model.model_dump(mode="json"),
                "thinking_level": config.thinking_level,
                "code_hash": code_hash,
                "context_config": (
                    config.running.light_context_config.model_dump()
                ),
                "chat_session_id": chat.session_id if chat else None,
                "max_corrections": MAX_CORRECTIONS,
            },
        )
        yield json_text(status)
        model, _ = await run_sync_io(AgentBuilder().build_model, config)
        async with asyncio.timeout(1200):
            if chat is None:
                result = await summarize(
                    config,
                    model,
                    cases,
                    snapshot,
                    coverage,
                    output_dir,
                    prompt,
                )
            else:
                async for event in summary_events(
                    config,
                    model,
                    cases,
                    snapshot,
                    coverage,
                    output_dir,
                    prompt,
                    chat,
                ):
                    if isinstance(event, Proposals):
                        result = event
                    else:
                        yield event
        write_json(
            output,
            {
                "schema_version": 1,
                "summary": result.summary,
                "proposals": proposal_rows(result, cases),
                "limitations": result.limitations,
                "coverage": coverage,
                "run": {
                    "source": str(source),
                    "targets_source": str(targets_path),
                    "cases_hash": digest(cases),
                    "harness_hash": digest(snapshot),
                    "instructions_hash": digest(prompt),
                    "code_hash": code_hash,
                    "session_id": output_dir.name,
                    "model": str(model.model),
                    "at": now(),
                    "chat_session_id": chat.session_id if chat else None,
                },
            },
        )
        status.update(state="completed", proposal_count=len(result.proposals))
    except (asyncio.CancelledError, GeneratorExit):
        status["state"] = "stopped"
        raise
    except Exception as exc:
        status.update(state="failed", error=f"{type(exc).__name__}: {exc}")
    finally:
        status["finished_at"] = now()
        write_json(status_path, status)
