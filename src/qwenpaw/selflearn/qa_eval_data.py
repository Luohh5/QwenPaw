"""Evaluation contracts, evidence checks, paired sampling and summaries."""

import difflib
import hashlib
import json
import math
import random
from pathlib import Path
from typing import Literal

from pydantic import Field, StrictBool, StrictInt

from .qa_data import StrictModel


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_rows(path):
    rows = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        key = value.get("case_id")
        if not isinstance(key, str) or not key or key in rows:
            raise ValueError(f"缺少或重复 case_id：{key}")
        rows[key] = value
    if not rows:
        raise ValueError(f"题集或结果为空：{path}")
    return rows


def match_rows(cases, rows):
    if cases.keys() != rows.keys():
        raise ValueError(
            f"题号不一致：缺失 {sorted(cases.keys() - rows.keys())}，"
            f"额外 {sorted(rows.keys() - cases.keys())}"
        )
    for key, case in cases.items():
        if rows[key].get("question") != case["question"]:
            raise ValueError(f"问题内容不一致：{key}")


class PointAssessment(StrictModel):
    point_id: str
    status: Literal[
        "met", "partial", "missing", "contradicted", "not_applicable"
    ]
    reason: str = Field(min_length=1)


class Deduction(StrictModel):
    answer_quote: str
    reason: str = Field(min_length=1)
    source_ids: list[str] = Field(min_length=1)
    impact: str = Field(min_length=1)


class AnswerScore(StrictModel):
    case_id: str
    # Providers may strip nullable branches. Omission must remain legal on
    # the wire; validation below still enforces the state-dependent contract.
    score: StrictInt | None = Field(
        default=None,
        ge=0,
        le=4,
        description="正常评分必须填 0–4；待核实或设施故障时省略，保存为 null。",
    )
    status: Literal["scored", "run_failed", "needs_review"]
    failure_cause: (
        Literal["bot_execution", "evaluation_infrastructure", "unknown"] | None
    ) = Field(
        default=None,
        description="仅运行失败时填写；正常评分时省略，保存为 null。",
    )
    point_assessments: list[PointAssessment]
    major_error: StrictBool
    major_error_clause: (
        Literal["core_conclusion", "main_path", "serious_harm"] | None
    ) = Field(
        default=None,
        description="仅 major_error=true 时填写；否则省略，保存为 null。",
    )
    major_error_reason: str
    deductions: list[Deduction]
    reason: str = Field(min_length=1)
    uncertainties: list[str]


class ComparisonNote(StrictModel):
    improvement: str
    regressions: str
    limitations: str


def validate_score(score, answer, reference):
    if score.case_id != reference["case_id"]:
        raise ValueError("评分题号不一致")
    if score.status == "needs_review":
        if score.score is not None or not score.uncertainties:
            raise ValueError("待核实必须 score=null 并说明缺失证据")
    elif score.status == "scored":
        if not (answer.get("new_answer") or "").strip():
            raise ValueError("空回答应按运行失败规则处理")
        if score.score is None:
            raise ValueError("status=scored 时必须填写 score（0–4 整数）")
        if score.failure_cause is not None:
            raise ValueError(
                "status=scored 时请省略 failure_cause（或填 null），"
                "不能填写 unknown 等失败原因；不因此改变分数或运行状态"
            )
        if answer.get("status") not in (None, "success"):
            raise ValueError("运行失败不能当作正常成功回答评分")
    else:
        if (
            answer.get("status") == "success"
            and (answer.get("new_answer") or "").strip()
        ):
            raise ValueError("正常返回的有效回答不能改记为运行失败")
        if not score.failure_cause:
            raise ValueError("运行失败必须记录原因")
        expected = (
            None if score.failure_cause == "evaluation_infrastructure" else 0
        )
        if score.score != expected:
            raise ValueError("运行失败分数不符合评分规则")
    if score.major_error:
        if (
            score.score is None
            or score.score > 1
            or not score.major_error_reason
            or not score.major_error_clause
            or not score.deductions
            or not any(d.answer_quote for d in score.deductions)
        ):
            raise ValueError("关键错误须有证据、影响说明，且最高 1 分")
    elif score.major_error_clause is not None:
        raise ValueError(
            "major_error=false 时请省略 major_error_clause（或填 null）；"
            "不能填写关键错误条款，不因此改判为关键错误"
        )
    points = {p["id"] for p in reference.get("required_points", [])}
    actual = [p.point_id for p in score.point_assessments]
    if score.status == "scored" and (
        set(actual) != points or len(actual) != len(points)
    ):
        raise ValueError("必须逐项核对参考要点，不得重复或遗漏")
    sources = {s["id"] for s in reference.get("sources", [])}
    for index, deduction in enumerate(score.deductions):
        if not set(deduction.source_ids) <= sources:
            raise ValueError("扣分引用了不存在的来源 ID")
        if deduction.answer_quote and deduction.answer_quote not in (
            answer.get("new_answer") or ""
        ):
            quote = deduction.answer_quote
            original = answer.get("new_answer") or ""
            # Only Markdown code delimiters may be restored, with one unique
            # match. Preserve all other characters; never paraphrase evidence.
            stripped = "".join(c for c in original if c != "`")
            needle = quote.replace("`", "")
            if needle and stripped.count(needle) == 1:
                positions = [i for i, c in enumerate(original) if c != "`"]
                start = stripped.index(needle)
                deduction.answer_quote = original[
                    positions[start] : positions[start + len(needle) - 1] + 1
                ]
            else:
                closest = difflib.get_close_matches(
                    quote, original.splitlines(), n=1, cutoff=0.2
                )
                raise ValueError(
                    f"deductions[{index}].answer_quote 不是 new_answer 的连续原文。"
                    "请逐字复制，保留反引号、标点和空格；遗漏项用空字符串。"
                    f"不匹配的引用：{quote!r}；可回读原文行：{closest}"
                )
    if score.status == "scored" and score.score < 4 and not score.deductions:
        raise ValueError("实质扣分必须记录依据及影响")


def random_sample(cases, seed):
    rng = random.Random(seed)
    topics = sorted({r["topic"] for r in cases.values()})
    return [
        rng.choice(sorted(k for k, r in cases.items() if r["topic"] == t))
        for t in topics
    ]


def targeted_sample(groups, random_ids):
    chosen = set(random_ids)
    reasons = {}
    for label, predicate in (
        ("关键错误", lambda r: r["major_error"]),
        ("待核实", lambda r: r["score"] is None),
        ("2/3 分边界", lambda r: r["score"] in (2, 3)),
    ):
        # Each group's existing categories must be covered.
        for rows in groups:
            candidates = sorted(k for k, r in rows.items() if predicate(r))
            if candidates and not chosen.intersection(candidates):
                selected = candidates[0]
                chosen.add(selected)
                reasons[selected] = label
    return reasons


def agreement(first, second, ids):
    pairs = [(first[k], second[k]) for k in ids]
    deltas = [
        b["score"] - a["score"]
        for a, b in pairs
        if a["score"] is not None and b["score"] is not None
    ]
    return {
        "sample_count": len(ids),
        "comparable": len(deltas),
        "unresolved_pairs": len(ids) - len(deltas),
        "exact_agreement": (
            sum(d == 0 for d in deltas) / len(deltas) if deltas else None
        ),
        "one_point": sum(abs(d) == 1 for d in deltas),
        "two_or_more": sum(abs(d) >= 2 for d in deltas),
        "signed_mean_delta": sum(deltas) / len(deltas) if deltas else None,
        "major_error_disagreements": sum(
            a["major_error"] != b["major_error"] for a, b in pairs
        ),
    }


def summarize(cases, answers, scores):
    values = [r["score"] for r in scores.values()]
    valid = [v for v in values if v is not None]
    durations = [
        r["duration_ms"]
        for r in answers.values()
        if type(r.get("duration_ms")) in (int, float)
        and math.isfinite(r["duration_ms"])
        and r["duration_ms"] >= 0
    ]
    result = {
        "count": len(cases),
        "scored": len(valid),
        "needs_review": [k for k, r in scores.items() if r["score"] is None],
        "total": (
            100 * sum(valid) / (4 * len(cases))
            if len(valid) == len(cases)
            else None
        ),
        "at_least_3": sum(v >= 3 for v in valid),
        "major_errors": [k for k, r in scores.items() if r["major_error"]],
        "run_failures": [
            k for k, r in scores.items() if r["status"] == "run_failed"
        ],
        "failure_causes": {
            cause: sum(
                r.get("failure_cause") == cause for r in scores.values()
            )
            for cause in (
                "bot_execution",
                "evaluation_infrastructure",
                "unknown",
            )
        },
        "run_success": sum(
            r.get("status") == "success" for r in answers.values()
        ),
        "run_status_unknown": sum(
            r.get("status") is None for r in answers.values()
        ),
        "effective_answers": sum(
            bool((r.get("new_answer") or "").strip()) for r in answers.values()
        ),
        "mean_duration_seconds": (
            sum(durations) / len(durations) / 1000 if durations else None
        ),
        "duration_count": len(durations),
        "duration_includes_failures": True,
    }
    for field in ("topic", "kind"):
        groups = {}
        for category in sorted({r[field] for r in cases.values()}):
            ids = [k for k, r in cases.items() if r[field] == category]
            known = [
                scores[k]["score"]
                for k in ids
                if scores[k]["score"] is not None
            ]
            groups[category] = {
                "count": len(ids),
                "scored": len(known),
                "points": sum(known),
                "maximum": 4 * len(ids),
                "mean": sum(known) / len(known) if known else None,
                "at_least_3": sum(v >= 3 for v in known),
                "at_least_3_ratio": (
                    sum(v >= 3 for v in known) / len(ids)
                    if len(known) == len(ids)
                    else None
                ),
            }
        result[field] = groups
    return result


def compare(cases, old_answers, new_answers, old, new):
    baseline = summarize(cases, old_answers, old)
    candidate = summarize(cases, new_answers, new)
    deltas = {
        k: (
            new[k]["score"] - old[k]["score"]
            if new[k]["score"] is not None and old[k]["score"] is not None
            else None
        )
        for k in cases
    }
    added_errors = sorted(
        set(candidate["major_errors"]) - set(baseline["major_errors"])
    )
    complete = baseline["total"] is not None and candidate["total"] is not None
    delta = candidate["total"] - baseline["total"] if complete else None
    return {
        "baseline": baseline,
        "candidate": candidate,
        "delta": delta,
        "case_deltas": deltas,
        "improved": [k for k, d in deltas.items() if d is not None and d > 0],
        "regressed": [k for k, d in deltas.items() if d is not None and d < 0],
        "new_major_errors": added_errors,
        "crossed_up": [
            k
            for k in cases
            if old[k]["score"] is not None
            and new[k]["score"] is not None
            and old[k]["score"] < 3 <= new[k]["score"]
        ],
        "crossed_down": [
            k
            for k in cases
            if old[k]["score"] is not None
            and new[k]["score"] is not None
            and new[k]["score"] < 3 <= old[k]["score"]
        ],
        "eligible": bool(complete and delta > 0 and not added_errors),
    }
