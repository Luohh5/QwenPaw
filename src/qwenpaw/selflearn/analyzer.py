# -*- coding: utf-8 -*-
"""Isolated, read-only episode analysis using the QwenPaw agent loop."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pointer: str
    quote: str = Field(min_length=1)


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ai_claim: str | None
    relation: Literal["direct", "indirect", "unrelated", "unclear"]
    stance: Literal["support", "reject", "clarify", "neutral", "unclear"]
    signal_strength: Literal["strong", "medium", "weak", "none"]
    feedback_reliability: Literal[
        "supported",
        "contradicted",
        "unverified",
    ]
    ai_assessment: Literal[
        "correct",
        "incorrect",
        "missed",
        "uncertain",
        "not_applicable",
    ]
    evidence: list[Evidence] = Field(min_length=1)
    reason: str = Field(min_length=1)
    problem: str | None


class Analysis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    findings: list[Finding]
    summary: str = Field(min_length=1)
    missing_evidence: list[str]


def json_text(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def digest(value) -> str:
    return hashlib.sha256(json_text(value).encode()).hexdigest()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_json(path: Path, value) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json_text(value) + "\n", encoding="utf-8")
    temporary.replace(path)


def read_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def append_jsonl(path: Path, value) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json_text(value) + "\n")


def pointer_value(episode: dict, pointer: str):
    if not pointer.startswith("/"):
        raise ValueError("Evidence must use an absolute JSON Pointer")
    value = episode
    for part in pointer[1:].split("/"):
        key = part.replace("~1", "/").replace("~0", "~")
        value = value[int(key)] if isinstance(value, list) else value[key]
    return value


def field_text(value) -> str:
    return value if isinstance(value, str) else json_text(value)


def validate_evidence(result: Analysis, episode: dict) -> None:
    errors = []
    for i, finding in enumerate(result.findings):
        for j, evidence in enumerate(finding.evidence):
            try:
                value = pointer_value(episode, evidence.pointer)
            except (KeyError, IndexError, ValueError, TypeError):
                error = "Unknown evidence pointer"
            else:
                if evidence.quote in field_text(value):
                    continue
                error = "Quote is not present"
            errors.append(
                f"findings[{i}].evidence[{j}]: {error} at {evidence.pointer}; "
                f"quote={json_text(evidence.quote)}",
            )
    if errors:
        raise ValueError("\n".join(errors))


def episode_tools(episode: dict, reads: list):
    from agentscope.tool import FunctionTool

    def read_episode(
        pointer: str,
        start_line: int = 1,
        search: str = "",
    ) -> str:
        """Read up to 200 lines of an original episode field.

        Args:
            pointer: JSON Pointer, for example /reviewed_code/diff.
            start_line: First line (1-based), also used for paging searches.
            search: Optional literal substring; return matching line numbers.
        """
        lines = field_text(pointer_value(episode, pointer)).splitlines()
        offset = max(0, start_line - 1)
        selected = [
            (i + 1, line)
            for i, line in enumerate(lines)
            if i >= offset and (not search or search in line)
        ][:200]
        reads.append(
            {
                "pointer": pointer,
                "start_line": start_line,
                "search": search,
            },
        )
        return json.dumps(
            {"total_lines": len(lines), "lines": selected},
            ensure_ascii=False,
            indent=2,
        )

    return [FunctionTool(read_episode, is_read_only=True)]


def episode_prompt(episode: dict) -> str:
    packet = dict(episode)
    code = dict(packet.get("reviewed_code", {}))
    diff = code.get("diff")
    if diff and len(diff) > 16000:
        code["diff"] = {
            "read_with": "read_episode",
            "pointer": "/reviewed_code/diff",
            "total_lines": len(diff.splitlines()),
        }
        packet["reviewed_code"] = code
    return "分析这一条 episode，返回结构化结果。\n" + json_text(packet)


def instructions(*stage_files: str) -> str:
    root = Path(__file__).parent
    stage_files = stage_files or ("AGENTS.md", "github_review.md")
    return "\n\n".join(
        (root / name).read_text(encoding="utf-8")
        for name in ("BACKGROUND.md", *stage_files)
    )


def build_analyzer(
    config,
    model,
    prompt,
    episode,
    session_id,
    reads,
    workspace_dir,
):
    return build_readonly_agent(
        config,
        model,
        prompt,
        session_id,
        episode_tools(episode, reads),
        workspace_dir=workspace_dir,
    )


def recovery_tools(
    workspace_dir,
    max_bytes,
    scroll,
    archive_dirs=("tool_results", "dialog"),
):
    from agentscope.tool import FunctionTool
    from ..utils.io_utils import read_text_async

    async def read_file(
        file_path: str,
        start_line: int = 1,
        end_line: int | None = None,
        start_char: int = 0,
    ) -> str:
        """Read archived tool results or context from configured cache paths.

        Args:
            file_path: Cache path supplied by a truncation notice.
            start_line: First archive line (1-based).
            end_line: Last archive line, inclusive; omit for the remainder.
            start_char: Character offset within that range. Continue with
                next_start_char and the SAME line range for very long lines.
        """
        path = (workspace_dir / file_path).resolve()
        if not any(
            path.is_relative_to((workspace_dir / name).resolve())
            for name in archive_dirs
        ):
            raise ValueError("只能读取已配置的裁剪缓存")
        if (
            start_line < 1
            or start_char < 0
            or (end_line is not None and end_line < start_line)
        ):
            raise ValueError("无效的读取范围")
        text = await read_text_async(path)
        text = "".join(
            text.splitlines(keepends=True)[start_line - 1 : end_line],
        )
        # Bound UTF-8 bytes, leaving room for the continuation header.
        stop = start_char + max(1, (max_bytes - 512) // 4)
        next_char = stop if stop < len(text) else None
        return (
            f"next_start_char={next_char}; keep the same line range.\n"
            + text[start_char:stop]
        )

    tools = [FunctionTool(read_file, is_read_only=True)]
    if scroll is not None:
        # Parameterized read-only queries over a private DB, never the REPL.
        tools.append(FunctionTool(scroll.recall_tool, is_read_only=True))
    return tools


def build_readonly_agent(
    config,
    model,
    prompt,
    session_id,
    tools,
    *,
    workspace_dir,
    max_iters=12,
    isolate=True,
    name="SelfLearnAnalyzer",
):
    from agentscope.agent import ReActConfig
    from agentscope.tool import Toolkit
    from ..agents.context import build_scroll_components
    from ..agents.react_agent import QwenPawAgent
    from ..runtime.builder import AgentBuilder
    from ..providers.stream_diagnostics import ToolTiming, enabled

    workspace_dir = Path(workspace_dir).resolve()
    workspace_dir.mkdir(parents=True, exist_ok=True)
    config = config.model_copy(deep=True)
    lcc = config.running.light_context_config
    # Isolated runs inherit budgets but use their own history/cache paths.
    if isolate:
        lcc.dialog_path = "dialog"
        lcc.tool_result_pruning_config.tool_results_cache = "tool_results"
        lcc.scroll_config.db_filename = "history.db"
    ctx = SimpleNamespace(
        workspace=SimpleNamespace(
            workspace_dir=workspace_dir,
        ),
    )
    # Reuse the runtime's builders with isolated paths, not its live tools.
    # pylint: disable-next=protected-access
    offloader = AgentBuilder._build_offloader(ctx, config)
    scroll = (
        build_scroll_components(
            agent_config=config,
            workspace_dir=workspace_dir,
            model=model,
            session_id=session_id,
            agent_id=config.id,
            offloader=offloader,
        )
        if lcc.context_compact_config.enabled
        else None
    )
    tools = [
        *tools,
        *recovery_tools(
            workspace_dir,
            lcc.tool_result_pruning_config.pruning_recent_msg_max_bytes,
            scroll,
            (
                lcc.tool_result_pruning_config.tool_results_cache,
                lcc.dialog_path,
            ),
        ),
    ]
    prompt += (
        "\n\n工具结果可能被裁剪。read_file 只能回读已配置的结果缓存，"
        "按 next_start_char 续读时保持相同行范围；缓存行号不是原始证据行号。"
        "回读的分析结论不是新证据，最终引用仍须对应原始输入或 Harness 快照。"
    )
    if scroll is not None:
        prompt += (
            "\n较早的上下文会移入历史库。"
            "使用 recall_history 的 expand/search/recall_tool 回读；"
            "如有 next_cursor 则继续翻页。"
            "缺少当前任务或证据时先回读，不凭压缩索引猜测。"
        )
    if isolate:
        prompt += "\n历史库与缓存不包含其他案例会话或普通聊天。"
    else:
        prompt += "\n当前聊天历史仅作调试上下文，不是案例事实或 Harness 证据；" "提案引用仍须回到本批次案例和冻结快照。"

    class AnalyzeAgent(QwenPawAgent):
        def _get_stop_handlers(self) -> list:
            # Production plugins must not participate in the analysis loop.
            return []

    middlewares = [
        # pylint: disable-next=protected-access
        AgentBuilder._build_tool_result_pruning_middleware(ctx, config),
    ]
    if enabled():
        middlewares.append(ToolTiming())
    agent = AnalyzeAgent(
        name=name,
        model=model,
        system_prompt=prompt,
        toolkit=Toolkit(tools=tools),
        react_config=ReActConfig(
            max_iters=max_iters,
            interruption_raise_cancelled_error=True,
        ),
        middlewares=middlewares,
        agent_config=config,
        workspace_dir=workspace_dir,
        offloader=offloader,
        # pylint: disable-next=protected-access
        context_config=AgentBuilder._build_context_config(config),
        context_manager=scroll.context_manager if scroll is not None else None,
        effective_skills=[],
        request_context={"session_id": session_id},
    )
    agent.state.session_id = session_id
    return agent


async def analyze_episode(config, model, prompt, episode, trace_path):
    from agentscope.message import Msg, TextBlock
    from ..utils.io_utils import run_sync_io

    session_id = trace_path.stem
    trace = {"episode_id": episode["episode_id"], "reads": [], "replies": []}
    agent = await run_sync_io(
        build_analyzer,
        config,
        model,
        prompt,
        episode,
        session_id,
        trace["reads"],
        trace_path.with_suffix(""),
    )
    message = episode_prompt(episode)
    try:
        for attempt in range(2):
            response = await agent.reply(
                Msg(
                    name="user",
                    role="user",
                    content=[TextBlock(text=message)],
                ),
                structured_schema=Analysis,
            )
            trace["replies"].append(response.model_dump(mode="json"))
            if response.finished_reason == "interrupted":
                raise asyncio.CancelledError()
            try:
                result = Analysis.model_validate(response.structured_output)
                validate_evidence(result, episode)
                return result
            except ValueError as exc:
                if attempt:
                    raise
                message = (
                    "结果校验失败，请修正以下全部错误（数组索引从 0 开始）：\n"
                    f"{exc}\n"
                    "用 read_episode 回查对应字段，优先引用能够支持结论的"
                    "连续短句或单行代码；保留原文的反引号、代码围栏、空白和"
                    "diff 行首 +/-（新增空行也有 +），不要加入省略号或拼接片段。"
                    "保留已通过的引用；若找不到支持证据，应调整判断并说明证据缺口。"
                )
    finally:
        try:
            write_json(trace_path, trace)
        finally:
            await agent.close()


# Keep progress, resume and cancellation in the same run lifecycle.
# pylint: disable-next=too-many-statements
async def run_analysis(source, root, config, limit=None):
    """Yield progress for TaskTracker; resume successful analyses only."""
    from ..runtime.builder import AgentBuilder
    from ..utils.io_utils import run_sync_io

    status_path = root / "status.json"
    status = {
        "state": "running",
        "source": str(source),
        "total": 0,
        "completed": 0,
        "skipped": 0,
        "failed": 0,
        "started_at": now(),
        "current_episode": None,
        "output": None,
        "error": None,
    }
    write_json(status_path, status)
    try:
        episodes = await run_sync_io(read_jsonl, source)
        if limit is not None:
            episodes = episodes[:limit]
        for episode in episodes:
            if (
                episode.get("schema_version") != 1
                or not isinstance(episode.get("episode_id"), str)
                or not isinstance(episode.get("agent_output"), dict)
            ):
                raise ValueError("请输入预处理后的 selflearn JSONL，而非 raw 数据")
        status["total"] = len(episodes)
        prompt = instructions()
        version = digest(
            {
                "prompt": prompt,
                "code": Path(__file__).read_text(encoding="utf-8"),
                "config": config.model_dump(
                    mode="json",
                    include={
                        "active_model",
                        "thinking_level",
                        "fallback_models",
                        "fallback_policy",
                        "llm_routing",
                        "running",
                    },
                ),
            },
        )
        run_id = digest([str(source), version])[:12]
        output_dir = root / run_id
        traces = output_dir / "traces"
        traces.mkdir(parents=True, exist_ok=True)
        output = output_dir / "episode_analyses.jsonl"
        output.touch(exist_ok=True)
        status["output"] = str(output)
        write_json(
            output_dir / "manifest.json",
            {
                "schema_version": 1,
                "source": str(source),
                "analyzer_hash": version,
                "model": config.active_model.model_dump(mode="json"),
            },
        )
        previous = read_jsonl(output)
        done = {
            (row["episode_id"], row["run"]["input_hash"]) for row in previous
        }
        model = None
        for episode in episodes:
            input_hash = digest(episode)
            key = (episode["episode_id"], input_hash)
            status["current_episode"] = episode["episode_id"]
            write_json(status_path, status)
            if key in done:
                status["skipped"] += 1
            else:
                if model is None:
                    builder = AgentBuilder()
                    model, _ = await run_sync_io(
                        builder.build_model,
                        config,
                    )
                session_id = uuid4().hex
                try:
                    async with asyncio.timeout(600):
                        result = await analyze_episode(
                            config,
                            model,
                            prompt,
                            episode,
                            traces / f"{session_id}.json",
                        )
                    append_jsonl(
                        output,
                        {
                            "episode_id": episode["episode_id"],
                            **result.model_dump(mode="json"),
                            "run": {
                                "input_hash": input_hash,
                                "session_id": session_id,
                                "model": str(model.model),
                                "at": now(),
                            },
                        },
                    )
                    done.add(key)
                    status["completed"] += 1
                except Exception as exc:
                    status["failed"] += 1
                    append_jsonl(
                        output_dir / "errors.jsonl",
                        {
                            "episode_id": episode["episode_id"],
                            "session_id": session_id,
                            "error": f"{type(exc).__name__}: {exc}",
                            "at": now(),
                        },
                    )
            write_json(status_path, status)
            yield json_text(status)
        status["state"] = (
            "completed_with_errors" if status["failed"] else "completed"
        )
        status["current_episode"] = None
    except asyncio.CancelledError:
        status["state"] = "stopped"
        raise
    except Exception as exc:
        status.update(state="failed", error=str(exc))
    finally:
        status["finished_at"] = now()
        write_json(status_path, status)
