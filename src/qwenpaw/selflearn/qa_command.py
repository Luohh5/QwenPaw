"""Console and terminal entry points for the QA knowledge workflow."""

import argparse
import asyncio
import json
import shlex
from pathlib import Path

from ..exceptions import ConfigurationException
from .qa_workflow import run_workflow
from .qa_sources import default_repo


def run_qa(**kwargs):
    return run_workflow(workflow="qa", **kwargs)


RUN_KEY = "selflearn:qa"
HELP = """/selflearn qa "日期范围.jsonl" [--name 日期范围] [--concurrency 3]
/selflearn qa-skip "日期范围.txt" [--all | --topic 主题ID] [--accept-edited-txt]
/selflearn eval-init --alias-root "Alias 项目/alias" --collection qwenpaw_faq_old
  --model qwen3.8-max --thinking false [--cases 题集.jsonl] [--references 标准目录]
/selflearn prepare "日期范围.txt" [--name 日期范围]
/selflearn batch --collection 库名 [--name 回答文件名] [--model 模型名]
/selflearn score "回答.jsonl" [--references 标准目录] [--name 评分目录名]
  [--concurrency 4]
/selflearn evaluate "日期范围.txt" [--name 日期范围] [--concurrency 4]
/selflearn train_eval "日期范围.txt" [--old-collection 旧库 --new-collection 新库]
  [--name 评测名称] [--concurrency 4]
/selflearn status
/selflearn stop
所有步骤默认在当前 session 可见；过程同时保存为一份 session.md。
qa：原始 A.jsonl、A_train.jsonl、A_test.jsonl 位于 analyze/history_jsonl/A/；
约 8:2 切分后，训练历史生成 TXT 与测试标准生成独立进行。
evaluate：generalized 和 specialized 成绩都不下降且无新增关键错误才更新基线。
batch：工作区 eval 下只保存回答 JSONL。
score：selflearn/score/回答文件名 下保存评分、报告和 session。
score/index.json 登记成绩；selflearn/baseline.json 指向当前基线。
evaluate 自动建库、复用基线回答和有效评分、评新库，再比较。
同参数再次执行会续跑；需要另开版本时使用 --name 日期范围_v2。
--background 可选；密钥继承环境变量。不会上传或修改服务器。"""


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def parser():
    result = Parser(add_help=False)
    result.add_argument("mode", choices=["qa"])
    result.add_argument("source")
    result.add_argument("--output")
    result.add_argument("--name")
    result.add_argument(
        "--repo", default=str(default_repo()) if default_repo() else None
    )
    result.add_argument("--knowledge")
    result.add_argument("--skills-dir")
    result.add_argument("--limit", type=int)
    result.add_argument("--max-topics", type=int)
    result.add_argument("--offline", action="store_true")
    result.add_argument("--concurrency", type=int, default=3)
    result.add_argument("--background", action="store_true")
    return result


def options(tokens, base):
    if tokens and tokens[0] == "qa-skip":
        cli = Parser(add_help=False)
        cli.add_argument("source")
        cli.add_argument("--accept-edited-txt", action="store_true")
        group = cli.add_mutually_exclusive_group()
        group.add_argument("--all", dest="skip_all", action="store_true")
        group.add_argument("--topic", action="append", default=[])
        values = vars(cli.parse_args(tokens[1:]))
        values["source"] = (
            Path(base) / Path(values["source"]).expanduser()
        ).resolve()
        return {"workflow": "qa-skip", **values}
    if tokens and tokens[0] == "train_eval":
        cli = Parser(add_help=False)
        cli.add_argument("source")
        cli.add_argument("--old-collection")
        cli.add_argument("--new-collection")
        cli.add_argument("--name")
        cli.add_argument("--concurrency", type=int, default=4)
        cli.add_argument("--skills-dir")
        cli.add_argument("--background", action="store_true")
        values = vars(cli.parse_args(tokens[1:]))
        if bool(values["old_collection"]) != bool(values["new_collection"]):
            raise ValueError(
                "请同时指定旧库和新库，或同时省略以使用本批原比较记录"
            )
        if not 1 <= values["concurrency"] <= 8:
            raise ValueError("--concurrency 必须为 1–8")
        for key in ("source", "skills_dir"):
            if values[key]:
                values[key] = (
                    Path(base) / Path(values[key]).expanduser()
                ).resolve()
        return {"workflow": "train_eval", **values}
    if tokens and tokens[0] in {"prepare", "batch", "score"}:
        cli = Parser(add_help=False)
        mode = tokens[0]
        paths = []
        if mode == "batch":
            cli.add_argument("--collection", required=True)
            cli.add_argument("--model", dest="model_name")
        else:
            cli.add_argument("source")
            paths.append("source")
        cli.add_argument("--name")
        cli.add_argument("--background", action="store_true")
        if mode == "score":
            cli.add_argument("--references")
            cli.add_argument("--skills-dir")
            cli.add_argument("--concurrency", type=int, default=4)
            paths.extend(["references", "skills_dir"])
        values = vars(cli.parse_args(tokens[1:]))
        for name in paths:
            if values[name]:
                values[name] = (
                    Path(base) / Path(values[name]).expanduser()
                ).resolve()
        if not 1 <= values.get("concurrency", 1) <= 8:
            raise ValueError("--concurrency 必须为 1–8")
        return {"workflow": mode, **values}
    if tokens and tokens[0] in {"evaluate", "eval-init"}:
        from .qa_sources import default_repo

        cli = Parser(add_help=False)
        mode = tokens[0]
        if mode == "evaluate":
            cli.add_argument("source")
            cli.add_argument("--resume")
            cli.add_argument("--name")
            cli.add_argument("--concurrency", type=int, default=4)
            cli.add_argument("--skills-dir")
            cli.add_argument("--background", action="store_true")
            paths = ("source", "resume", "skills_dir")
        else:
            cli.add_argument("--alias-root", required=True)
            cli.add_argument("--collection", required=True)
            cli.add_argument("--model", required=True)
            cli.add_argument(
                "--thinking", choices=["true", "false"], required=True
            )
            cli.add_argument("--cases")
            cli.add_argument("--references")
            cli.add_argument("--baseline-answers")
            cli.add_argument(
                "--repo",
                default=str(default_repo()) if default_repo() else None,
            )
            paths = (
                "alias_root",
                "cases",
                "references",
                "baseline_answers",
                "repo",
            )
        values = vars(cli.parse_args(tokens[1:]))
        if not 1 <= values.get("concurrency", 1) <= 8:
            raise ValueError("--concurrency 必须为 1–8")
        for name in paths:
            if values[name]:
                values[name] = (
                    Path(base) / Path(values[name]).expanduser()
                ).resolve()
        return {"workflow": mode, **values}
    args = vars(parser().parse_args(tokens))
    args.pop("mode")
    if not 1 <= args["concurrency"] <= 8:
        raise ValueError("--concurrency 必须为 1–8")
    for name in ("limit", "max_topics"):
        if args[name] is not None and args[name] < 1:
            raise ValueError(f"--{name.replace('_', '-')} 必须是正整数")
    for name in ("source", "output", "repo", "knowledge", "skills_dir"):
        if args[name]:
            args[name] = (Path(base) / Path(args[name]).expanduser()).resolve()
    if not args["source"].is_file() or args["source"].suffix != ".jsonl":
        raise ValueError(f"找不到 JSONL：{args['source']}")
    if args["output"] and args["output"].suffix.lower() != ".txt":
        raise ValueError("--output 必须是 .txt 文件")
    for name in ("repo", "skills_dir"):
        if args[name] and not args[name].is_dir():
            raise ValueError(f"找不到目录：{args[name]}")
    if args["knowledge"] and not args["knowledge"].is_file():
        raise ValueError(f"找不到知识文件：{args['knowledge']}")
    return args


def format_status(status):
    if status.get("event") == "session":
        return status["text"]
    if status.get("event") == "activity":
        return (
            f"{status['activity']} · {status['label']}\n"
            f"{status.get('detail') or ''}\n"
            f"本步骤已用 {status['elapsed_seconds']} 秒。"
        )
    if status.get("branches"):
        return (
            f"TXT：{status['branches'].get('txt')}；"
            f"测试标准：{status['branches'].get('benchmark')}\n"
            f"{status.get('output') or ''}\n"
            + "\n".join(status.get("errors", {}).values())
        )
    if status.get("phase") == "benchmark" and status.get("progress"):
        return f"准备专用测试标准\n{status['progress']}\n{status.get('current', '')}"
    if status.get("phase") == "evaluate":
        return "\n".join(
            str(status[k])
            for k in ("state", "current", "run_dir", "output", "error")
            if status.get(k)
        )
    phases = {
        "prepare": "准备输入",
        "split": "切分训练与测试历史",
        "benchmark": "准备专用测试标准",
        "analyze": "分析问答",
        "propose": "归纳知识任务",
        "research": "查证答案",
        "verify": "核验条目",
        "done": "导出结束",
    }
    phase = phases.get(status.get("phase"), status.get("phase", ""))
    lines = [
        f"{status['state']} · {phase}",
        f"分析完成 {status.get('completed', 0)}，复用 {status.get('reused', 0)}，"
        f"失败 {status.get('failed', 0)}；导出 {status.get('exported', 0)} 条。",
    ]
    for field, label in (
        ("current", "当前"),
        ("output", "TXT"),
        ("run_dir", "过程记录"),
        ("error", "错误"),
    ):
        if status.get(field):
            lines.append(f"{label}：{status[field]}")
    if status.get("phase") == "done":
        lines.append(
            f"待处理 {status.get('pending', 0)}，"
            f"未进入本轮生成 {status.get('deferred', 0)}。"
        )
        if not status.get("exported"):
            lines.append(
                "没有通过核验的新增知识，TXT 为空；原因见当前 session 的分析与核验结论。"
            )
    return "\n".join(lines)


async def handle_selflearn(ctx, args):
    from agentscope.message import Msg, TextBlock
    from ..config.config import load_agent_config
    from ..providers.provider_manager import ProviderManager

    def reply(text):
        return Msg(
            name="assistant", role="assistant", content=[TextBlock(text=text)]
        )

    if (ctx.request.channel or "console") != "console":
        return reply("/selflearn 仅支持 QwenPaw Console。")
    if not args.strip() or args.strip() in {"help", "--help"}:
        return reply(HELP)
    root = Path(ctx.workspace_dir) / "selflearn"
    tracker = ctx.workspace.task_tracker
    if args.strip() == "stop":
        stopped = await tracker.request_stop(RUN_KEY)
        return reply(
            "已请求停止，完成的分析和候选保留。"
            if stopped
            else "没有运行中的答疑学习任务。"
        )
    if args.strip() == "status":
        path = root / "qa_status.json"
        if not path.exists():
            return reply("尚未运行答疑学习。\n" + HELP)
        status = json.loads(path.read_text(encoding="utf-8"))
        if (
            status["state"] == "running"
            and await tracker.get_status(RUN_KEY) != "running"
        ):
            status["state"] = "interrupted"
        return reply(format_status(status))
    if await tracker.get_status(RUN_KEY) == "running":
        return reply(
            "已有答疑学习任务运行中，使用 /selflearn status 或 stop。"
        )
    try:
        config = await asyncio.to_thread(load_agent_config, ctx.agent_id)
        settings = options(
            shlex.split(args), config.project_dir or ctx.workspace_dir
        )
        workflow = settings.pop("workflow", "qa")
        background = settings.pop("background", False)
        if workflow == "qa-skip":
            from .qa_skip import skip_topics

            return reply(
                await asyncio.to_thread(skip_topics, root, **settings)
            )
        if workflow == "eval-init":
            from .qa_evaluate import initialize

            path = await asyncio.to_thread(initialize, root, **settings)
            return reply(
                f"评测配置已保存：{path}\n"
                '使用 /selflearn evaluate "A.txt" 开始。密钥继承当前环境变量。'
            )
        active = (
            config.active_model
            or ProviderManager.get_instance().get_active_model()
        )
        if not active or not active.provider_id or not active.model:
            raise ValueError("请先配置可用模型")
        config = config.model_copy(deep=True, update={"active_model": active})
    except (ValueError, OSError, ConfigurationException) as exc:
        return reply(
            str(exc)
            if args.lstrip().startswith("qa-skip ")
            else str(exc) + "\n" + HELP
        )

    async def stream(_payload):
        from .qa_evaluate import run_evaluate

        settings["live"] = not background
        if workflow == "qa":
            runner = run_qa
        elif workflow == "evaluate" and settings.get("resume"):
            settings.pop("name", None)
            settings.pop("concurrency", None)
            runner = run_evaluate
        else:
            settings.pop("resume", None)
            runner = run_workflow
            settings["workflow"] = workflow
        async for event in runner(root=root, config=config, **settings):
            yield f"data: {event}\n\n"

    queue, started = await tracker.attach_or_start(
        RUN_KEY, None, stream, owner=ctx.workspace
    )
    if not background and started:
        from ..runtime.slash_command_registry import CommandStream

        return CommandStream(_qa_in_chat(ctx, config, tracker, queue))
    await tracker.detach_subscriber(RUN_KEY, queue)
    return reply(
        f"已启动答疑学习/评测：{settings.get('source') or settings.get('collection')}\n"
        "使用 /selflearn status 查看进度和结果路径；/selflearn stop 停止。"
        if started
        else "已有答疑学习任务运行中。"
    )


async def _make_chat_recorder(ctx, config):
    from ..hooks.session.session_hook import SessionLoadHook
    from ..runtime.builder import AgentBuilder
    from .analyzer import build_readonly_agent

    await SessionLoadHook().run(ctx)
    model, _ = await asyncio.to_thread(AgentBuilder().build_model, config)
    agent = await asyncio.to_thread(
        build_readonly_agent,
        config,
        model,
        "此会话记录答疑学习的进度和结果。分析在独立上下文执行。",
        ctx.session_id,
        [],
        workspace_dir=ctx.workspace_dir,
        isolate=False,
        name="SelfLearnProgress",
    )
    ctx.agent = agent
    ctx.agent_config = config
    if ctx.session_state:
        agent.load_state_dict(ctx.session_state)
    agent.state.session_id = ctx.session_id
    await agent.observe(ctx.input_msgs)
    return agent


async def _qa_in_chat(ctx, config, tracker, queue):
    from agentscope.message import Msg, TextBlock

    finished = False
    phase = None
    try:
        recorder = await _make_chat_recorder(ctx, config)
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=20)
            except asyncio.TimeoutError:
                continue
            else:
                if event is None:
                    finished = True
                    break
                if not event.startswith("data: "):
                    continue
                status = json.loads(event[6:].strip())
                phase = status.get("phase", phase)
                if (
                    status.get("event") == "activity"
                    or status.get("display") is False
                    or (
                        phase
                        in {
                            "analyze",
                            "split",
                            "benchmark",
                            "branches",
                            "evaluate",
                            "score",
                            "batch",
                        }
                        and status.get("state") == "running"
                    )
                ):
                    continue
                if status.get("event") == "replay_end":
                    continue
                if "state" not in status and status.get("event") not in {
                    "activity",
                    "session",
                }:
                    text = str(status.get("error", status))
                else:
                    text = format_status(status)
            message = Msg(
                name="assistant",
                role="assistant",
                content=[TextBlock(text=text)],
            )
            await recorder.observe(message)
            yield message
    finally:
        if not finished:
            await tracker.request_stop(RUN_KEY)
        await tracker.detach_subscriber(RUN_KEY, queue)


def selflearn_command_spec():
    from ..runtime.slash_command_registry import CommandSpec

    return CommandSpec(
        name="selflearn",
        handler=handle_selflearn,
        category="selflearn",
        help_text="Analyze QA history, export FAQ TXT and evaluate additions",
    )


def main():
    """Run with an existing agent model, without restarting a service."""
    import sys
    from ..config.config import load_agent_config

    cli = argparse.ArgumentParser(
        description=HELP, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    cli.add_argument("--agent", default="default")
    cli.add_argument("--work-dir", type=Path, default=None)
    cli.add_argument("arguments", nargs=argparse.REMAINDER)
    args = cli.parse_args()

    async def run():
        settings = options(args.arguments, Path.cwd())
        workflow = settings.pop("workflow", "qa")
        settings.pop("background", None)
        config = load_agent_config(args.agent)
        from ..config.utils import load_config

        root = (
            args.work_dir.resolve()
            if args.work_dir
            else Path(
                load_config().agents.profiles[args.agent].workspace_dir
            ).expanduser()
            / "selflearn"
        )
        if workflow == "qa-skip":
            from .qa_skip import skip_topics

            print(skip_topics(root, **settings))
            return 0
        if workflow == "eval-init":
            from .qa_evaluate import initialize

            print(initialize(root, **settings))
            return 0
        config = load_agent_config(args.agent)
        if workflow in {"evaluate", "score", "qa", "train_eval"}:
            from ..providers.provider_manager import ProviderManager

            active = (
                config.active_model
                or ProviderManager.get_instance().get_active_model()
            )
            if not active or not active.provider_id or not active.model:
                raise ValueError("请先为 QwenPaw 配置评分模型")
            config = config.model_copy(
                deep=True, update={"active_model": active}
            )
        from .qa_evaluate import run_evaluate

        if workflow == "qa":
            runner = run_qa
        elif workflow == "evaluate" and settings.get("resume"):
            settings.pop("name", None)
            settings.pop("concurrency", None)
            runner = run_evaluate
        else:
            runner = run_workflow
            settings.pop("resume", None)
            settings["workflow"] = workflow
        final = {}
        async for event in runner(root=root, config=config, **settings):
            final = json.loads(event)
            print(format_status(final), flush=True)
        return 0 if final.get("state") == "completed" else 1

    try:
        sys.exit(asyncio.run(run()))
    except KeyboardInterrupt:
        sys.exit(130)
    except (ValueError, OSError, ConfigurationException) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
