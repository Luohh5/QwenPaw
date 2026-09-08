#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build human-reviewable eval case drafts from collected Episodes."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


DEFECT_WORDS = re.compile(
    r"(?i)\b(?:p0|p1|blocker|crash|bug|broken|regression|runtime error|failing tests?)\b"
    r"|错误|崩溃|回归|失败|问题|缺陷|漏判|不工作",
)
STRENGTH = {"low": 0, "medium": 1, "high": 2}


def read_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.open(encoding="utf-8")
        if line.strip()
    ]


def human(values: list[dict]) -> list[dict]:
    return [
        item
        for item in values
        if item.get("user", {}).get("type") != "Bot"
        and item.get("_selflearn", {}).get("attribution")
        not in {"pre_episode_thread", "unknown_thread"}
    ]


def feedback(episode: dict) -> list[dict]:
    data = episode["feedback"]
    return (
        human(data["issue_comments_after_review"])
        + human(data["reviews_after_review"])
        + human(data["inline_comments_after_review"])
    )


def body_text(items: list[dict]) -> str:
    return "\n".join(item.get("body") or "" for item in items)


def has_changes_requested(items: list[dict]) -> bool:
    return any(item.get("state") == "CHANGES_REQUESTED" for item in items)


def has_approval_or_lgtm(items: list[dict]) -> bool:
    return any(
        item.get("state") == "APPROVED"
        or "lgtm" in (item.get("body") or "").lower()
        for item in items
    )


def compact(item: dict) -> dict:
    result = {
        "id": item.get("id"),
        "author": (item.get("user") or {}).get("login"),
        "body": item.get("body") or "",
        "url": item.get("html_url"),
    }
    for key in ("state", "created_at", "submitted_at", "path", "line"):
        if item.get(key) is not None:
            result[key] = item[key]
    return result


def classify(episode: dict) -> tuple[str, str, list[str]] | None:
    reviews = human(episode["feedback"]["reviews_after_review"])
    comments = feedback(episode)
    text = body_text(comments)
    commits = episode["feedback"]["commits_after_review"]
    verdict = episode["review"]["verdict"]
    outcome = episode["pr_outcome"]
    merged = outcome.get("merged_at_collection", outcome.get("merged", False))

    if verdict == "APPROVE" and (
        has_changes_requested(reviews)
        or (commits and DEFECT_WORDS.search(text))
    ):
        reasons = ["AI approved the PR"]
        if has_changes_requested(reviews):
            reasons.append("a human review later requested changes")
        if DEFECT_WORDS.search(text):
            reasons.append("human feedback contains an explicit defect signal")
        if commits:
            reasons.append("a commit followed the feedback")
        strength = "high" if len(reasons) >= 3 else "medium"
        return "missed_issue", strength, reasons

    if (
        verdict == "REQUEST_CHANGES"
        and merged
        and outcome.get("relation") == "within_episode"
        and not commits
        and has_approval_or_lgtm(reviews)
    ):
        return (
            "likely_overwarning",
            "medium",
            [
                "AI requested changes",
                "the PR was merged without a later commit",
                "a human later approved or said LGTM",
            ],
        )
    return None


def build_case(
    episode: dict,
    kind: str,
    strength: str,
    reasons: list[str],
) -> dict:
    snapshot = episode["snapshot"]
    data = episode["feedback"]
    reviews = human(data["reviews_after_review"])
    source = snapshot.get("source")
    linked = episode["linkage"].get("confidence") in {"medium", "high"}
    outcome = episode["pr_outcome"]
    merged = outcome.get("merged_at_collection", outcome.get("merged", False))
    return {
        "case_id": episode["episode_id"],
        "candidate_type": kind,
        "signal_strength": strength,
        "selection_reasons": reasons,
        "input": {
            "repo": episode["repo"],
            "pr_number": episode["pr_number"],
            "base_sha": snapshot.get("base_sha"),
            "head_sha": snapshot.get("head_sha"),
            "diff": snapshot.get("diff"),
            "snapshot_source": source,
        },
        "agent_output": episode["review"],
        "feedback_evidence": {
            "comments": [
                compact(item)
                for item in human(data["issue_comments_after_review"])
            ],
            "reviews": [compact(item) for item in reviews],
            "inline_comments": [
                compact(item)
                for item in human(data["inline_comments_after_review"])
            ],
            "commits": [
                {
                    "sha": item.get("sha"),
                    "message": (item.get("commit") or {}).get("message", ""),
                    "url": item.get("html_url"),
                }
                for item in data["commits_after_review"]
            ],
            "merged": merged and outcome.get("relation") == "within_episode",
        },
        "oracle": {
            "expected_verdict": None,
            "must_find": [],
            "must_not_block": [],
        },
        "verification": {
            "status": "needs_human_review",
            "head_sha_verified": linked,
            "snapshot_usable_for_replay": linked
            and bool(snapshot.get("diff")),
            "notes": "Confirm the reviewed head SHA before using this as a replay case.",
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="work/selflearn/episodes.jsonl")
    parser.add_argument(
        "--output",
        default="work/selflearn/eval_candidates.jsonl",
    )
    parser.add_argument(
        "--types",
        default="missed_issue,likely_overwarning",
        help="comma-separated candidate types",
    )
    parser.add_argument("--min-strength", choices=STRENGTH, default="medium")
    args = parser.parse_args()
    types = set(args.types.split(","))
    cases = []
    for episode in read_jsonl(Path(args.input)):
        result = classify(episode)
        if not result:
            continue
        kind, strength, reasons = result
        if kind in types and STRENGTH[strength] >= STRENGTH[args.min_strength]:
            cases.append(build_case(episode, kind, strength, reasons))

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as file:
        for case in cases:
            file.write(json.dumps(case, ensure_ascii=False) + "\n")
    print(f"wrote {len(cases)} candidate cases to {output}")


if __name__ == "__main__":
    main()
