"""Batch-select useful knowledge gaps, then write source-backed FAQs once."""

import asyncio
import copy
import json
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import Field

from . import qa_pipeline as pipeline
from .analyzer import digest
from .qa_benchmark import quiet_call
from .qa_data import (
    StrictModel,
    HistoryEvidence,
    FAQDraft,
    load_episodes,
    validate_history,
    validate_draft,
    export_text,
)
from .qa_sources import ResearchSources
from .qa_storage import transcript_event


class Topic(StrictModel):
    question: str = Field(min_length=1)
    signal: Literal["bad_answer", "inefficient", "frequent", "repeated_error"]
    reason: str = Field(min_length=1)
    knowledge_helpful: bool
    evidence: list[HistoryEvidence] = Field(min_length=1)


class Selection(StrictModel):
    reviewed_record_ids: list[str]
    topics: list[Topic]
    limitations: list[str]


def validate_selection(value, rows, expected):
    if len(value.reviewed_record_ids) != len(
        set(value.reviewed_record_ids)
    ) or set(value.reviewed_record_ids) != set(expected):
        raise ValueError("必须覆盖本批全部记录，不能遗漏或引入其他记录")
    for topic in value.topics:
        validate_history(topic.evidence, rows)
        ids = {e.record_id for e in topic.evidence}
        if not ids <= set(expected):
            raise ValueError("引用超出本批历史")
        if topic.signal == "frequent" and len(ids) < 2:
            raise ValueError("高频问题必须有至少两条不同记录支持")


def batches(rows):
    """Bound prompt size; full raw traces remain accessible by record ID."""
    groups, current, size = [], {}, 0
    for key, raw in rows.items():
        preview = {
            "record_id": key,
            "input": raw.get("input"),
            "answer": raw.get("answer"),
            "feedback": raw.get("feedback"),
            "received_at": raw.get("received_at"),
            "finished_at": raw.get("finished_at"),
            "trace_count": len(raw.get("trace", [])),
            "trace_preview": json.dumps(
                raw.get("trace", []), ensure_ascii=False
            )[:6000],
        }
        # Previews never replace raw evidence.
        for field in ("input", "answer", "feedback"):
            encoded = json.dumps(preview[field], ensure_ascii=False)
            if len(encoded) > 5000:
                preview[field] = {"truncated_preview": encoded[:5000]}
        length = len(json.dumps(preview, ensure_ascii=False))
        if current and (len(current) >= 10 or size + length > 48000):
            groups.append(current)
            current, size = {}, 0
        current[key] = preview
        size += length
    if current:
        groups.append(current)
    return groups


async def generate(
    source,
    output,
    config,
    model,
    store,
    *,
    semaphore,
    repo=None,
    offline=False,
    skills_dir=None,
    knowledge=None,
    limit=None,
    max_topics=None,
):
    rows, _ = load_episodes(Path(source), limit)
    skills = Path(skills_dir or pipeline.SKILLS)
    selection_skill = (skills / "qa-history-select/SKILL.md").read_text()
    write_skill = (skills / "qa-knowledge-write/SKILL.md").read_text()
    snapshot = ResearchSources(repo, knowledge, offline)
    binding = digest(
        {
            "version": 1,
            "rows": rows,
            "select": selection_skill,
            "write": write_skill,
            "sources": snapshot.binding,
            "max_topics": max_topics,
            "judge": config.model_dump(
                mode="json", include={"active_model", "thinking_level"}
            ),
        }
    )
    previous = store.get("compact_binding", binding)
    if previous != binding:
        raise ValueError("训练输入、Skill 或来源改变，请使用新的批次名称")
    store.set("compact_binding", binding)
    export_date = store.get("compact_date", date.today().isoformat())
    store.set("compact_date", export_date)
    saved = store.get("compact_export")
    if saved:
        if (
            not Path(output).exists()
            or digest(Path(output).read_text()) != saved["digest"]
        ):
            raise ValueError("已生成的 TXT 丢失或改变；请恢复文件或使用新批次")
        transcript_event("TXT 已复用", str(output))
        return saved["result"]
    if store.get("compact_user_export"):
        raise ValueError(
            "TXT 已人工修改；请跳过剩余主题或另开批次，避免覆盖修改"
        )
    from ..providers.retry_chat_model import RetryChatModel

    if isinstance(model, RetryChatModel):
        model = model.with_minimum_stream_timeouts(120)

    async def select(index, group):
        key = f"compact_selection:{index}"
        cached = store.get(key)
        subset = {k: rows[k] for k in group}
        if cached:
            value = Selection.model_validate(cached)
            validate_selection(value, subset, group)
            return value
        async with semaphore:
            value = await quiet_call(
                config,
                model,
                selection_skill,
                {
                    "mode": "select",
                    "records": list(group.values()),
                    "instruction": (
                        "这些是原始记录的摘要视图；轨迹与长字段可能截断。"
                        "需要时回读原始字段；不要把未展示当作不存在。"
                    ),
                },
                Selection,
                pipeline.history_tools(subset),
                store.work / "compact" / f"select-{index}",
                lambda v: validate_selection(v, subset, group),
                timeout=420,
                max_iters=8,
                max_tool_calls=12,
            )
        store.set(key, value.model_dump())
        transcript_event(
            "历史筛选完成",
            f"第 {index + 1} 批：{len(group)} 条历史，"
            f"发现 {len(value.topics)} 个候选主题。\n\n"
            + "\n".join(f"- {t.question}：{t.reason}" for t in value.topics),
        )
        return value

    async def joined(coroutines):
        tasks = [asyncio.create_task(c) for c in coroutines]
        try:
            return await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    groups = batches(rows)
    transcript_event(
        "筛选训练历史", f"{len(rows)} 条历史，分 {len(groups)} 批集中分析。"
    )
    selections = await joined(select(i, g) for i, g in enumerate(groups))
    candidates = [
        t for v in selections for t in v.topics if t.knowledge_helpful
    ]
    if len(groups) > 1:
        cached = store.get("compact_merged")
        if cached:
            merged = Selection.model_validate(cached)
        else:
            async with semaphore:
                merged = await quiet_call(
                    config,
                    model,
                    selection_skill,
                    {
                        "mode": "merge",
                        "reviewed_record_ids": list(rows),
                        "topics": [t.model_dump() for t in candidates],
                        "question_index": [
                            {
                                "record_id": k,
                                "question": r["input"]["question"],
                            }
                            for k, r in rows.items()
                        ],
                    },
                    Selection,
                    pipeline.history_tools(rows),
                    store.work / "compact/merge",
                    lambda v: validate_selection(v, rows, rows),
                    timeout=300,
                    max_iters=6,
                    max_tool_calls=8,
                )
            store.set("compact_merged", merged.model_dump())
        validate_selection(merged, rows, rows)
        candidates = [t for t in merged.topics if t.knowledge_helpful]
    # Semantic merging is a Skill task; exact duplicates need no second task.
    topics = list(
        {t.question.strip().casefold(): t for t in candidates}.values()
    )
    topics.sort(
        key=lambda t: (-len({e.record_id for e in t.evidence}), t.question)
    )
    if max_topics is not None:
        topics = topics[:max_topics]
    transcript_event(
        "待补充知识",
        f"共 {len(topics)} 个主题；只为可通过知识改善的问题生成 FAQ。",
    )

    async def write(topic):
        key = "compact_faq:" + digest(topic.model_dump())
        if digest(topic.model_dump()) in store.get("compact_skipped", {}):
            return {"skipped": True, "topic": topic.model_dump()}
        cached = store.get(key)
        if cached:
            return cached
        sources = copy.copy(snapshot)
        sources.loaded, sources.read_ids, sources.read_pages = {}, set(), {}
        subset = {e.record_id: rows[e.record_id] for e in topic.evidence}
        try:
            async with semaphore:
                draft = await quiet_call(
                    config,
                    model,
                    write_skill,
                    {
                        "task": topic.model_dump(),
                        "source_binding": sources.binding,
                        "instruction": (
                            "只为已识别信号补充知识；必要查证与 FAQ 写作一次完成。"
                            "已有可靠原文直接使用，缺少依据才补查。"
                        ),
                    },
                    FAQDraft,
                    [*sources.tools(), *pipeline.history_tools(subset)],
                    store.work / "compact" / digest(key)[:16],
                    lambda v: validate_draft(v, sources.loaded),
                    timeout=420,
                    max_iters=12,
                    max_tool_calls=20,
                )
            result = {
                "draft": draft.model_dump(),
                "sources": sources.loaded,
                "topic": topic.model_dump(),
            }
            if draft.status in {"ready", "no_addition", "conflict"}:
                store.set(key, result)
            transcript_event(
                "FAQ 已生成" if draft.status == "ready" else "FAQ 待处理",
                f"{topic.question}\n\n"
                + (
                    draft.answer
                    if draft.status == "ready"
                    else "；".join(draft.limitations)
                ),
            )
            return result
        except Exception as exc:
            transcript_event("FAQ 生成失败", f"{topic.question}：{exc}")
            return {"error": str(exc), "topic": topic.model_dump()}

    outcomes = await joined(write(topic) for topic in topics)
    store.set("compact_outcomes", outcomes)
    accepted = [
        (FAQDraft.model_validate(v["draft"]), v["sources"])
        for v in outcomes
        if v.get("draft", {}).get("status") == "ready"
    ]
    pending = [
        v
        for v in outcomes
        if "error" in v or v.get("draft", {}).get("status") == "needs_evidence"
    ]
    text, count = export_text(accepted, export_date)
    result = {
        "state": "completed" if not pending else "completed_with_errors",
        "exported": count,
        "pending": len(pending),
        "output": str(output),
    }
    # A partial TXT is useful, but must not masquerade as evaluation-ready.
    output = Path(output)
    if output.exists() and output.read_text() != text:
        owned = store.get("compact_partial")
        if owned != digest(output.read_text()):
            raise ValueError("TXT 已由用户修改，不能覆盖")
        # Atomic replacement only for our own explicitly partial export.
        temporary = output.with_suffix(".txt.tmp")
        temporary.write_text(text)
        temporary.replace(output)
    else:
        pipeline.save_export(output, text)
    store.set("compact_partial", digest(text))
    if not pending:
        store.set("compact_export", {"digest": digest(text), "result": result})
    transcript_event(
        "TXT 已生成",
        f"{output}\n\n{count} 条 FAQ；待处理 {len(pending)} 个主题。"
        + (
            f'\n证据不足的主题可先查看：/selflearn qa-skip "{output}"'
            if pending
            else ""
        ),
    )
    return result
