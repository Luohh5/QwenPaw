# -*- coding: utf-8 -*-
"""Console entry for candidate generation and local version operations."""

import argparse
import json
import shlex
from pathlib import Path

from ..runtime.slash_command_registry import CommandSpec
from ..utils.io_utils import get_path_lock, run_sync_io
from .analyzer import now, write_json
from .optimizer import load_task, run_optimization
from .versions import find_candidate, read_json

RUN_KEY = "selflearn:optimize"
HELP = """用法：
/optimize "improvement_proposals.json" [--proposal 1]
  [--base auto|snapshot|候选ID|完整SHA]
  [--targets "目标清单.json"] [--also-target 目标ID]
/optimize status | stop | list
/optimize show 候选ID
/optimize diff 候选ID [另一个候选ID]
/optimize merge 候选A 候选B
/optimize pick 候选A --base 候选B
/optimize revert 候选ID [--base 候选ID]
/optimize save 候选ID     人工解决冲突/修改草稿后检查并保存
/optimize reject 候选ID   放弃候选，保留历史和草稿
/optimize clean 候选ID    仅移除无未保存改动的工作目录，保留版本历史

提案序号从 1 开始；默认 auto，由 Agent 查阅历史选择起点，不自动叠加最新候选。
显式 --base 指定初始起点；Agent 仍可保存/放弃尝试后换路线，最终只选一个候选。
相对文件路径从当前 Agent 的项目目录解析，未配置项目则从工作目录解析。
后台使用独立会话、当前模型及 thinking 设置；不会发布、push 或修改原项目。
"""


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message + "\n" + HELP)


def format_status(status):
    names = {
        "running": "修改中",
        "ready_for_evaluation": "候选已保存，等待评测（未发布）",
        "stopped": "已停止，草稿保留",
        "interrupted": "运行中断，草稿保留",
        "failed": "失败",
        "conflict": "合并/撤销未完成，请检查冲突或错误",
        "rejected": "已放弃，记录保留",
        "checkpoint": "探索检查点已保存，尚未选为最终候选",
        "not_selected": "未选用的尝试，记录保留",
        "no_change": "无需修改",
        "needs_evidence": "需要更多证据",
        "needs_scope": "需要额外授权或工程处理",
    }
    lines = [names.get(status.get("state", status.get("status")), "未知状态")]
    for field, label in (
        ("candidate_id", "候选"),
        ("selected_candidate_id", "最终选择"),
        ("base_revision", "起点"),
        ("candidate_revision", "版本"),
        ("output", "结果"),
        ("run_output", "运行记录"),
        ("worktree", "工作目录"),
        ("summary", "说明"),
        ("error", "错误"),
    ):
        if status.get(field):
            lines.append(f"{label}：{status[field]}")
    return "\n".join(lines)


def version_command(root, tokens):
    action = tokens[0]
    if action == "list" and len(tokens) == 1:
        rows = [
            read_json(p)
            for p in (root / "harnesses").glob(
                "*/experiments/*/candidate.json",
            )
        ]
        return (
            "\n\n".join(
                format_status(r)
                for r in sorted(
                    rows,
                    key=lambda r: r["created_at"],
                    reverse=True,
                )
            )
            or "尚无候选版本。"
        )
    counts = {
        "show": (2,),
        "diff": (2, 3),
        "merge": (3,),
        "pick": (4,),
        "revert": (2, 4),
        "save": (2,),
        "reject": (2,),
        "clean": (2,),
    }
    if action not in counts or len(tokens) not in counts[action]:
        raise ValueError(HELP)
    store, row = find_candidate(root, tokens[1])
    if action == "show":
        return (
            "```json\n"
            + json.dumps(row, ensure_ascii=False, indent=2)
            + "\n```"
        )
    if action == "diff":
        return (
            "```diff\n"
            + store.diff(row, tokens[2] if len(tokens) == 3 else None)
            + "\n```"
        )
    if action == "merge" or (
        action in {"pick", "revert"} and len(tokens) == 4
    ):
        if action != "merge" and tokens[2] != "--base":
            raise ValueError(HELP)
        other_store, other = find_candidate(root, tokens[-1])
        if other_store.root != store.root:
            raise ValueError("只能操作同一个 Harness 的候选")
        row = (
            store.integrate(row, other)
            if action == "merge"
            else store.integrate(
                other,
                row,
                operation=action,
            )
        )
    elif action == "revert":
        row = store.integrate(row, operation="revert")
    elif action == "save":
        row = store.commit(row, "Save reviewed draft " + row["candidate_id"])
    elif action == "reject":
        row = store.reject(row)
    elif action == "clean":
        store.clean(row)
        return "已移除干净的候选工作目录；commit、分支和实验记录保留，可从该候选再次开始。"
    return format_status(row)


# Explicit subcommand dispatch; early replies keep each branch independent.
# pylint: disable-next=R0911,R0912,R0915
async def handle_optimize(ctx, args):
    from agentscope.message import Msg, TextBlock
    from ..config.config import load_agent_config
    from ..providers.provider_manager import ProviderManager

    def reply(text):
        return Msg(
            name="assistant",
            role="assistant",
            content=[TextBlock(text=text)],
        )

    if (ctx.request.channel or "console") != "console":
        return reply("初版 /optimize 仅支持 QwenPaw Console。")
    if not args.strip():
        return reply(HELP)
    root = Path(ctx.workspace_dir) / "selflearn"
    tracker = ctx.workspace.task_tracker
    async with get_path_lock(root / "optimize_status.json"):
        try:
            tokens = shlex.split(args)
            running = await tracker.get_status(RUN_KEY) == "running"
            if tokens == ["stop"]:
                stopped = await tracker.request_stop(RUN_KEY)
                return reply(
                    ("已请求停止，草稿和日志保留。" if stopped else "没有正在运行的优化。"),
                )
            if tokens == ["status"]:
                path = root / "optimize_status.json"
                if not path.exists():
                    return reply("尚未运行 Optimize。\n" + HELP)
                status = await run_sync_io(read_json, path)
                if status["state"] == "running" and not running:
                    status.update(state="interrupted", finished_at=now())
                    write_json(path, status)
                    if status.get("candidate_id"):
                        store, row = await run_sync_io(
                            find_candidate,
                            root,
                            status["candidate_id"],
                        )
                        if row["status"] == "running":
                            row.update(status="interrupted", finished_at=now())
                            store.save(row)
                    if status.get("run_output"):
                        write_json(Path(status["run_output"]), status)
                return reply(format_status(status))
            if tokens[0] in {
                "list",
                "show",
                "diff",
                "merge",
                "pick",
                "revert",
                "save",
                "reject",
                "clean",
            }:
                if running and tokens[0] not in {"list", "show", "diff"}:
                    return reply(
                        "请先等待当前优化结束或 /optimize stop，再管理候选版本。",
                    )
                return reply(await run_sync_io(version_command, root, tokens))
            if running:
                return reply(
                    "已有优化正在运行。使用 /optimize status 或 /optimize stop。",
                )
            parser = Parser(add_help=False)
            parser.add_argument("source")
            parser.add_argument("--proposal", type=int, default=1)
            parser.add_argument("--base", default="auto")
            parser.add_argument("--targets")
            parser.add_argument("--also-target", action="append", default=[])
            options = parser.parse_args(tokens)
            config = await run_sync_io(load_agent_config, ctx.agent_id)
            project = Path(config.project_dir or ctx.workspace_dir)
            source = (project / Path(options.source).expanduser()).resolve()
            targets = (
                (project / Path(options.targets).expanduser()).resolve()
                if options.targets
                else None
            )
            # Fail unsupported/status/scope inputs before starting a model run.
            await run_sync_io(
                load_task,
                source,
                options.proposal,
                targets,
                options.also_target,
            )
            active = (
                config.active_model
                or ProviderManager.get_instance().get_active_model()
            )
            if not active or not active.provider_id or not active.model:
                raise ValueError("请先在 QwenPaw 中配置可用模型")
            config = config.model_copy(
                deep=True,
                update={"active_model": active},
            )

            async def stream(_payload):
                async for status in run_optimization(
                    source,
                    root,
                    config,
                    options.proposal,
                    options.base,
                    targets,
                    options.also_target,
                ):
                    yield f"data: {status}\n\n"

            queue, started = await tracker.attach_or_start(
                RUN_KEY,
                None,
                stream,
                owner=ctx.workspace,
            )
            await tracker.detach_subscriber(RUN_KEY, queue)
            return reply(
                (
                    f"已启动 Optimize：提案 {options.proposal}，起点 {options.base}。\n"
                    "使用 /optimize status 查看候选和结果；/optimize stop 停止。\n"
                    "只生成本地候选，不修改原项目、不发布。"
                    if started
                    else "已有优化正在运行。"
                ),
            )
        except (ValueError, OSError, KeyError) as exc:
            return reply(str(exc))


def optimize_command_spec():
    return CommandSpec(
        name="optimize",
        handler=handle_optimize,
        category="selflearn",
        help_text="Build and manage local Harness candidates",
    )
