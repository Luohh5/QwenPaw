#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Infer unique historical PR-review snapshots from review summaries."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from collect_github_episodes import GitHub, historical_link, read_jsonl


def resolve(github: GitHub, episode: dict) -> dict | None:
    repo = episode["repo"]
    number = episode["pr_number"]
    pr = github.get(f"/repos/{repo}/pulls/{number}")
    commits = github.pages(
        f"/repos/{repo}/pulls/{number}/commits",
        per_page=50,
    )
    timeline = github.pages(f"/repos/{repo}/issues/{number}/timeline")
    link, _ = historical_link(
        github,
        repo,
        pr,
        commits,
        timeline,
        episode["review"],
        {},
    )
    return link


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="work/selflearn/episodes.jsonl")
    parser.add_argument(
        "--output",
        default="work/selflearn/snapshot_backfills.jsonl",
    )
    args = parser.parse_args()

    github = GitHub(os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN"))
    backfills = [
        result
        for episode in read_jsonl(Path(args.input))
        if episode["linkage"]["confidence"] == "low"
        if (result := resolve(github, episode))
    ]
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as file:
        for result in backfills:
            file.write(json.dumps(result, ensure_ascii=False) + "\n")
    print(f"wrote {len(backfills)} backfills to {output}")


if __name__ == "__main__":
    main()
