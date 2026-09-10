"""QA evaluation orchestration. Credentials are inherited, never persisted."""

import asyncio
from contextlib import aclosing, contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import uuid
from time import monotonic

from .analyzer import digest, write_json
from .qa_eval_data import (
    ComparisonNote,
    compare,
    file_hash,
    load_rows,
    match_rows,
)
from .qa_eval_scoring import score_pair
from .qa_pipeline import PROGRESS, SKILLS, stage_call, stream_progress


@contextmanager
def eval_lock(root):
    import fcntl

    root.mkdir(parents=True, exist_ok=True)
    with (root / "eval.lock").open("a+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError("该工作区已有评测运行中") from exc
        yield


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def tree_hash(folder):
    return {
        str(p.relative_to(folder)): file_hash(p)
        for p in sorted(Path(folder).rglob("*"))
        if p.is_file()
    }


def initialize(
    root,
    alias_root,
    collection,
    model,
    thinking,
    cases=None,
    references=None,
    baseline_answers=None,
    repo=None,
):
    root = Path(root)
    alias_root = Path(alias_root).expanduser().resolve()
    if not (alias_root / "eval/knowledge_eval.py").is_file():
        raise ValueError(
            "--alias-root 必须指向含 eval/knowledge_eval.py 的 alias 目录"
        )
    cases = Path(
        cases or alias_root / "eval/question_set_20260907/cases_dev.jsonl"
    ).resolve()
    references = Path(
        references
        or alias_root / "eval/question_set_20260907/references_dev_v1_2"
    ).resolve()
    case_rows = load_rows(cases)
    refs = load_rows(references / "references.jsonl")
    match_rows(case_rows, refs)
    for name in ("SCORING_RULES.md", "SCORING_PROMPT.md"):
        if not (references / name).is_file():
            raise ValueError(f"缺少评分文件：{name}")
    value = {
        "schema_version": 1,
        "alias_root": str(alias_root),
        "initial_collection": collection,
        "model": model,
        "thinking": thinking,
        "cases": str(cases),
        "references": str(references),
        "max_iters": 30,
        "timeout": 600,
        "seed": 42,
        "repo": str(Path(repo).resolve()) if repo else None,
        "baseline_answers": (
            str(Path(baseline_answers).resolve()) if baseline_answers else None
        ),
    }
    with eval_lock(root):
        path = root / "config.json"
        if path.exists() and read_json(path) != value:
            raise ValueError(
                "配置已存在；修改配置请编辑 config.json，或使用新的 --work-dir"
            )
        write_json(path, value)
    return path


async def backend(profile, mode, **kwargs):
    alias = Path(profile["alias_root"])
    args = [
        str(alias / ".venv/bin/python"),
        str(alias / "eval/knowledge_eval.py"),
        mode,
    ]
    for key, value in kwargs.items():
        args.extend(["--" + key.replace("_", "-"), str(value)])
    process = await asyncio.create_subprocess_exec(
        *args,
        cwd=alias,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    communication = asyncio.create_task(process.communicate())
    try:
        if mode == "run" and (sink := PROGRESS.get()):
            total = len(load_rows(kwargs["cases"]))
            previous = None
            started = monotonic()
            while True:
                count = await asyncio.to_thread(
                    batch_answer_count, Path(kwargs["output"])
                )
                if count is not None and count != previous:
                    sink(
                        json.dumps(
                            {
                                "event": "activity",
                                "activity": "批测进度",
                                "label": "生成答疑回复",
                                "detail": f"已生成 {count}/{total} 条回答。",
                                "elapsed_seconds": round(
                                    monotonic() - started, 1
                                ),
                            },
                            ensure_ascii=False,
                        )
                    )
                    previous = count
                if communication.done():
                    break
                await asyncio.wait({communication}, timeout=2)
        stdout, stderr = await asyncio.shield(communication)
    except BaseException:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(
                    asyncio.shield(communication), timeout=25
                )
            except asyncio.TimeoutError:
                process.kill()
                await communication
        else:
            await communication
        raise
    if process.returncode:
        message = stderr.decode(errors="replace").strip().splitlines()
        # Backend sanitizes errors; mask again at this boundary.
        error = message[-1] if message else f"执行失败：{mode}"
        for name in ("DASHSCOPE_API_KEY", "GITHUB_TOKEN"):
            if os.environ.get(name):
                error = error.replace(os.environ[name], "[REDACTED]")
        raise ValueError(error[:1000])
    return json.loads(stdout.decode().strip().splitlines()[-1])


def batch_answer_count(path):
    """Read only complete JSONL records; never forward raw answers or logs."""
    if not path.exists():
        return 0
    try:
        return len(load_rows(path))
    except (OSError, ValueError):
        # A writer may be publishing a replacement while we poll.
        return None


def execution_args(profile):
    return {k: profile[k] for k in ("model", "thinking", "max_iters")}


def read_skills(directory=None):
    root = Path(directory) if directory else SKILLS
    result = {}
    for name in ("qa-evaluate", "qa-answer-score"):
        text = (root / name / "SKILL.md").read_text(encoding="utf-8")
        if not text.startswith("---\n") or f"name: {name}" not in text:
            raise ValueError(f"Skill 格式无效：{name}")
        result[name] = text
    return result


def enrich_cases(cases, references):
    result = {}
    for key, row in cases.items():
        reference = references[key]
        result[key] = {
            "case_id": key,
            "question": row["question"],
            "topic": reference.get("topic") or row.get("topic"),
            "kind": reference.get("kind") or row.get("kind"),
        }
        if not result[key]["topic"] or not result[key]["kind"]:
            raise ValueError(f"题目缺少 topic/kind：{key}")
    return result


def write_rows(path, rows):
    Path(path).write_text(
        "".join(
            json.dumps(r, ensure_ascii=False) + "\n" for r in rows.values()
        ),
        encoding="utf-8",
    )


def report_text(result, audit, notes, folder):
    old, new = result["baseline"], result["candidate"]

    def display(value):
        if value is None:
            return "未确定"
        return round(value, 3) if isinstance(value, float) else value

    lines = [
        "# 答疑知识库评测",
        "",
        "结论："
        + (
            "通过本轮评测，本地基线已更新。"
            if result["promoted"]
            else "未更新本地基线。"
        ),
        f"原因：{result['decision_reason']}",
        f"固定题集共 {old['count']} 题，换算为百分制。",
        "",
        "| 指标 | 基线 | 候选 |",
        "|---|---:|---:|",
    ]
    for key, label in (
        ("total", "固定题集得分 /100"),
        ("at_least_3", "≥3 分题数"),
        ("scored", "分数明确题数"),
        ("run_success", "运行成功题数"),
        ("run_status_unknown", "运行状态未知题数"),
        ("effective_answers", "有效回答题数"),
        ("mean_duration_seconds", "平均耗时 /秒"),
        ("duration_count", "有耗时记录题数"),
    ):
        lines.append(
            f"| {label} | {display(old[key])} | {display(new[key])} |"
        )
    lines += [
        "",
        f"总分变化：{display(result['delta'])}；耗时统计包含有有效耗时的失败记录。",
        "运行失败计 0 时，该总分包含交付失败，不能解释为纯知识正确率。",
        "",
        f"提升：{result['improved']}",
        f"退步：{result['regressed']}",
        f"新出现关键错误：{result['new_major_errors']}",
        f"跨过达标线：{result['crossed_up']}；跌破达标线：{result['crossed_down']}",
        "",
    ]
    for label, summary in (("基线", old), ("候选", new)):
        lines += [
            f"## {label}",
            "",
            f"关键错误：{summary['major_errors']}",
            f"待核实：{summary['needs_review']}",
            f"运行失败：{summary['run_failures']}；原因统计：{summary['failure_causes']}",
            "",
            "| 分类 | 得分 / 满分 | 已判 / 总数 | 均分 /4 | ≥3 分题数 | ≥3 比例 |",
            "|---|---:|---:|---:|---:|---:|",
        ]
        for field in ("topic", "kind"):
            for category, group in summary[field].items():
                lines.append(
                    f"| {category} | {group['points']} / {group['maximum']} "
                    f"| {group['scored']} / {group['count']} "
                    f"| {display(group['mean'])} | {group['at_least_3']} "
                    f"| {display(group['at_least_3_ratio'])} |"
                )
        lines.append("")
    lines += [
        "## 独立复评",
        "",
        "每主题随机一题，比较组使用相同题号；另有定向抽检。",
        "采用独立上下文；不声称不同模型验证。",
        "",
        "```json",
        json.dumps(audit, ensure_ascii=False, indent=2),
        "```",
        "",
        "## 结果解释",
        "",
        notes["improvement"],
        "",
        notes["regressions"],
        "",
        notes["limitations"],
        "",
        "一轮得分变化不代表稳定线上提升；评分参考稿未经过完整人工验收。",
        "服务器知识库未上传或切换。",
        "",
        "## 逐题扣分与待核实",
        "",
    ]
    for group, label in (("group_1", "基线"), ("group_2", "候选")):
        rows = load_rows(folder / "scoring" / group / "scored_results.jsonl")
        for key, row in rows.items():
            if row["score"] != 4:
                lines += [
                    f"### {label} · {key} · {row['score']}",
                    "",
                    row["reason"],
                    "",
                ]
                for deduction in row["deductions"]:
                    lines.append(
                        f"- {deduction['answer_quote'] or '遗漏'}："
                        f"{deduction['reason']}；影响：{deduction['impact']}；"
                        f"来源：{', '.join(deduction['source_ids'])}"
                    )
                lines.extend(
                    f"- 待核实：{text}" for text in row["uncertainties"]
                )
                lines.append("")
    return "\n".join(lines)


def finish_report(folder, manifest, result, audit, notes):
    """Publish completion only after the baseline pointer was saved."""
    write_json(folder / "comparison.json", result)
    temporary = folder / "REPORT.pending.md"
    temporary.write_text(
        report_text(result, audit, notes, folder), encoding="utf-8"
    )
    temporary.replace(folder / "REPORT.md")
    manifest.update(
        completed=True,
        finished_at=datetime.now(timezone.utc).isoformat(),
        result=result,
        artifact_hashes={
            name: file_hash(folder / name)
            for name in ("A.txt", "comparison.json", "REPORT.md")
        },
    )
    write_json(folder / "manifest.json", manifest)


async def run_evaluate(
    source, root, config, resume=None, skills_dir=None, model=None, live=False
):
    root = Path(root) / "eval"
    with eval_lock(root):
        async with aclosing(
            stream_progress(
                _evaluate(
                    Path(source), root, config, resume, skills_dir, model
                ),
                live,
            )
        ) as events:
            async for event in events:
                yield event


async def _evaluate(source, root, config, resume, skills_dir, model):
    from ..runtime.builder import AgentBuilder

    status_file = root.parent / "qa_status.json"
    status = {"state": "running", "phase": "evaluate", "current": "检查配置"}
    write_json(status_file, status)
    yield json.dumps(status, ensure_ascii=False)
    try:
        profile = read_json(root / "config.json")
        if any(
            k not in profile
            for k in ("alias_root", "model", "thinking", "initial_collection")
        ):
            raise ValueError("评测配置不完整，请先 eval-init")
        for name in ("DASHSCOPE_API_KEY", "GITHUB_TOKEN"):
            if not os.environ.get(name):
                raise ValueError(f"缺少环境变量：{name}")
        if (
            not source.is_file()
            or source.suffix.lower() != ".txt"
            or not source.read_text(encoding="utf-8").strip()
        ):
            raise ValueError("候选 TXT 不存在或为空")
        skills = read_skills(skills_dir)
        references_dir = Path(profile["references"])
        references = load_rows(references_dir / "references.jsonl")
        original_cases = load_rows(profile["cases"])
        match_rows(original_cases, references)
        cases = enrich_cases(original_cases, references)
        execution = await backend(profile, "config", **execution_args(profile))
        judge = config.model_dump(
            mode="json",
            include={
                "active_model",
                "thinking_level",
                "running",
                "fallback_models",
                "fallback_policy",
                "llm_routing",
            },
        )
        score_binding = {
            "judge": judge,
            "skills": skills,
            "seed": profile["seed"],
            "references": tree_hash(references_dir),
            "implementation": {
                p.name: file_hash(p)
                for p in Path(__file__).parent.glob("qa_eval*.py")
            },
        }
        binding = {
            "profile": profile,
            "source_hash": file_hash(source),
            "cases": digest(cases),
            "execution": execution,
            "scoring": digest(score_binding),
        }
        baseline_file = root / "baseline.json"
        baseline = (
            read_json(baseline_file)
            if baseline_file.exists()
            else {"collection": profile["initial_collection"]}
        )
        if resume:
            folder = Path(resume).resolve()
            if folder.parent != (root / "runs").resolve():
                raise ValueError("--resume 必须是当前工作区 runs 下的运行目录")
            manifest = read_json(folder / "manifest.json")
            if manifest["binding"] != binding:
                raise ValueError(
                    "输入、配置、代码或 Skill 已改变，不能续跑旧批次"
                )
            if manifest.get("completed"):
                status.update(
                    state="completed",
                    current="该轮已经结束",
                    output=str(folder / "REPORT.md"),
                    run_dir=str(folder),
                )
                write_json(status_file, status)
                yield json.dumps(status, ensure_ascii=False)
                return
            # Recover a completed pointer update after a process interruption.
            next_path = folder / "baseline_next.json"
            if (
                next_path.exists()
                and baseline == read_json(next_path)
                and (folder / "finish_pending.json").exists()
            ):
                pending = read_json(folder / "finish_pending.json")
                pending["result"]["promoted"] = pending["result"]["eligible"]
                finish_report(
                    folder,
                    manifest,
                    pending["result"],
                    pending["audit"],
                    pending["notes"],
                )
                status.update(
                    state="completed",
                    current="已恢复完成记录",
                    run_dir=str(folder),
                    output=str(folder / "REPORT.md"),
                )
                write_json(status_file, status)
                yield json.dumps(status, ensure_ascii=False)
                return
            if manifest["parent_baseline"] != baseline:
                raise ValueError("基线已变化，不能续跑旧批次")
        else:
            run_id = (
                datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ_")
                + uuid.uuid4().hex[:8]
            )
            folder = root / "runs" / run_id
            folder.mkdir(parents=True)
            manifest = {
                "run_id": run_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "binding": binding,
                "parent_baseline": baseline,
                "candidate": f"qa_eval_{run_id.lower()}",
                "completed": False,
            }
            write_json(folder / "manifest.json", manifest)
            shutil.copyfile(source, folder / "A.txt")
            write_rows(folder / "cases.jsonl", cases)
            shutil.copytree(references_dir, folder / "references")
            write_json(folder / "skills.json", skills)
            write_json(folder / "scoring_binding.json", score_binding)

        def verify_inputs():
            if (
                file_hash(folder / "A.txt") != binding["source_hash"]
                or digest(load_rows(folder / "cases.jsonl"))
                != binding["cases"]
                or tree_hash(folder / "references")
                != score_binding["references"]
                or read_json(folder / "skills.json") != skills
            ):
                raise ValueError(
                    "本轮保存的 TXT、题集、参考或 Skill 快照已改变"
                )

        verify_inputs()
        status.update(run_dir=str(folder), current="复制旧库并追加 TXT")
        write_json(status_file, status)
        yield json.dumps(status, ensure_ascii=False)
        database_result = folder / "database/result.json"
        if not database_result.exists():
            if (folder / "database").exists():
                raise ValueError(
                    "上次建库中断；保留候选库供检查，请启动新一轮（不会删除旧库）"
                )
            await backend(
                profile,
                "prepare",
                base=baseline["collection"],
                target=manifest["candidate"],
                txt=folder / "A.txt",
                output=folder / "database",
            )
        database = read_json(database_result)

        async def verify_databases():
            for collection, fingerprint in (
                (baseline["collection"], database["base_fingerprint"]),
                (manifest["candidate"], database["candidate_fingerprint"]),
            ):
                live = await backend(profile, "inspect", collection=collection)
                if live["fingerprint"] != fingerprint:
                    raise ValueError(f"知识库在本轮期间发生变化：{collection}")

        await verify_databases()
        if (
            baseline.get("fingerprint")
            and baseline["fingerprint"] != database["base_fingerprint"]
        ):
            raise ValueError(
                "基线库被外部修改，旧成绩不能复用；请使用新的评测工作目录"
            )
        answers = {}
        for group, collection in (
            ("group_1", baseline["collection"]),
            ("group_2", manifest["candidate"]),
        ):
            status["current"] = (
                "生成基线回答" if group == "group_1" else "生成候选回答"
            )
            write_json(status_file, status)
            yield json.dumps(status, ensure_ascii=False)
            destination = folder / group / "answers.jsonl"
            destination.parent.mkdir(exist_ok=True)
            if not destination.exists():
                old_answers = baseline.get("answers") or profile.get(
                    "baseline_answers"
                )
                reused = False
                if (
                    group == "group_1"
                    and old_answers
                    and Path(old_answers).exists()
                ):
                    old_path = Path(old_answers)
                    old_manifest_path = old_path.with_suffix(".manifest.json")
                    if old_manifest_path.exists():
                        old_manifest = read_json(old_manifest_path)
                        if (
                            old_manifest.get("execution") == execution
                            and old_manifest.get("collection") == collection
                            and old_manifest.get("collection_fingerprint")
                            == database["base_fingerprint"]
                            and old_manifest.get("answers_sha256")
                            == file_hash(old_path)
                        ):
                            rows = load_rows(old_path)
                            match_rows(cases, rows)
                            shutil.copyfile(old_path, destination)
                            shutil.copyfile(
                                old_manifest_path,
                                destination.with_suffix(".manifest.json"),
                            )
                            reused = True
                if not reused:
                    await backend(
                        profile,
                        "run",
                        collection=collection,
                        cases=folder / "cases.jsonl",
                        output=destination,
                        timeout=profile["timeout"],
                        **execution_args(profile),
                    )
            answer_manifest_path = destination.with_suffix(".manifest.json")
            if not answer_manifest_path.exists():
                await backend(
                    profile,
                    "run",
                    collection=collection,
                    cases=folder / "cases.jsonl",
                    output=destination,
                    timeout=profile["timeout"],
                    **execution_args(profile),
                )
            answer_manifest = read_json(answer_manifest_path)
            if (
                answer_manifest["answers_sha256"] != file_hash(destination)
                or answer_manifest["execution"] != execution
                or answer_manifest["collection"] != collection
                or answer_manifest.get("collection_fingerprint")
                != database[
                    (
                        "base_fingerprint"
                        if group == "group_1"
                        else "candidate_fingerprint"
                    )
                ]
            ):
                raise ValueError("回答文件或运行配置改变，不能继续")
            answers[group] = load_rows(destination)
            match_rows(cases, answers[group])
        await verify_databases()
        if model is None:
            model, _ = await asyncio.to_thread(
                AgentBuilder().build_model, config
            )
        write_json(
            folder / "judge.json",
            {
                "configured": judge,
                "actual_model_name": getattr(model, "model_name", None)
                or "unknown",
                "class": type(model).__name__,
            },
        )
        scoring = folder / "scoring"
        if (
            not scoring.exists()
            and baseline.get("scoring_binding") == binding["scoring"]
        ):
            prior = Path(baseline["scoring_group"])
            if (
                prior.is_dir()
                and tree_hash(prior) == baseline.get("scoring_files")
                and baseline.get("answers_hash")
                == file_hash(folder / "group_1/answers.jsonl")
            ):
                shutil.copytree(prior, scoring / "group_1")

        async def progress(phase, key, group, completed):
            label = {
                "first": "首评",
                "second": "独立复评",
                "adjudication": "核查分歧",
            }.get(phase, phase)
            group_label = "基线" if group == "group_1" else "候选"
            action = "完成" if completed else "开始"
            status.update(current=f"{action}{label} · {group_label} · {key}")
            write_json(status_file, status)
            if sink := PROGRESS.get():
                sink(json.dumps(status, ensure_ascii=False))

        status["current"] = "逐题评分和独立复评"
        write_json(status_file, status)
        yield json.dumps(status, ensure_ascii=False)
        final, audit = await score_pair(
            cases,
            answers,
            references,
            (folder / "references/SCORING_RULES.md").read_text(
                encoding="utf-8"
            ),
            (folder / "references/SCORING_PROMPT.md").read_text(
                encoding="utf-8"
            ),
            skills["qa-answer-score"],
            config,
            model,
            scoring,
            repo=profile.get("repo"),
            seed=profile["seed"],
            notify=progress,
        )
        result = compare(
            cases,
            answers["group_1"],
            answers["group_2"],
            final["group_1"],
            final["group_2"],
        )
        result["eligible"] = (
            result["eligible"] and database["added_chunks"] > 0
        )
        result["promoted"] = False
        result["decision_reason"] = (
            "总分提高，独立复评完成，且没有新增关键错误"
            if result["eligible"]
            else "未满足总分提高、无新增关键错误、分数完整且有新增知识的全部条件"
        )
        notes_path = folder / "comparison_note.json"
        if notes_path.exists():
            notes = ComparisonNote.model_validate(read_json(notes_path))
        else:
            status["current"] = "解释分数变化并生成报告"
            write_json(status_file, status)
            yield json.dumps(status, ensure_ascii=False)
            notes = await stage_call(
                config,
                model,
                skills["qa-evaluate"],
                {"comparison": result, "audit": audit},
                ComparisonNote,
                [],
                folder / "interpretation",
                lambda _: None,
            )
            write_json(notes_path, notes.model_dump())
        await verify_databases()
        if (
            await backend(profile, "config", **execution_args(profile))
            != execution
        ):
            raise ValueError("机器人代码或依赖在评测期间改变，不能更新基线")
        verify_inputs()
        current = (
            read_json(baseline_file)
            if baseline_file.exists()
            else {"collection": profile["initial_collection"]}
        )
        if current != manifest["parent_baseline"]:
            raise ValueError("基线指针已经改变，禁止覆盖")
        chosen = "group_2" if result["eligible"] else "group_1"
        if result["eligible"] or not baseline_file.exists():
            new_baseline = {
                "collection": (
                    manifest["candidate"]
                    if result["eligible"]
                    else baseline["collection"]
                ),
                "fingerprint": (
                    database["candidate_fingerprint"]
                    if result["eligible"]
                    else database["base_fingerprint"]
                ),
                "run_id": manifest["run_id"],
                "parent": {
                    k: baseline[k]
                    for k in ("collection", "run_id")
                    if k in baseline
                },
                "answers": str(folder / chosen / "answers.jsonl"),
                "answers_hash": file_hash(folder / chosen / "answers.jsonl"),
                "scoring_binding": binding["scoring"],
                "scoring_group": str(scoring / chosen),
                "scoring_files": tree_hash(scoring / chosen),
                "summary": (
                    result["candidate"]
                    if result["eligible"]
                    else result["baseline"]
                ),
            }
            # Save the intended result before replacing the baseline pointer.
            write_json(folder / "baseline_next.json", new_baseline)
        write_json(
            folder / "finish_pending.json",
            {"result": result, "audit": audit, "notes": notes.model_dump()},
        )
        if result["eligible"] or not baseline_file.exists():
            write_json(baseline_file, new_baseline)
            result["promoted"] = result["eligible"]
        finish_report(folder, manifest, result, audit, notes.model_dump())
        status.update(
            state="completed",
            current=(
                "通过，已更新本地基线"
                if result["promoted"]
                else "未通过，保留旧基线"
            ),
            output=str(folder / "REPORT.md"),
        )
    except asyncio.CancelledError:
        status.update(state="stopped", current="已停止，完成的文件保留")
        write_json(status_file, status)
        raise
    except Exception as exc:
        error = str(exc)
        for name in ("DASHSCOPE_API_KEY", "GITHUB_TOKEN"):
            if os.environ.get(name):
                error = error.replace(os.environ[name], "[REDACTED]")
        status.update(state="failed", error=error)
    write_json(status_file, status)
    yield json.dumps(status, ensure_ascii=False)
