# -*- coding: utf-8 -*-
"""The /analyze entry point, backed by the workspace's task tracker."""

import json
import shlex
from pathlib import Path

from ..runtime.slash_command_registry import CommandSpec, CommandStream
from .analyzer import run_analysis
from .summarizer import run_summary

RUN_KEY = "selflearn:analyze"
HELP = (
    '用法：/analyze "JSONL 文件路径" [--limit 条数]\n'
    '/analyze summarize "episode_analyses.jsonl" --targets "目标清单.json"\n'
    "/analyze status 查看进度；/analyze stop 停止。\n"
    "相对路径从当前 Agent 的项目目录解析；未配置项目则从工作目录解析。\n"
    "两阶段都不修改 Harness；第一阶段重跑时跳过已完成案例。"
    "第二阶段在当前聊天中运行并保留历史，建议在专用调试聊天中启动。"
)


def format_status(status: dict) -> str:
    names = {
        "running": "分析中",
        "completed": "已完成",
        "completed_with_errors": "已结束，部分案例失败",
        "stopped": "已停止",
        "failed": "任务失败",
        "interrupted": "运行已中断",
    }
    lines = [names[status["state"]], f"输入：{status['source']}"]
    if status.get("phase") == "summarize":
        lines.append(
            f"第二阶段：{status['total']} 条案例，"
            f"产出 {status['proposal_count']} 项任务单。",
        )
    else:
        lines.append(
            f"本次完成 {status['completed']}，复用 {status['skipped']}，"
            f"失败 {status['failed']} / 共 {status['total']} 条。",
        )
    if status.get("current_episode"):
        lines.append(f"当前案例：{status['current_episode']}")
    if status.get("output"):
        lines.append(f"结果：{status['output']}")
    if status.get("error"):
        lines.append(f"错误：{status['error']}")
    if status.get("failed") and status.get("output"):
        lines.append(f"失败详情：{Path(status['output']).parent / 'errors.jsonl'}")
    return "\n".join(lines)


# Explicit subcommand dispatch; early replies keep each branch independent.
# pylint: disable-next=R0911,R0912,R0915
async def handle_analyze(ctx, args: str):
    from agentscope.message import Msg, TextBlock
    from ..config.config import load_agent_config
    from ..providers.provider_manager import ProviderManager
    from ..utils.io_utils import run_sync_io

    def reply(text):
        return Msg(
            name="assistant",
            role="assistant",
            content=[TextBlock(text=text)],
        )

    if (ctx.request.channel or "console") != "console":
        return reply("初版 /analyze 仅支持 QwenPaw Console，避免群成员读取本机文件。")
    if not args.strip():
        return reply(HELP)

    tracker = ctx.workspace.task_tracker
    root = Path(ctx.workspace_dir) / "selflearn"
    status_path = root / "status.json"
    status = (
        json.loads(status_path.read_text()) if status_path.exists() else {}
    )
    if status.get("run_key") and status.get("state") == "running":
        started_at = await tracker.get_run_started_at(status["run_key"])
        if started_at is None or started_at != status.get("task_started_at"):
            status["state"] = "interrupted"
    run_key = (
        status.get("run_key") or RUN_KEY
        if status.get("state") == "running"
        else RUN_KEY
    )
    if args.strip() == "stop":
        stopped = await tracker.request_stop(run_key)
        return reply("分析已停止，已完成结果保留。" if stopped else "没有正在运行的分析。")
    if args.strip() == "status":
        if not status_path.exists():
            return reply("尚未运行分析。\n" + HELP)
        if (
            status["state"] == "running"
            and await tracker.get_status(run_key) == "idle"
        ):
            status["state"] = "interrupted"
        return reply(format_status(status))
    if await tracker.get_status(RUN_KEY) == "running" or (
        status.get("state") == "running"
        and await tracker.get_status(run_key) == "running"
    ):
        return reply("已有分析正在运行。使用 /analyze status 或 /analyze stop。")

    try:
        tokens = shlex.split(args)
        target_arg = None
        if tokens[0] == "summarize":
            if len(tokens) != 4 or tokens[2] != "--targets":
                raise ValueError(HELP)
            source_arg, target_arg, limit = tokens[1], tokens[3], None
        else:
            if len(tokens) not in (1, 3) or (
                len(tokens) == 3 and tokens[1] != "--limit"
            ):
                raise ValueError(HELP)
            source_arg = tokens[0]
            limit = int(tokens[2]) if len(tokens) == 3 else None
        if limit is not None and limit < 1:
            raise ValueError("--limit 必须为正整数")
        config = await run_sync_io(load_agent_config, ctx.agent_id)
        base = Path(config.project_dir or ctx.workspace_dir)
        source = (base / Path(source_arg).expanduser()).resolve()
        if source.suffix != ".jsonl" or not source.is_file():
            raise ValueError(f"找不到 JSONL 文件：{source}")
        targets = (
            (base / Path(target_arg).expanduser()).resolve()
            if target_arg
            else None
        )
        if targets is not None and not targets.is_file():
            raise ValueError(f"找不到 Harness 目标清单：{targets}")
        active = (
            config.active_model
            or ProviderManager.get_instance().get_active_model()
        )
        if not active or not active.provider_id or not active.model:
            raise ValueError("请先在 QwenPaw 中配置可用模型")
        config = config.model_copy(deep=True, update={"active_model": active})
    except (ValueError, OSError) as exc:
        return reply(str(exc))

    root.mkdir(parents=True, exist_ok=True)
    if targets is not None:
        ctx.extras[
            "selflearn_run_key"
        ] = await ctx.workspace.chat_manager.get_chat_id_by_session(
            ctx.session_id,
            "console",
            ctx.request.user_id,
        )
        return CommandStream(
            _summary_in_chat(ctx, source, targets, root, config),
        )

    async def stream(_payload):
        async for progress in run_analysis(source, root, config, limit):
            yield f"data: {progress}\n\n"

    queue, started = await tracker.attach_or_start(
        RUN_KEY,
        None,
        stream,
        owner=ctx.workspace,
    )
    await tracker.detach_subscriber(RUN_KEY, queue)
    if not started:
        return reply("已有分析正在运行。使用 /analyze status 查看进度。")
    return reply(
        f"已启动第一轮分析：{source}\n"
        + (f"仅处理前 {limit} 条。\n" if limit else "")
        + f"结果保存在 {root} 下的批次目录。\n"
        "使用 /analyze status 查看进度和结果路径；/analyze stop 停止。",
    )


async def _summary_in_chat(ctx, source, targets, root, config):
    from agentscope.message import Msg, TextBlock

    async for event in run_summary(source, targets, root, config, chat=ctx):
        if isinstance(event, str):
            event = Msg(
                name="assistant",
                role="assistant",
                content=[
                    TextBlock(
                        text=format_status(json.loads(event)),
                    ),
                ],
            )
        yield event
    status = json.loads((root / "status.json").read_text())
    text = format_status(status)
    if status["state"] == "completed":
        output = json.loads(Path(status["output"]).read_text())
        text += (
            "\n\n```json\n"
            + json.dumps(
                output,
                ensure_ascii=False,
                indent=2,
            )
            + "\n```"
        )
    message = Msg(
        name="assistant",
        role="assistant",
        content=[TextBlock(text=text)],
    )
    if ctx.agent is not None:
        await ctx.agent.observe(message)
    yield message


def analyze_command_spec() -> CommandSpec:
    return CommandSpec(
        name="analyze",
        handler=handle_analyze,
        category="selflearn",
        help_text="Analyze feedback from a selflearn JSONL file",
    )
