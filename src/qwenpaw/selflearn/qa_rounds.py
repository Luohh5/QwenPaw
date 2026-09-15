"""A frozen history round and its generalized/specialized comparison."""

import asyncio
import json
from contextlib import aclosing
from pathlib import Path

from . import qa_pipeline
from .analyzer import digest
from .qa_benchmark import build_benchmark, check_benchmark, overlap_report
from .qa_data import load_episodes
from .qa_datasets import name_for, prepare_split, check_split, read
from .qa_eval_data import compare, file_hash, load_rows
from .qa_storage import ArtifactStore


def checked_round(root, source):
    source = Path(source).resolve()
    matches = []
    for p in (root.parent / "analyze/history_jsonl").glob("*/round.json"):
        value = read(p)
        if value.get("txt", {}).get("path") == str(source):
            matches.append((p, value))
    if len(matches) != 1:
        raise ValueError(
            "TXT 没有唯一的训练/测试切分记录；请先运行新版 /selflearn qa，旧 TXT 请用新批次重做"
        )
    path, value = matches[0]
    if not value.get("ready_for_evaluation", True):
        raise ValueError(
            "本批 TXT 或测试标准尚未完成；请继续 /selflearn qa 后再评测"
        )
    if file_hash(source) != value["txt"]["sha256"]:
        raise ValueError("TXT 已改变，请使用新的批次名称")
    split = check_split(path.parent)
    benchmark = check_benchmark(path.parent / "specialized")
    if (
        file_hash(path.parent / "split.json") != value["split_sha256"]
        or file_hash(path.parent / "specialized/manifest.json")
        != value["benchmark_sha256"]
        or benchmark["split_sha256"] != value["split_sha256"]
    ):
        raise ValueError("本轮切分、测试标准和 TXT 的关联不一致")
    return path, value, split, benchmark


async def analyze(root, source, config, name=None, **kwargs):
    from ..runtime.builder import AgentBuilder
    from . import qa_workflow as flow, qa_compact

    root = Path(root)
    name = name_for(source, name)
    profile = flow.profile_for(root)
    output = Path(
        kwargs.get("output") or root.parent / "analyze/txt" / (name + ".txt")
    ).resolve()
    folder = root.parent / "analyze/history_jsonl" / name
    saved = flow.read(folder / "round.json")
    if saved and saved.get("ready_for_evaluation", True):
        _, _, split, _ = checked_round(root, saved["txt"]["path"])
        if file_hash(source) != split["original"]["sha256"]:
            raise ValueError("原始历史已改变，请使用新批次")
        state = {
            "state": "completed",
            "phase": "done",
            "output": saved["txt"]["path"],
            "exported": saved.get("exported", 0),
            "reused_historical_export": True,
        }
        flow.transcript_event("复用已完成批次", str(output))
        flow.write_json(root / "qa_status.json", state)
        yield json.dumps(state, ensure_ascii=False)
        return
    if output.exists() and not (folder / "split.json").exists():
        raise ValueError("已有 TXT 没有对应切分记录，请使用新的批次名称")
    concurrency = kwargs.get("concurrency", 3)
    if not 1 <= concurrency <= 8:
        raise ValueError("concurrency 必须为 1–8")
    semaphore = asyncio.Semaphore(concurrency)
    model = kwargs.pop("model", None)
    if model is None:
        model, _ = await asyncio.to_thread(AgentBuilder().build_model, config)
    state = {"state": "running", "phase": "split", "current": name}
    flow.write_json(root / "qa_status.json", state)
    yield json.dumps(state, ensure_ascii=False)
    transcript = root.parent / "analyze/sessions" / (name + ".md")
    store = ArtifactStore(
        root.parent / "analyze/.state" / (name + "-dataset.sqlite"), transcript
    )
    with store.working():
        folder, split = await prepare_split(
            root, source, config, model, store, name, kwargs.get("skills_dir")
        )
        flow.transcript_event(
            "切分完成",
            f"训练 {len(split['train']['record_ids'])} 条，"
            f"测试 {len(split['test']['record_ids'])} 条。",
        )
        state.update(
            phase="branches",
            branches={"txt": "running", "benchmark": "running"},
        )
        flow.write_json(root / "qa_status.json", state)
        yield json.dumps(state, ensure_ascii=False)
        flow.transcript_event(
            "分别生成知识和测试标准",
            "两条支线独立进行；TXT 生成后即可查看，评测需等待两边完成。",
        )

        async def training():
            return await qa_compact.generate(
                folder / split["train"]["file"],
                output,
                config,
                model,
                store,
                semaphore=semaphore,
                repo=kwargs.get("repo") or profile.get("repo"),
                offline=kwargs.get("offline", False),
                skills_dir=kwargs.get("skills_dir"),
                knowledge=kwargs.get("knowledge"),
                limit=kwargs.get("limit"),
                max_topics=kwargs.get("max_topics"),
            )

        async def benchmark():
            path, manifest = await build_benchmark(
                folder,
                split,
                profile,
                config,
                model,
                store,
                repo=kwargs.get("repo") or profile.get("repo"),
                offline=kwargs.get("offline", False),
                skills_dir=kwargs.get("skills_dir"),
                concurrency=concurrency,
                semaphore=semaphore,
            )
            return {"path": str(path), "manifest": manifest}

        results, errors = {}, {}

        async def branch(label, call):
            try:
                value = await call()
                results[label] = value
                state["branches"][label] = value.get("state", "completed")
            except Exception as exc:
                errors[label] = str(exc) or type(exc).__name__
                state["branches"][label] = "failed"
                flow.transcript_event(
                    "TXT 支线失败" if label == "txt" else "测试标准支线失败",
                    errors[label],
                )
            flow.write_json(root / "qa_status.json", state)

        tasks = [
            asyncio.create_task(branch("txt", training)),
            asyncio.create_task(branch("benchmark", benchmark)),
        ]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        txt = results.get("txt", {})
        ref = results.get("benchmark", {})
        ready = txt.get("state") == "completed" and bool(ref)
        record = {
            "schema_version": 2,
            "name": name,
            "split_sha256": file_hash(folder / "split.json"),
            "ready_for_evaluation": ready,
            "branches": dict(state["branches"]),
            "errors": errors,
            "exported": txt.get("exported", 0),
            "train_limit": kwargs.get("limit"),
            "skipped_topics": list(store.get("compact_skipped", {}).values()),
        }
        if txt.get("output"):
            record["txt"] = {"path": str(output), "sha256": file_hash(output)}
            record["training_state"] = txt["state"]
        if ref:
            references = Path(ref["path"])
            record.update(
                benchmark_sha256=file_hash(references / "manifest.json"),
                references=str(references),
                cases=str(references / ref["manifest"]["cases_file"]),
            )
        flow.write_json(folder / "round.json", record)
        state.update(
            state="completed" if ready else "completed_with_errors",
            phase="done",
            output=txt.get("output"),
            exported=txt.get("exported", 0),
            ready_for_evaluation=ready,
            errors=errors,
            run_dir=str(transcript),
        )
        flow.transcript_event(
            "本轮处理结果",
            f"TXT：{state['branches']['txt']}；"
            f"测试标准：{state['branches']['benchmark']}。"
            + (
                "可以开始评测。"
                if ready
                else "已完成部分保留，重发原命令继续未完成支线。"
            ),
        )
        flow.write_json(root / "qa_status.json", state)
        yield json.dumps(state, ensure_ascii=False)


def dual_decision(suites, added_chunks):
    return bool(
        added_chunks > 0
        and all(
            s["baseline"]["count"] > 0
            and s["delta"] is not None
            and s["delta"] >= 0
            and not s["new_major_errors"]
            for s in suites.values()
        )
        and set(suites) == {"generalized", "specialized"}
    )


def report_text(result):
    lines = [
        "# 本轮知识评测",
        "",
        result["decision_reason"],
        "",
        "| 题集 | 题数 | Old | New | 变化 |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for name, v in result["suites"].items():
        lines.append(
            f"| {name} | {v['baseline']['count']} | {v['baseline']['total']} "
            f"| {v['candidate']['total']} | {v['delta']} |"
        )
    for name, v in result["suites"].items():
        lines.extend(
            [
                "",
                f"## {name}",
                f"改善题：{v['improved']}；退步题：{v['regressed']}；"
                f"新增关键错误：{v['new_major_errors']}",
                f"旧评分：{v['old_score_file']}",
                f"新评分：{v['new_score_file']}",
            ]
        )
    lines.extend(
        [
            "",
            f"通用题与训练历史的明显重叠：{result['generalized_overlap']}",
            "重叠检测包含规范化问题匹配，不保证发现所有语义重复。",
            f"专用题待处理记录：{result['excluded_test_records']}",
            "小样本也按双套成绩不下降的规则判断，不表示已证明统计显著提升。",
            f"来源：{result['round']}",
            f"TXT：{result['txt']}",
        ]
    )
    return "\n".join(lines) + "\n"


async def evaluate(
    root, source, config, name=None, concurrency=4, skills_dir=None, model=None
):
    from . import qa_workflow as flow

    root, source = Path(root), Path(source).resolve()
    round_path, round_value, split, benchmark = checked_round(root, source)
    name = name_for(source, name or round_value["name"])
    if name != round_value["name"]:
        raise ValueError(
            "评测名称必须与切分批次一致；新批次请从 /selflearn qa 开始"
        )
    profile = flow.profile_for(root)
    specialized = dict(
        profile,
        cases=round_value["cases"],
        references=round_value["references"],
        baseline_answers=None,
    )
    profiles = {"generalized": profile, "specialized": specialized}
    methods = {
        k: flow.score_method(v["references"], config, skills_dir)
        for k, v in profiles.items()
    }
    folder = root / "comparisons" / name
    folder.mkdir(parents=True, exist_ok=True)
    completed = folder / "comparison.json"
    pending = folder / ".commit.json"
    binding = {
        "round_sha256": file_hash(round_path),
        "txt_sha256": file_hash(source),
        "cases": {k: file_hash(v["cases"]) for k, v in profiles.items()},
        "methods": {k: digest(v) for k, v in methods.items()},
        "execution": await flow.legacy.backend(
            profile, "config", **flow.legacy.execution_args(profile)
        ),
        "policy": "both-nondecreasing-no-new-major-v1",
    }

    def verify_binding():
        checked_round(root, source)
        if file_hash(round_path) != binding["round_sha256"] or any(
            file_hash(p["cases"]) != binding["cases"][k]
            or digest(flow.score_method(p["references"], config, skills_dir))
            != binding["methods"][k]
            for k, p in profiles.items()
        ):
            raise ValueError("评测期间题集、评分标准或本轮成果改变")

    def verify_outputs(value):
        for path, expected in value["artifacts"].items():
            if file_hash(path) != expected:
                raise ValueError(f"评测产物改变：{path}")

    def finish(value):
        flow.write_json(completed, value)
        qa_pipeline.save_export(folder / "REPORT.md", report_text(value))
        if pending.exists():
            pending.unlink()
        flow.transcript_event("双题集评测结论", report_text(value))
        return completed

    if completed.exists():
        saved = read(completed)
        if saved["binding"] != binding:
            raise ValueError("此批次已评测但配置改变，请新建批次；旧结果保留")
        verify_outputs(saved)
        return finish(saved)
    if pending.exists():
        value = read(pending)
        if value["result"]["binding"] != binding:
            raise ValueError("待提交评测的配置改变")
        verify_outputs(value["result"])
        verify_binding()
        current = flow.read(root / "baseline.json")
        if current not in (value["parent"], value["next"]):
            raise ValueError("基线被其他任务改变，不能继续更新")
        for c, fp in value["fingerprints"].items():
            if (await flow.legacy.backend(profile, "inspect", collection=c))[
                "fingerprint"
            ] != fp:
                raise ValueError("知识库内容改变")
        flow.write_json(root / "baseline.json", value["next"])
        return finish(value["result"])
    run = flow.read(folder / "run.json")
    current = flow.read(root / "baseline.json")
    if run:
        if run["binding"] != binding or run["parent"] != current:
            raise ValueError("未完成评测的配置或基线改变，请新建批次")
    else:
        run = {"binding": binding, "parent": current}
        flow.write_json(folder / "run.json", run)
    base = (current or {}).get("collection", profile["initial_collection"])
    if current and current.get("fingerprint"):
        snapshot = await flow.legacy.backend(
            profile, "inspect", collection=base
        )
        if snapshot["fingerprint"] != current["fingerprint"]:
            raise ValueError("当前基线知识库与登记内容不同，不能混用原基线")
    database = await flow.prepare_collection(root, source, profile, name)
    if database["base"] != base:
        raise ValueError("候选知识库的来源不是当前基线")
    results, baseline_scores, candidate_scores, artifacts = {}, {}, {}, {}

    async def scored(answers, p, method):
        cached = flow.checked_score(root, answers, method)
        score_name = answers.stem
        existing = flow.registry(root)["scores"].get(score_name)
        if existing and existing["method_hash"] != digest(method):
            score_name += "--" + digest(method)[:8]
        value = cached or await flow.score_answers(
            root,
            answers,
            p["references"],
            config,
            name=score_name,
            concurrency=concurrency,
            skills_dir=skills_dir,
            model=model,
        )
        entry = next(
            v
            for v in reversed(list(flow.registry(root)["scores"].values()))
            if v.get("status") == "completed"
            and v["answers"] == str(answers)
            and v["method_hash"] == digest(method)
        )
        return value, entry["score_file"]

    for suite, p in profiles.items():
        flow.transcript_event(
            "评测题集", f"{suite}：{base} 与 {database['candidate']}"
        )
        answers, scores, score_paths = {}, {}, {}
        for label, collection in (
            ("old", base),
            ("new", database["candidate"]),
        ):
            answers[label] = await flow.publish_answers(root, p, collection)
            scores[label], score_paths[label] = await scored(
                answers[label], p, methods[suite]
            )
            artifacts[str(answers[label])] = file_hash(answers[label])
            artifacts[score_paths[label]] = file_hash(score_paths[label])
            # Register the suite identity without changing existing names.
            for key, entry in list(flow.registry(root)["scores"].items()):
                if entry.get("score_file") == score_paths[label]:
                    flow.record(
                        root,
                        "scores",
                        key,
                        {
                            **entry,
                            "suite": suite,
                            "batch": name,
                            "cases_sha256": binding["cases"][suite],
                        },
                    )
            key = answers[label].stem
            entry = flow.registry(root)["answers"][key]
            flow.record(
                root,
                "answers",
                key,
                {
                    **entry,
                    "suite": suite,
                    "batch": name if suite == "specialized" else None,
                },
            )
        cases = flow.legacy.enrich_cases(
            load_rows(p["cases"]),
            load_rows(Path(p["references"]) / "references.jsonl"),
        )
        results[suite] = compare(
            cases,
            load_rows(answers["old"]),
            load_rows(answers["new"]),
            scores["old"]["scores"],
            scores["new"]["scores"],
        )
        results[suite].update(
            eligible=(
                results[suite]["delta"] is not None
                and results[suite]["delta"] >= 0
                and not results[suite]["new_major_errors"]
            ),
            old_score_file=score_paths["old"],
            new_score_file=score_paths["new"],
        )
        baseline_scores[suite] = {
            "answers": str(answers["old"]),
            "score_file": score_paths["old"],
            "method_hash": digest(methods[suite]),
            "summary": scores["old"]["summary"],
        }
        candidate_scores[suite] = {
            "answers": str(answers["new"]),
            "score_file": score_paths["new"],
            "method_hash": digest(methods[suite]),
            "summary": scores["new"]["summary"],
        }
    verify_binding()
    if (
        await flow.legacy.backend(
            profile, "config", **flow.legacy.execution_args(profile)
        )
        != binding["execution"]
    ):
        raise ValueError("评测期间机器人执行配置改变")
    if flow.read(root / "baseline.json") != run["parent"]:
        raise ValueError("评测期间基线改变，不能覆盖")
    fingerprints = {
        base: database["base_fingerprint"],
        database["candidate"]: database["candidate_fingerprint"],
    }
    for c, fp in fingerprints.items():
        if (await flow.legacy.backend(profile, "inspect", collection=c))[
            "fingerprint"
        ] != fp:
            raise ValueError("评测期间知识库改变")
    eligible = dual_decision(results, database["added_chunks"])
    train, _ = load_episodes(round_path.parent / split["train"]["file"])
    result = {
        "schema_version": 3,
        "binding": binding,
        "round": str(round_path),
        "txt": str(source),
        "suites": results,
        "eligible": eligible,
        "promoted": eligible,
        "database": database,
        "artifacts": artifacts,
        "generalized_overlap": overlap_report(
            train, load_rows(profile["cases"])
        ),
        "excluded_test_records": benchmark["excluded"],
        "decision_reason": (
            "两套题集成绩均不下降、无新增关键错误，更新本地基线。"
            if eligible
            else "未满足两套题集评分完整、成绩均不下降、无新增关键错误且有新增知识；保留原基线。"
        ),
    }
    old = current or {
        "collection": base,
        "fingerprint": database["base_fingerprint"],
        **baseline_scores["generalized"],
    }
    next_baseline = (
        {
            "collection": database["candidate"],
            "fingerprint": database["candidate_fingerprint"],
            **candidate_scores["generalized"],
            "specialized_last": candidate_scores["specialized"],
            "parent": {
                k: old[k]
                for k in ("collection", "answers", "score_file")
                if k in old
            },
            "txt": str(source),
            "txt_sha256": file_hash(source),
            "comparison": str(completed),
        }
        if eligible
        else old
    )
    flow.write_json(
        pending,
        {
            "parent": current,
            "next": next_baseline,
            "fingerprints": fingerprints,
            "result": result,
        },
    )
    flow.write_json(root / "baseline.json", next_baseline)
    return finish(result)
