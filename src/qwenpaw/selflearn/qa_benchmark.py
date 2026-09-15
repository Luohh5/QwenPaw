"""Build independent, source-checked specialized references before learning."""

import asyncio
import copy
import hashlib
from pathlib import Path
from typing import Literal

from pydantic import Field

from . import qa_pipeline
from .analyzer import digest
from .qa_data import (
    StrictModel,
    SourceEvidence,
    HistoryEvidence,
    load_episodes,
    validate_history,
    validate_sources,
)
from .qa_datasets import read, write_rows, normalized
from .qa_eval_data import file_hash, load_rows, match_rows
from .qa_sources import ResearchSources
from .qa_storage import write_json, transcript_event


class RequiredPoint(StrictModel):
    id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    essential: bool
    source_ids: list[str] = Field(min_length=1)


class TestReference(StrictModel):
    status: Literal["ready", "needs_review"]
    question: str = Field(min_length=1)
    question_evidence: list[HistoryEvidence] = Field(min_length=1)
    topic: str = Field(min_length=1)
    kind: Literal["fact", "procedure", "troubleshooting"]
    reference_answer: str
    required_points: list[RequiredPoint]
    acceptable_alternatives: list[str]
    major_errors: list[str]
    evidence: list[SourceEvidence]
    limitations: list[str]


def validate_reference(value, key, rows, sources):
    validate_history(value.question_evidence, rows)
    if key not in {e.record_id for e in value.question_evidence}:
        raise ValueError("题干必须引用当前历史问题")
    validate_sources(value.evidence, sources.loaded)
    if value.status == "ready":
        if (
            not value.reference_answer.strip()
            or not value.required_points
            or not value.evidence
        ):
            raise ValueError("可用标准必须有参考答案、要点和证据")
        ids = [p.id for p in value.required_points]
        if len(ids) != len(set(ids)):
            raise ValueError("评分要点 ID 重复")
        cited = {e.source_id for e in value.evidence}
        if any(not set(p.source_ids) <= cited for p in value.required_points):
            raise ValueError("每个评分要点都必须关联已引用的来源")


async def quiet_call(*args, **kwargs):
    sink = qa_pipeline.PROGRESS.get()

    def quiet(raw):
        import json

        event = json.loads(raw)
        event["display"] = False
        sink(json.dumps(event, ensure_ascii=False))

    token = qa_pipeline.PROGRESS.set(quiet if sink else None)
    try:
        return await qa_pipeline.stage_call(*args, **kwargs)
    finally:
        qa_pipeline.PROGRESS.reset(token)


def check_benchmark(folder):
    folder = Path(folder)
    manifest = read(folder / "manifest.json")
    for filename, expected in manifest["files"].items():
        path = folder / filename
        if path.parent != folder or file_hash(path) != expected:
            raise ValueError(f"专用题集或评分标准已改变：{path}；请新建批次")
    match_rows(
        load_rows(folder / "references.jsonl"),
        load_rows(folder / manifest["cases_file"]),
    )
    return manifest


async def build_benchmark(
    folder,
    split,
    profile,
    config,
    model,
    store,
    repo=None,
    offline=False,
    skills_dir=None,
    concurrency=3,
    status_path=None,
    semaphore=None,
    suite="specialized",
    destination=None,
    benchmark_name=None,
):
    if not 1 <= concurrency <= 8:
        raise ValueError("concurrency 必须为 1–8")
    folder = Path(folder)
    if suite not in {"specialized", "train"}:
        raise ValueError("未知题集类型")
    destination = Path(destination) if destination else folder / suite
    benchmark_name = benchmark_name or split["name"]
    partition = "test" if suite == "specialized" else "train"
    if (destination / "manifest.json").exists():
        manifest = check_benchmark(destination)
        if manifest.get("suite", "specialized") != suite:
            raise ValueError("评分标准的题集类型不一致")
        if manifest["split_sha256"] != file_hash(folder / "split.json"):
            raise ValueError("测试标准对应的切分已改变")
        return destination, manifest
    rows, _ = load_episodes(folder / split[partition]["file"])
    snapshot = ResearchSources(repo, None, offline)
    skill = (
        Path(skills_dir or qa_pipeline.SKILLS)
        / (
            "qa-build-specialized-eval/SKILL.md"
            if suite == "specialized"
            else "qa-build-train-eval/SKILL.md"
        )
    ).read_text()
    general = Path(profile["references"])
    rules = (
        (general / "SCORING_RULES.md")
        .read_text()
        .replace("适用于固定70道日常题。", "适用于本批次固定专用测试题。")
        .replace(
            "每轮从10个主题各随机抽1题，共10题；",
            "每轮从本题集每个主题随机抽1题；",
        )
    )
    if suite == "train":
        rules = rules.replace("本批次固定专用测试题", "本批次训练集诊断题")
    prompt = (general / "SCORING_PROMPT.md").read_text()
    prompt = prompt.replace("70个case_id", "本题集全部case_id")
    prompt = prompt.replace("共10题", "数量以实际主题数为准")
    prompt = prompt.replace("10主题", "各主题")
    binding = {
        "split": file_hash(folder / "split.json"),
        "skill": skill,
        "rules": rules,
        "prompt": prompt,
        "sources": snapshot.binding,
        "judge": config.model_dump(
            mode="json", include={"active_model", "thinking_level"}
        ),
    }
    previous = store.get("reference_binding", binding)
    # A Skill improvement may resume this batch, while source/scoring changes
    # cannot mix standards. Record each retained reference's original method.
    if {k: v for k, v in previous.items() if k != "skill"} != {
        k: v for k, v in binding.items() if k != "skill"
    }:
        raise ValueError("未完成的标准生成配置或来源改变，请使用新的批次名称")
    for key in rows:
        if store.get("reference:" + key) and not store.get("method:" + key):
            store.set("method:" + key, digest(previous))
    store.set("method_binding:" + digest(previous), previous)
    store.set("method_binding:" + digest(binding), binding)
    store.set("reference_binding", binding)
    from ..providers.retry_chat_model import RetryChatModel

    if isinstance(model, RetryChatModel):
        model = model.with_minimum_stream_timeouts(120)
    counts = {"completed": 0, "failed": 0, "excluded": 0, "reused": 0}
    total = len(rows)
    semaphore = semaphore or asyncio.Semaphore(concurrency)

    def report(key, outcome, reused=False):
        counts[outcome] += 1
        counts["reused"] += int(reused)
        finished = counts["completed"] + counts["failed"] + counts["excluded"]
        text = (
            f"已处理 {finished}/{total}；已生成 {counts['completed']}，"
            f"失败 {counts['failed']}，待人工处理 {counts['excluded']}。"
        )
        if status_path:
            write_json(
                status_path,
                {
                    "state": "running",
                    "phase": "benchmark",
                    "total": total,
                    **counts,
                    "current": rows[key]["input"]["question"],
                    "progress": text,
                },
            )
        return text

    transcript_event(
        "准备测试标准", f"共 {total} 题，同时处理最多 {concurrency} 题。"
    )

    async def one(key, raw):
        cache_key = "reference:" + key
        cached = store.get(cache_key)
        if cached:
            progress = report(key, "completed", reused=True)
            transcript_event(
                "测试标准已复用", f"{raw['input']['question']}\n\n{progress}"
            )
            return "ready", cached
        sources = copy.copy(snapshot)
        sources.loaded, sources.read_ids, sources.read_pages = {}, set(), {}
        stage = store.work / "references" / digest(key)[:16]
        record = {k: v for k, v in raw.items() if k != "trace"}
        try:
            draft_key = "reference_draft:" + key
            saved = store.get(draft_key)
            # A validated draft survives an interrupted independent review.
            # Rebuild unfinished drafts after a Skill change.
            if saved and saved["binding"] == digest(binding):
                value = TestReference.model_validate(saved["value"])
                sources.loaded = saved["sources"]
                validate_reference(value, key, rows, sources)
            else:
                value = await quiet_call(
                    config,
                    model,
                    skill,
                    {
                        "mode": "build",
                        "record_id": key,
                        "question": raw["input"]["question"],
                        "record": record,
                        "instruction": (
                            "当前问题及上下文已提供；仅查证本题必需事实，"
                            "不读取训练集、TXT 或新旧测试回复。"
                        ),
                    },
                    TestReference,
                    [*qa_pipeline.history_tools(rows), *sources.tools()],
                    stage / "build",
                    lambda v: validate_reference(v, key, rows, sources),
                    timeout=420,
                    max_iters=12,
                    max_tool_calls=20,
                )
                if value.status == "ready":
                    store.set(
                        draft_key,
                        {
                            "binding": digest(binding),
                            "value": value.model_dump(),
                            "sources": {
                                sid: sources.loaded[sid]
                                for sid in {
                                    e.source_id for e in value.evidence
                                }
                            },
                        },
                    )
            if value.status != "ready":
                progress = report(key, "excluded")
                transcript_event(
                    "测试题待处理",
                    f"{raw['input']['question']}："
                    f"{'；'.join(value.limitations)}\n\n{progress}",
                )
                return "excluded", {
                    "record_id": key,
                    "reason": value.limitations or ["标准证据不足"],
                }
            used = {e.source_id for e in value.evidence}
            entry = value.model_dump(
                exclude={
                    "status",
                    "question_evidence",
                    "evidence",
                    "limitations",
                }
            )
            entry.update(
                case_id=suite + "-" + digest(key)[:16],
                history_record_id=key,
                reference_version=suite + "-" + benchmark_name,
                uncertainties=value.limitations,
                review_status="source_validated",
                question_evidence=[
                    e.model_dump() for e in value.question_evidence
                ],
                sources=[
                    {
                        "id": sid,
                        "url": sources.loaded[sid].get("url"),
                        "path": sources.loaded[sid].get("path"),
                        "commit": sources.loaded[sid].get("revision"),
                        "snapshot_text": sources.loaded[sid]["text"],
                        "file_sha256": hashlib.sha256(
                            sources.loaded[sid]["text"].encode()
                        ).hexdigest(),
                    }
                    for sid in sorted(used)
                ],
            )
            store.set(cache_key, entry)
            store.set("method:" + key, digest(binding))
            progress = report(key, "completed")
            transcript_event(
                "测试标准已生成",
                f"{raw['input']['question']}\n\n"
                f"{len(value.required_points)} 个评分要点；"
                f"引用校验通过。\n\n{progress}",
            )
            return "ready", entry
        except Exception as exc:
            reason = str(exc) or type(exc).__name__
            progress = report(key, "failed")
            transcript_event(
                "测试标准生成失败",
                f"{raw['input']['question']}：{reason}\n\n{progress}",
            )
            return "error", {"record_id": key, "reason": reason}

    async def limited(key, raw):
        async with semaphore:
            return await one(key, raw)

    workers = [asyncio.create_task(limited(k, r)) for k, r in rows.items()]
    try:
        outcomes = await asyncio.gather(*workers)
    finally:
        for worker in workers:
            if not worker.done():
                worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
    references = [v for status, v in outcomes if status == "ready"]
    excluded = [v for status, v in outcomes if status == "excluded"]
    errors = [v for status, v in outcomes if status == "error"]
    # Freeze exclusions before learning, never based on candidate scores.
    destination.mkdir(parents=True, exist_ok=True)
    if errors:
        write_json(
            destination / "pending.json",
            {"errors": errors, "excluded": excluded},
        )
        raise ValueError(
            "测试标准生成有执行失败；已完成部分保留，请重试原命令"
        )
    if not references:
        write_json(destination / "pending.json", {"excluded": excluded})
        raise ValueError(
            f"没有可用的 {suite} 题；原因见 {destination / 'pending.json'}，可重试或补充历史"
        )
    cases_name = benchmark_name + "_" + suite + ".jsonl"
    cases = [
        {k: r[k] for k in ("case_id", "question", "topic", "kind")}
        for r in references
    ]
    write_rows(destination / cases_name, cases)
    write_rows(destination / "references.jsonl", references)
    qa_pipeline.save_export(destination / "SCORING_RULES.md", rules)
    qa_pipeline.save_export(destination / "SCORING_PROMPT.md", prompt)
    manifest = {
        "schema_version": 1,
        "suite": suite,
        "split_sha256": file_hash(folder / "split.json"),
        "cases_file": cases_name,
        "count": len(cases),
        "excluded": excluded,
        "binding": digest(binding),
        "reference_bindings": {
            r["case_id"]: store.get(
                "method:" + r["history_record_id"], digest(binding)
            )
            for r in references
        },
        "files": {
            n: file_hash(destination / n)
            for n in (
                cases_name,
                "references.jsonl",
                "SCORING_RULES.md",
                "SCORING_PROMPT.md",
            )
        },
    }
    write_json(destination / "manifest.json", manifest)
    (destination / "pending.json").unlink(missing_ok=True)
    return destination, manifest


def overlap_report(train, cases):
    questions = {}
    for key, row in train.items():
        questions.setdefault(normalized(row["input"]["question"]), []).append(
            key
        )
        for m in row["input"].get("messages", []) or []:
            if (
                isinstance(m, dict)
                and m.get("role") == "user"
                and isinstance(m.get("content"), str)
            ):
                questions.setdefault(normalized(m["content"]), []).append(key)
    return [
        {
            "case_id": k,
            "history_record_ids": questions[normalized(c["question"])],
        }
        for k, c in cases.items()
        if normalized(c["question"]) in questions
    ]
