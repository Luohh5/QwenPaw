"""Named QA artifacts, independent score jobs, and a persistent baseline."""

from contextlib import aclosing
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shutil

from . import qa_evaluate as legacy
from . import qa_pipeline
from . import qa_eval_scoring
from .analyzer import digest
from .qa_eval_data import compare, file_hash, load_rows, match_rows, summarize
from .qa_storage import (
    ArtifactStore,
    period_name,
    safe_name,
    transcript_event as _transcript_event,
    workspace_lock,
    write_json,
)


def transcript_event(label, text):
    _transcript_event(label, text, force=True)


def read(path, default=None):
    return (
        json.loads(Path(path).read_text(encoding="utf-8"))
        if Path(path).exists()
        else default
    )


def registry(root):
    return read(
        root / "score/index.json",
        {"schema_version": 2, "answers": {}, "scores": {}},
    )


def record(root, section, key, value):
    index = registry(root)
    index[section][key] = value
    write_json(root / "score/index.json", index)


def immutable_copy(source, target):
    source, target = Path(source), Path(target)
    if target.exists():
        if file_hash(source) != file_hash(target):
            raise ValueError(
                f"同名文件内容不同：{target}；请用 --name 指定带 _v2 的名称"
            )
        return
    qa_pipeline.save_export(target, source.read_text(encoding="utf-8"))


def judge_config(config):
    return config.model_dump(
        mode="json",
        include={
            "active_model",
            "thinking_level",
            "fallback_models",
            "fallback_policy",
            "llm_routing",
        },
    )


def score_method(references, config, skills_dir=None):
    return {
        "version": "qa-score-v2",
        "references": legacy.tree_hash(Path(references)),
        "skill": legacy.read_skills(skills_dir)["qa-answer-score"],
        "judge": judge_config(config),
        "seed": 42,
        "implementation": {
            p.name: file_hash(p)
            for p in (
                Path(qa_eval_scoring.__file__),
                Path(__file__).with_name("qa_eval_data.py"),
            )
        },
    }


def profile_for(root):
    value = read(root / "config.json")
    if value is None:
        raise ValueError(
            "请先运行 /selflearn eval-init 配置答疑机器人和评分标准"
        )
    return value


def answer_name(profile, collection):
    return safe_name(
        "--".join(
            (
                Path(profile["cases"]).stem,
                collection,
                re.sub(r"[^\w.\-]", "_", profile["model"]),
                "thinking_" + profile["thinking"],
            )
        )
    )


async def publish_answers(
    root, profile, collection, model_name=None, name=None
):
    """Backend sidecars stay in one checkpoint DB; eval contains only JSONL."""
    if model_name:
        profile = {**profile, "model": model_name}
    execution = await legacy.backend(
        profile, "config", **legacy.execution_args(profile)
    )
    live = await legacy.backend(profile, "inspect", collection=collection)
    name = safe_name(name or answer_name(profile, collection))
    destination = root.parent / "eval" / (name + ".jsonl")
    existing = registry(root)["answers"].get(name)
    binding = {
        "collection": collection,
        "collection_fingerprint": live["fingerprint"],
        "execution": execution,
        "cases_sha256": file_hash(profile["cases"]),
    }
    if destination.exists():
        if (
            not existing
            or any(existing.get(k) != v for k, v in binding.items())
            or existing["answers_sha256"] != file_hash(destination)
        ):
            raise ValueError(
                f"回答文件内容或运行配置改变：{destination}；请指定新 --name"
            )
        match_rows(load_rows(profile["cases"]), load_rows(destination))
        transcript_event("复用已完成回答", str(destination))
        return destination
    supplied = (
        profile.get("baseline_answers")
        if collection == profile["initial_collection"]
        else None
    )
    if supplied and Path(supplied).is_file():
        meta = read(Path(supplied).with_suffix(".manifest.json"))
        if (
            meta
            and all(meta.get(k) == v for k, v in binding.items())
            and meta.get("answers_sha256") == file_hash(supplied)
        ):
            match_rows(load_rows(profile["cases"]), load_rows(supplied))
            immutable_copy(supplied, destination)
            record(
                root,
                "answers",
                name,
                {
                    **meta,
                    "path": str(destination),
                    "imported_from": str(supplied),
                },
            )
            return destination
    store = ArtifactStore(
        root / ".state" / ("batch-" + name + ".sqlite"),
        root / "score" / name / "session.md",
    )
    with store.working() as work:
        if store.get("binding", binding) != binding:
            raise ValueError(
                "未完成批测的模型、知识库或题集变化；请指定新 --name"
            )
        store.set("binding", binding)
        output = work / "answers.jsonl"
        transcript_event(
            "生成答疑回复", f"知识库：{collection}\n\n输出：{destination}"
        )
        meta = read(output.with_suffix(".manifest.json"))
        if meta is None:
            meta = await legacy.backend(
                profile,
                "run",
                collection=collection,
                cases=profile["cases"],
                output=output,
                timeout=profile["timeout"],
                **legacy.execution_args(profile),
            )
        if any(meta.get(k) != v for k, v in binding.items()) or meta[
            "answers_sha256"
        ] != file_hash(output):
            raise ValueError("批测结果与来源不一致")
        match_rows(load_rows(profile["cases"]), load_rows(output))
        immutable_copy(output, destination)
        meta = {
            **meta,
            "path": str(destination),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        record(root, "answers", name, meta)
        transcript_event("回答已保存", str(destination))
        return destination


def checked_score(root, source, method):
    for value in reversed(list(registry(root)["scores"].values())):
        if (
            value.get("status") != "completed"
            or value.get("answers") != str(Path(source).resolve())
            or value.get("answers_sha256") != file_hash(source)
            or value.get("method_hash") != digest(method)
        ):
            continue
        path = Path(value["score_file"])
        if not path.is_file() or file_hash(path) != value["score_sha256"]:
            raise ValueError(f"已登记评分被修改或丢失：{path}")
        result = read(path)
        if result["source"]["sha256"] != file_hash(source) or result[
            "method_hash"
        ] != digest(method):
            raise ValueError("评分文件与登记来源不一致")
        return result
    return None


def score_report(result):
    summary = result["summary"]
    total = summary["total"]
    lines = [
        "# 答疑评分",
        "",
        f"回答：{result['source']['path']}",
        f"评分标准：{result['references']}",
        f"总分：{total if total is not None else '待核实，不能给完整总分'} / 100",
        f"独立复评：{'已完成' if result['audit']['calibrated'] else '未完成'}",
        "",
        "| 题号 | 分数 | 原因 |",
        "|---|---:|---|",
    ]
    for key, row in result["scores"].items():
        number = row["score"] if row["score"] is not None else "待核实"
        reason = row["reason"].replace("|", "／").replace("\n", " ")
        lines.append(f"| {key} | {number} | {reason} |")
    lines.extend(
        [
            "",
            "## 耗时与题型",
            "",
            f"平均每题耗时：{summary['mean_duration_seconds']} 秒"
            f"（{summary['duration_count']} 题有耗时记录）。",
            "",
        ]
    )
    for field, label in (("kind", "题型"), ("topic", "主题")):
        lines.extend(
            [
                f"### {label}",
                "",
                "```json",
                json.dumps(summary[field], ensure_ascii=False, indent=2),
                "```",
                "",
            ]
        )
    return "\n".join(lines) + "\n"


async def score_answers(
    root,
    source,
    references,
    config,
    *,
    name=None,
    concurrency=4,
    skills_dir=None,
    model=None,
):
    from ..runtime.builder import AgentBuilder
    import asyncio

    source, references = Path(source).resolve(), Path(references).resolve()
    rows, refs = load_rows(source), load_rows(references / "references.jsonl")
    match_rows(refs, rows)
    cases = legacy.enrich_cases(refs, refs)
    method = score_method(references, config, skills_dir)
    reused = checked_score(root, source, method)
    if reused:
        transcript_event(
            "复用已完成评分",
            f"{source.name}：{reused['summary']['total']} / 100",
        )
        return reused
    name = safe_name(name or source.stem)
    folder = root / "score" / name
    binding = {
        "answers_sha256": file_hash(source),
        "method_hash": digest(method),
    }
    store = ArtifactStore(folder / ".checkpoint.sqlite", folder / "session.md")
    with store.working() as work:
        if store.get("binding", binding) != binding:
            raise ValueError(
                f"{name} 已使用不同的回答或评分标准；"
                f"请加 --name {name}_v2，旧评分会保留"
            )
        store.set("binding", binding)
        for p in references.rglob("*"):
            if p.is_file():
                destination = work / "references" / p.relative_to(references)
                destination.parent.mkdir(parents=True, exist_ok=True)
                if not destination.exists():
                    shutil.copyfile(p, destination)
        if legacy.tree_hash(work / "references") != method["references"]:
            raise ValueError("评分标准快照改变")
        write_json(work / "method.json", method)
        entry = {
            **binding,
            "answers": str(source),
            "references": str(references),
            "standard_name": references.name,
            "standard_hash": digest(method["references"]),
            "judge": method["judge"],
            "score_file": str(folder / "score.json"),
            "session": str(folder / "session.md"),
            "status": "running",
        }
        record(root, "scores", name, entry)
        transcript_event(
            "开始或继续评分",
            f"回答：{source}\n\n标准：{references}\n\n"
            f"最多同时评分 {concurrency} 题；已完成题直接复用。",
        )
        if model is None:
            model, _ = await asyncio.to_thread(
                AgentBuilder().build_model, config
            )
        completed = 0

        async def notify(phase, key, group, done):
            nonlocal completed
            if done:
                completed += 1
            label = {
                "first": "首评",
                "second": "独立复评",
                "adjudication": "核查分歧",
            }[phase]
            if sink := qa_pipeline.PROGRESS.get():
                sink(
                    json.dumps(
                        {
                            "event": "activity",
                            "activity": "完成" if done else "开始",
                            "label": f"{label} · {key}",
                            "detail": f"本次已完成或复用 {completed} 项评分。",
                            "elapsed_seconds": 0,
                        },
                        ensure_ascii=False,
                    )
                )

        try:
            final, audit = await qa_eval_scoring.score_pair(
                cases,
                {"answers": rows},
                refs,
                (references / "SCORING_RULES.md").read_text(),
                (references / "SCORING_PROMPT.md").read_text(),
                method["skill"],
                config,
                model,
                work / "scoring",
                repo=read(root / "config.json", {}).get("repo"),
                seed=42,
                notify=notify,
                concurrency=concurrency,
            )
            if (
                file_hash(source) != binding["answers_sha256"]
                or score_method(references, config, skills_dir) != method
            ):
                raise ValueError("评分期间输入、标准或评分代码改变")
            result = {
                "schema_version": 2,
                "status": "completed",
                "source": {
                    "path": str(source),
                    "sha256": binding["answers_sha256"],
                },
                "references": str(references),
                "method_hash": digest(method),
                "judge": method["judge"],
                "summary": summarize(cases, rows, final["answers"]),
                "scores": final["answers"],
                "audit": audit,
                "finished_at": datetime.now(timezone.utc).isoformat(),
            }
            write_json(folder / "score.json", result)
            (folder / "REPORT.md").write_text(
                score_report(result), encoding="utf-8"
            )
            record(
                root,
                "scores",
                name,
                {
                    **entry,
                    "status": "completed",
                    "summary": result["summary"],
                    "score": result["summary"]["total"],
                    "score_sha256": file_hash(folder / "score.json"),
                },
            )
            transcript_event(
                "评分完成",
                f"总分：{result['summary']['total']} / 100\n\n"
                f"结果：{folder / 'score.json'}",
            )
            return result
        except BaseException as exc:
            record(
                root,
                "scores",
                name,
                {
                    **entry,
                    "status": "incomplete",
                    "error": str(exc) or "执行中断",
                },
            )
            transcript_event(
                "评分尚未完成", "已完成题已保留。" + (str(exc) or "执行中断")
            )
            raise


async def prepare_collection(root, source, profile, name):
    source = Path(source).resolve()
    baseline = read(
        root / "baseline.json", {"collection": profile["initial_collection"]}
    )
    store = ArtifactStore(
        root / ".state" / ("collection-" + name + ".sqlite"),
        root.parent / "analyze/sessions" / (name + ".md"),
    )
    with store.working() as work:
        binding = {
            "source": str(source),
            "sha256": file_hash(source),
            "baseline": baseline["collection"],
            "candidate": "qwenpaw_faq_" + name,
        }
        previous = store.get("binding")
        if previous is not None and previous != binding:
            raise ValueError(
                "此日期的 TXT 或基线已改变；" "新一轮请加 --name 日期范围_v2"
            )
        store.set("binding", binding)
        result = read(work / "database/result.json")
        if result is None:
            if (work / "database").exists():
                raise ValueError(
                    "上次建库中断，原候选保留；请用 --name 日期范围_v2 创建新候选"
                )
            transcript_event(
                "复制基线并加入新知识",
                f"{binding['baseline']} → {binding['candidate']}\n\n"
                f"TXT：{source}",
            )
            result = await legacy.backend(
                profile,
                "prepare",
                base=binding["baseline"],
                target=binding["candidate"],
                txt=source,
                output=work / "database",
            )
        for collection, fingerprint in (
            (result["base"], result["base_fingerprint"]),
            (result["candidate"], result["candidate_fingerprint"]),
        ):
            live = await legacy.backend(
                profile, "inspect", collection=collection
            )
            if live["fingerprint"] != fingerprint:
                raise ValueError(f"知识库发生变化：{collection}")
        store.set("result", result)
        return result


async def _analyze_training(root, source, config, name=None, **kwargs):
    name = safe_name(name or Path(source).stem)
    workspace = root.parent
    history = Path(source).resolve()
    output = Path(
        kwargs.pop("output", None)
        or workspace / "analyze/txt" / (name + ".txt")
    )
    immutable_copy(source, history)
    transcript = workspace / "analyze/sessions" / (name + ".md")
    store = ArtifactStore(
        workspace / "analyze/.state" / (name + "-train.sqlite"), transcript
    )
    with store.working() as work:
        imported = store.get("imported_export")
        if imported and output.exists():
            if imported["source_sha256"] != file_hash(history) or imported[
                "output_sha256"
            ] != file_hash(output):
                raise ValueError("导入的历史或 TXT 已改变；请使用新 --name")
            transcript_event(
                "复用已导入的 TXT",
                f"{output}\n\n本次未重新分析。若要应用新参数或重新分析，请用 --name {name}_v2。",
            )
            status = {
                "state": "completed",
                "phase": "done",
                "exported": imported["exported"],
                "output": str(output),
                "run_dir": str(transcript),
                "reused_historical_export": True,
            }
            write_json(root / "qa_status.json", status)
            yield json.dumps(status, ensure_ascii=False)
            return
        transcript_event(
            "分析历史问答", f"输入：{history}\n\n最终 TXT：{output}"
        )
        # Freeze the export date across resumes on later days.
        batch_date = store.get("batch_date", datetime.now().date().isoformat())
        store.set("batch_date", batch_date)
        final = {}
        async with aclosing(
            qa_pipeline.run_qa(
                history,
                work,
                config,
                output=output,
                live=False,
                batch_date=batch_date,
                **kwargs,
            )
        ) as events:
            async for raw in events:
                value = json.loads(raw)
                value["run_dir"] = str(transcript)
                value["session"] = str(transcript)
                write_json(root / "qa_status.json", value)
                final = value
                if value.get("state") == "running":
                    yield json.dumps(value, ensure_ascii=False)
        store.set(
            "lineage",
            {
                "input": str(history),
                "input_sha256": file_hash(history),
                "output": final.get("output"),
                "session": str(transcript),
                "state": final.get("state"),
            },
        )
        if final.get("output"):
            transcript_event(
                "分析结束",
                f"生成 {final.get('exported', 0)} 条知识：{final['output']}",
            )
        yield json.dumps(final, ensure_ascii=False)


async def _evaluate_single_suite(
    root, source, config, name=None, concurrency=4, skills_dir=None, model=None
):
    profile = profile_for(root)
    name = period_name(source, name)
    old_baseline = read(root / "baseline.json")
    parent = old_baseline or {"collection": profile["initial_collection"]}
    # A finished named operation is idempotent even after baseline promotion.
    completed_path = (
        root
        / "score"
        / answer_name(profile, "qwenpaw_faq_" + name)
        / "comparison.json"
    )
    pending_path = completed_path.parent / ".comparison-pending.json"
    if pending_path.exists() and not completed_path.exists():
        pending = read(pending_path)
        value = pending["comparison"]
        current = read(root / "baseline.json")
        if current not in (pending["parent_baseline"], pending["baseline"]):
            raise ValueError("待恢复评测的基线已被其他操作修改")
        if value["txt_sha256"] != file_hash(source) or value[
            "method_hash"
        ] != digest(score_method(profile["references"], config, skills_dir)):
            raise ValueError("待恢复评测的 TXT 或评分方法已改变")
        for score_path in (value["old_score_file"], value["new_score_file"]):
            entry = next(
                (
                    v
                    for v in registry(root)["scores"].values()
                    if v.get("score_file") == score_path
                    and v.get("status") == "completed"
                ),
                None,
            )
            if (
                not entry
                or file_hash(score_path) != entry["score_sha256"]
                or file_hash(entry["answers"]) != entry["answers_sha256"]
            ):
                raise ValueError("待恢复评测的评分或回答改变")
        db = value["database"]
        for collection, fingerprint in (
            (db["base"], db["base_fingerprint"]),
            (db["candidate"], db["candidate_fingerprint"]),
        ):
            if (
                await legacy.backend(profile, "inspect", collection=collection)
            )["fingerprint"] != fingerprint:
                raise ValueError("待恢复评测的知识库改变")
        write_json(root / "baseline.json", pending["baseline"])
        write_json(completed_path, value)
        pending_path.unlink()
    if completed_path.exists():
        value = read(completed_path)
        if value["txt_sha256"] != file_hash(source):
            raise ValueError("此日期已评测不同 TXT；请加 --name 日期范围_v2")
        transcript_event(
            "该轮已完成", json.dumps(value, ensure_ascii=False, indent=2)
        )
        return completed_path
    database = await prepare_collection(root, source, profile, name)
    old_answers = (
        Path(parent["answers"])
        if parent.get("answers")
        else await publish_answers(root, profile, parent["collection"])
    )
    if parent.get("answers"):
        # Revalidate the saved execution against the current profile and DB.
        entry = registry(root)["answers"].get(old_answers.stem)
        current_execution = await legacy.backend(
            profile, "config", **legacy.execution_args(profile)
        )
        if (
            not entry
            or entry["answers_sha256"] != file_hash(old_answers)
            or entry["collection_fingerprint"] != database["base_fingerprint"]
            or entry["execution"] != current_execution
            or entry["collection"] != parent["collection"]
        ):
            raise ValueError(
                "基线回答与当前配置或知识库不一致；请重新初始化基线，不能混用旧分数"
            )
    method = score_method(profile["references"], config, skills_dir)
    old_score = checked_score(root, old_answers, method)
    if parent.get("score_file") and old_score is None:
        raise ValueError(
            "评分标准或评分模型变化。请先用 /selflearn score 为基线回答生成同标准的新评分，再继续"
        )
    if old_score is None:
        old_score = await score_answers(
            root,
            old_answers,
            profile["references"],
            config,
            concurrency=concurrency,
            skills_dir=skills_dir,
            model=model,
        )
    if (
        not old_baseline
        or not old_baseline.get("score_file")
        or old_baseline.get("method_hash") != digest(method)
    ):
        # Initialization records the old baseline once, independently of the
        # candidate's success. Null scores remain explicitly unresolved.
        entry = next(
            v
            for v in registry(root)["scores"].values()
            if v.get("status") == "completed"
            and v.get("answers_sha256") == file_hash(old_answers)
            and v.get("method_hash") == digest(method)
        )
        old_baseline = {
            "collection": parent["collection"],
            "fingerprint": database["base_fingerprint"],
            "answers": str(old_answers),
            "score_file": entry["score_file"],
            "method_hash": digest(method),
            "summary": old_score["summary"],
        }
        write_json(root / "baseline.json", old_baseline)
    new_answers = await publish_answers(root, profile, database["candidate"])
    new_score = await score_answers(
        root,
        new_answers,
        profile["references"],
        config,
        concurrency=concurrency,
        skills_dir=skills_dir,
        model=model,
    )
    refs = load_rows(Path(profile["references"]) / "references.jsonl")
    cases = legacy.enrich_cases(refs, refs)
    result = compare(
        cases,
        load_rows(old_answers),
        load_rows(new_answers),
        old_score["scores"],
        new_score["scores"],
    )
    eligible = result["eligible"] and database["added_chunks"] > 0
    if read(root / "baseline.json") != old_baseline:
        raise ValueError("基线在评测期间发生变化，不能覆盖")
    for collection, fingerprint in (
        (database["base"], database["base_fingerprint"]),
        (database["candidate"], database["candidate_fingerprint"]),
    ):
        if (await legacy.backend(profile, "inspect", collection=collection))[
            "fingerprint"
        ] != fingerprint:
            raise ValueError("知识库在评分期间改变，不能更新基线")
    if score_method(profile["references"], config, skills_dir) != method:
        raise ValueError("评分方法在评测期间改变")
    execution = await legacy.backend(
        profile, "config", **legacy.execution_args(profile)
    )
    for path in (old_answers, new_answers):
        entry = registry(root)["answers"].get(path.stem)
        if (
            not entry
            or entry["answers_sha256"] != file_hash(path)
            or entry["execution"] != execution
            or entry["cases_sha256"] != file_hash(profile["cases"])
        ):
            raise ValueError("评测期间回答、题集或机器人配置改变")
    result.update(
        eligible=bool(eligible),
        promoted=bool(eligible),
        txt=str(Path(source).resolve()),
        txt_sha256=file_hash(source),
        baseline_collection=database["base"],
        candidate_collection=database["candidate"],
        decision_reason=(
            "本轮总分提高且无新增关键错误；已更新本地基线"
            if eligible
            else "未满足完整评分、总分提高、无新增关键错误、有新增知识的全部条件；保留原基线"
        ),
        old_score_file=old_baseline["score_file"],
        database=database,
        method_hash=digest(method),
        new_score_file=next(
            v["score_file"]
            for v in registry(root)["scores"].values()
            if v.get("status") == "completed"
            and v.get("answers") == str(new_answers)
            and v.get("method_hash") == digest(method)
        ),
    )
    # Persist recovery intent before the baseline pointer, then publish report.
    next_baseline = (
        {
            "collection": database["candidate"],
            "fingerprint": database["candidate_fingerprint"],
            "answers": str(new_answers),
            "score_file": result["new_score_file"],
            "method_hash": digest(method),
            "summary": new_score["summary"],
            "parent": {
                k: old_baseline[k]
                for k in ("collection", "answers", "score_file")
                if k in old_baseline
            },
            "txt": result["txt"],
            "txt_sha256": result["txt_sha256"],
        }
        if eligible
        else old_baseline
    )
    write_json(
        completed_path.parent / ".comparison-pending.json",
        {
            "comparison": result,
            "baseline": next_baseline,
            "parent_baseline": old_baseline,
        },
    )
    if eligible:
        write_json(root / "baseline.json", next_baseline)
    write_json(completed_path, result)
    (completed_path.parent / ".comparison-pending.json").unlink(
        missing_ok=True
    )
    transcript_event(
        "评测结论",
        result["decision_reason"]
        + f"\n\n旧库：{result['baseline']['total']} / 100；"
        + f"新库：{result['candidate']['total']} / 100。"
        + f"\n\n比较记录：{completed_path}",
    )
    with (completed_path.parent / "REPORT.md").open(
        "a", encoding="utf-8"
    ) as stream:
        stream.write(
            "\n## 与基线比较\n\n"
            + result["decision_reason"]
            + f"\n\n旧库 {result['baseline']['total']} / 100，"
            + f"新库 {result['candidate']['total']} / 100。\n"
        )
    return completed_path


async def analyze(root, source, config, name=None, **kwargs):
    from .qa_rounds import analyze as run

    async with aclosing(
        run(root, source, config, name=name, **kwargs)
    ) as events:
        async for event in events:
            yield event


async def evaluate(root, source, config, **kwargs):
    from .qa_rounds import evaluate as run

    return await run(root, source, config, **kwargs)


async def run_workflow(root, config, workflow="qa", live=True, **settings):
    root = Path(root)

    async def perform():
        from .qa_pipeline import PROGRESS
        from .qa_storage import ACTIVE_STORE

        original_sink = PROGRESS.get()

        def sink(raw):
            if store := ACTIVE_STORE.get():
                if json.loads(raw).get("event") == "activity":
                    store.checkpoint()
            if original_sink:
                original_sink(raw)

        token = PROGRESS.set(sink)
        try:
            if workflow == "qa":
                async with aclosing(
                    analyze(root, config=config, **settings)
                ) as events:
                    async for event in events:
                        yield event
                return
            state = {
                "state": "running",
                "phase": "evaluate",
                "current": workflow,
            }
            write_json(root / "qa_status.json", state)
            yield json.dumps(state, ensure_ascii=False)
            if workflow == "score":
                references = (
                    settings.pop("references", None)
                    or profile_for(root)["references"]
                )
                value = await score_answers(
                    root, references=references, config=config, **settings
                )
                result = next(
                    v["score_file"]
                    for v in registry(root)["scores"].values()
                    if v.get("status") == "completed"
                    and v.get("answers_sha256") == value["source"]["sha256"]
                    and v.get("method_hash") == value["method_hash"]
                )
            elif workflow == "train_eval":
                from .qa_train_eval import train_evaluate

                result = await train_evaluate(root, config=config, **settings)
            elif workflow == "batch":
                result = await publish_answers(
                    root, profile_for(root), **settings
                )
            elif workflow == "prepare":
                source = settings.pop("source")
                name = period_name(source, settings.pop("name", None))
                result = (
                    await prepare_collection(
                        root, source, profile_for(root), name
                    )
                )["candidate"]
            else:
                result = await evaluate(root, config=config, **settings)
            state.update(
                state="completed", current="执行完成", output=str(result)
            )
        except Exception as exc:
            phase = read(root / "qa_status.json", {}).get("phase", workflow)
            state = {
                "state": "failed",
                "phase": phase,
                "error": str(exc)
                or (
                    "执行超时，请重试；已完成的结果会保留"
                    if isinstance(exc, TimeoutError)
                    else type(exc).__name__
                ),
                "error_type": type(exc).__name__,
            }
        finally:
            PROGRESS.reset(token)
        write_json(root / "qa_status.json", state)
        yield json.dumps(state, ensure_ascii=False)

    with workspace_lock(root):
        async with aclosing(
            qa_pipeline.stream_progress(perform(), live)
        ) as events:
            async for event in events:
                yield event
