"""QA history contracts and deterministic evidence/export checks."""

import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .analyzer import digest, field_text, pointer_value


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HistoryEvidence(StrictModel):
    record_id: str
    pointer: str
    quote: str = Field(min_length=1)


class FeedbackLink(StrictModel):
    target_record_id: str
    relation: Literal[
        "correction", "unresolved", "confirmation", "followup", "unclear"
    ]
    confidence: Literal["high", "medium", "low"]
    reason: str
    evidence: list[HistoryEvidence] = Field(min_length=1)


Kind = Literal[
    "knowledge_gap",
    "outdated",
    "scattered",
    "retrieval",
    "answering",
    "tool_failure",
    "other",
    "uncertain",
]


class QAFinding(StrictModel):
    problem: str
    kind: Kind
    assessment: Literal["confirmed", "suspected"]
    reason: str
    evidence: list[HistoryEvidence] = Field(min_length=1)


class QAAnalysis(StrictModel):
    topic: str
    summary: str
    answer_quality: Literal[
        "correct", "partial", "incorrect", "uncertain", "unanswered"
    ]
    feedback_links: list[FeedbackLink]
    findings: list[QAFinding]
    missing_evidence: list[str]


class FindingRef(StrictModel):
    record_id: str
    finding_index: int = Field(ge=0)


class KnowledgeTask(StrictModel):
    question: str = Field(min_length=1)
    problem: str
    kind: Kind
    action: Literal["research", "record_only", "needs_evidence"]
    reason: str
    related_record_ids: list[str] = Field(min_length=1)
    supporting_findings: list[FindingRef]
    research_questions: list[str]


class KnowledgePlan(StrictModel):
    summary: str
    tasks: list[KnowledgeTask]
    limitations: list[str]


class SourceEvidence(StrictModel):
    source_id: str
    quote: str = Field(min_length=1)


class FAQDraft(StrictModel):
    status: Literal["ready", "needs_evidence", "no_addition", "conflict"]
    question: str
    answer: str
    applicability: str
    evidence: list[SourceEvidence]
    limitations: list[str]


class FAQReview(StrictModel):
    decision: Literal["accept", "reject", "needs_evidence"]
    reason: str
    evidence: list[SourceEvidence]


def load_episodes(source: Path, limit=None):
    """Preserve raw fields; never fabricate sessions or feedback."""
    rows = {}
    duplicates = 0
    for line_number, line in enumerate(
        source.read_text(encoding="utf-8").splitlines(), 1
    ):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
            if not isinstance(raw, dict):
                raise ValueError("每行必须是对象")
            question = raw.get("input", {}).get("question")
            if not isinstance(question, str) or not question.strip():
                raise ValueError("缺少 input.question")
            if not isinstance(raw.get("trace", []), list):
                raise ValueError("trace 必须是数组")
            if raw.get("answer") is not None and not isinstance(
                raw["answer"], str
            ):
                raise ValueError("answer 必须是文本或 null")
            key = raw.get("record_id") or "derived-" + digest(raw)[:24]
            if not isinstance(key, str):
                raise ValueError("record_id 必须是文本")
            if key in rows:
                if rows[key] != raw:
                    raise ValueError(f"record_id 重复但内容不同：{key}")
                duplicates += 1
                continue
            rows[key] = raw
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError(f"第 {line_number} 行：{exc}") from exc
    if not rows:
        raise ValueError("JSONL 没有可分析的问答")
    return dict(list(rows.items())[:limit]), duplicates


def validate_history(evidence, rows):
    for item in evidence:
        if item.record_id not in rows:
            raise ValueError(f"未知 record_id：{item.record_id}")
        try:
            value = pointer_value(rows[item.record_id], item.pointer)
        except (ValueError, TypeError, KeyError, IndexError) as exc:
            raise ValueError(
                f"无效引用：{item.record_id}{item.pointer}"
            ) from exc
        if item.quote not in field_text(value):
            raise ValueError(
                f"引用不是连续原文：{item.record_id}{item.pointer}: {item.quote!r}"
            )


def validate_analysis(result, record_id, rows):
    for finding in result.findings:
        validate_history(finding.evidence, rows)
    for link in result.feedback_links:
        if link.target_record_id not in rows:
            raise ValueError("反馈目标不在输入记录中")
        validate_history(link.evidence, rows)
        cited = {e.record_id for e in link.evidence}
        if not {record_id, link.target_record_id} <= cited:
            raise ValueError("跨记录反馈关联必须引用当前记录和目标回答的证据")


def validate_plan(plan, analyses):
    for task in plan.tasks:
        if not set(task.related_record_ids) <= analyses.keys():
            raise ValueError("任务引用了未成功分析的记录")
        for ref in task.supporting_findings:
            if ref.record_id not in task.related_record_ids:
                raise ValueError("支持项必须属于 related_record_ids")
            if ref.finding_index >= len(analyses[ref.record_id]["findings"]):
                raise ValueError("不存在的 finding_index")
        if task.action == "research" and not task.research_questions:
            raise ValueError("research 任务需要具体待核实的问题")


def task_record(task, rows):
    value = task.model_dump()
    ids = set(task.related_record_ids)
    sessions = {
        rows[k].get("session_id") for k in ids if rows[k].get("session_id")
    }
    value.update(
        task_id=digest(value)[:16],
        occurrence_count=len(ids),
        finding_record_count=len(
            {r.record_id for r in task.supporting_findings}
        ),
        known_session_count=len(sessions),
        unknown_session_records=sum(
            not rows[k].get("session_id") for k in ids
        ),
    )
    return value


def validate_sources(evidence, sources):
    for item in evidence:
        source = sources.get(item.source_id)
        if source is None or item.quote not in source["text"]:
            raise ValueError(
                f"来源未读取或引用不匹配：{item.source_id}: {item.quote!r}"
            )


def validate_draft(draft, sources):
    validate_sources(draft.evidence, sources)
    if draft.status == "ready":
        if not all(
            [
                draft.question.strip(),
                draft.answer.strip(),
                draft.applicability.strip(),
                draft.evidence,
            ]
        ):
            raise ValueError("ready 必须包含问题、答案、适用条件和来源")
        if not any(sources[e.source_id].get("url") for e in draft.evidence):
            raise ValueError("ready 至少需要一个可追溯的来源链接")
        # Generate references from captured sources, not invented URLs.
        allowed = {
            s["url"].rstrip("/") for s in sources.values() if s.get("url")
        }
        for url in re.findall(r"https?://[^\s<>\]\)\"']+", draft.answer):
            if url.rstrip("/。.,；;") not in allowed and not any(
                url.rstrip("。.,；;") in sources[e.source_id]["text"]
                for e in draft.evidence
            ):
                raise ValueError(
                    f"答案含未读取的链接：{url}；参考链接由程序附加"
                )


RECORD_PATTERN = r"'create_time':\s*'\d{4}-\d{2}-\d{2}'"


def export_text(items, date):
    records, seen = [], set()
    for draft, sources in items:
        validate_draft(draft, sources)
        key = re.sub(r"\s+", "", draft.question).casefold()
        if key in seen:
            continue
        seen.add(key)
        answer = (
            draft.answer.strip() + "\n适用范围：" + draft.applicability.strip()
        )
        urls = list(
            dict.fromkeys(
                sources[e.source_id]["url"]
                for e in draft.evidence
                if sources[e.source_id].get("url")
            )
        )
        answer += "\n参考：" + "\n".join(urls)
        if re.search(
            r"(?:sk-[A-Za-z0-9_-]{20,}|Bearer\s+[A-Za-z0-9._-]{20,})", answer
        ):
            raise ValueError("候选含疑似真实凭据，不能导出")
        if re.search(RECORD_PATTERN, answer + draft.question):
            raise ValueError(
                "正文不能包含会被导入器误识别的 create_time 记录头"
            )
        question = draft.question.replace("\r", " ").replace("\n", " ")
        # The existing importer stores whole blocks, not Python literals.
        records.append(
            f"'create_time': '{date}', 'question': '{question}', "
            f"'answer': '{answer}'"
        )
    text = "\n\n".join(records) + ("\n" if records else "")
    if len(re.findall(RECORD_PATTERN, text)) != len(records):
        raise ValueError("TXT 记录边界校验失败")
    return text, len(records)
