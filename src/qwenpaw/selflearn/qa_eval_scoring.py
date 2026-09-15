"""Independent scoring, paired review and audited adjudication."""

import asyncio
import ipaddress
import json
import socket
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from .analyzer import digest
from .qa_storage import write_json
from .qa_eval_data import (
    AnswerScore,
    agreement,
    file_hash,
    random_sample,
    targeted_sample,
    validate_score,
)
from .qa_pipeline import stage_call
from .qa_sources import lines_page


async def fetch_source(url):
    """Read a public HTTPS reference with a bounded response size."""
    import httpx

    async with httpx.AsyncClient(timeout=25, trust_env=False) as client:
        for _ in range(4):
            parts = urlsplit(url)
            if (
                parts.scheme != "https"
                or not parts.hostname
                or parts.username
                or parts.password
                or parts.port not in (None, 443)
            ):
                raise ValueError("来源必须是公开 HTTPS URL")
            addresses = await asyncio.to_thread(
                socket.getaddrinfo, parts.hostname, 443
            )
            if any(
                not ipaddress.ip_address(a[4][0]).is_global for a in addresses
            ):
                raise ValueError("不读取本地或内网来源")
            async with client.stream("GET", url) as response:
                if response.is_redirect:
                    url = urljoin(url, response.headers["location"])
                    continue
                response.raise_for_status()
                raw = bytearray()
                async for block in response.aiter_bytes():
                    raw.extend(block)
                    if len(raw) > 2_000_000:
                        raise ValueError("来源超过 2MB")
                return raw.decode("utf-8", errors="replace")
    raise ValueError("来源重定向过多")


def source_tool(reference, repo, folder):
    from agentscope.tool import FunctionTool

    sources = {s["id"]: s for s in reference.get("sources", [])}
    cache = {}

    async def read_reference_source(
        source_id: str, start_line: int = 1, search: str = ""
    ) -> str:
        """Read pinned source evidence by reference source ID, in pages."""
        if source_id not in sources:
            raise ValueError("来源 ID 不存在")
        source = sources[source_id]
        if source_id not in cache:
            text, origin = None, ""
            if "snapshot_text" in source:
                import hashlib

                snapshot = source["snapshot_text"]
                if not isinstance(snapshot, str) or hashlib.sha256(
                    snapshot.encode()
                ).hexdigest() != source.get("file_sha256"):
                    raise ValueError("冻结的评分来源内容与校验值不一致")
                text, origin = snapshot, "frozen-reference"
            path = source.get("path")
            commit = source.get("commit")
            if text is None and repo and path and commit:
                result = await asyncio.to_thread(
                    subprocess.run,
                    ["git", "-C", str(repo), "show", f"{commit}:{path}"],
                    capture_output=True,
                    timeout=20,
                )
                if result.returncode == 0:
                    import hashlib

                    expected = source.get("file_sha256")
                    if (
                        expected
                        and hashlib.sha256(result.stdout).hexdigest()
                        != expected
                    ):
                        return json.dumps(
                            {"unavailable": "源码与参考记录的 SHA-256 不一致"},
                            ensure_ascii=False,
                        )
                    text = result.stdout.decode("utf-8", errors="replace")
                    origin = f"git:{commit}:{path}"
            if text is None and source.get("url"):
                url = source["url"]
                if url.startswith("https://github.com/") and "/blob/" in url:
                    url = (
                        url.replace(
                            "https://github.com/",
                            "https://raw.githubusercontent.com/",
                            1,
                        )
                        .replace("/blob/", "/", 1)
                        .split("#")[0]
                    )
                try:
                    text = await fetch_source(url)
                    origin = url
                except Exception as exc:
                    return json.dumps(
                        {
                            "unavailable": type(exc).__name__,
                            "instruction": "未重新核实；影响分数且参考不足时保留待核实",
                        },
                        ensure_ascii=False,
                    )
            if text is None:
                return json.dumps(
                    {"unavailable": "无可读取来源"}, ensure_ascii=False
                )
            cache[source_id] = text
            (folder / "sources").mkdir(parents=True, exist_ok=True)
            write_json(
                folder / "sources" / f"{digest(source_id)[:12]}.json",
                {
                    "source_id": source_id,
                    "origin": origin,
                    "text": text,
                    "sha256": digest(text),
                },
            )
        return json.dumps(
            {
                "source_id": source_id,
                **lines_page(cache[source_id], start_line, search),
            },
            ensure_ascii=False,
        )

    return FunctionTool(read_reference_source, is_read_only=True)


async def score_pair(
    cases,
    answers,
    references,
    rules,
    prompt,
    skill,
    config,
    model,
    folder,
    repo=None,
    seed=42,
    notify=None,
    concurrency=1,
):
    """Use opaque group keys, without revealing baseline/candidate labels."""
    folder = Path(folder)
    first, second, final = {}, {}, {}

    async def call(group, key, phase, previous=None):
        if notify:
            await notify(phase, key, group, False)
        stage_dir = folder / group / phase / digest(key)[:20]
        stage_dir.mkdir(parents=True, exist_ok=True)
        result_path = stage_dir / "result.json"
        result_meta = stage_dir / "result.meta.json"
        answer = answers[group][key]
        reference = references[key]
        # Remove configuration labels, old answers and unrelated input fields.
        task = {
            "question": cases[key]["question"],
            "case_id": key,
            "new_answer": answer.get("new_answer", ""),
            "run_status": answer.get("status"),
            "run_error": answer.get("error"),
            "reference": {
                **reference,
                "sources": [
                    {k: v for k, v in s.items() if k != "snapshot_text"}
                    for s in reference.get("sources", [])
                ],
            },
        }
        if previous:
            task["reviews_to_reconcile"] = previous
            task["instruction"] = (
                "核查全部分歧，依据来源定稿。不能机械取平均；reason 写明复核理由。"
            )
        if result_path.exists():
            if not result_meta.exists() or json.loads(
                result_meta.read_text(encoding="utf-8")
            ) != {"sha256": file_hash(result_path), "task_hash": digest(task)}:
                raise ValueError("评分缓存与原始输入或校验记录不一致")
            value = AnswerScore.model_validate_json(
                result_path.read_text(encoding="utf-8")
            )
            validate_score(value, answer, reference)
        else:
            value = await stage_call(
                config,
                model,
                skill + "\n" + rules + "\n" + prompt,
                task,
                AnswerScore,
                [source_tool(reference, repo, stage_dir)],
                stage_dir,
                lambda r: validate_score(r, answer, reference),
            )
            write_json(result_path, value.model_dump())
            write_json(
                result_meta,
                {"sha256": file_hash(result_path), "task_hash": digest(task)},
            )
        if notify:
            await notify(phase, key, group, True)
        return value.model_dump()

    # Interleave groups rather than judging all baseline answers first.
    groups = sorted(answers)
    for group in groups:
        first[group], second[group], final[group] = {}, {}, {}
    if not 1 <= concurrency <= 8:
        raise ValueError("评分并发数必须为 1–8")
    semaphore = asyncio.Semaphore(concurrency)

    async def batch(jobs, target):
        errors = []

        async def run(job):
            group, key, phase, previous = job
            async with semaphore:
                try:
                    target[group][key] = await call(
                        group, key, phase, previous
                    )
                except Exception as exc:
                    errors.append(f"{phase} · {key}：{exc}")

        workers = [asyncio.create_task(run(job)) for job in jobs]
        try:
            await asyncio.gather(*workers)
        finally:
            for worker in workers:
                if not worker.done():
                    worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
        if errors:
            write_json(folder / "failures.json", errors)
            raise ValueError(
                f"{len(errors)} 项评分未完成，其他结果已保存；再次执行可续跑。"
                + "\n".join(errors[:5])
            )

    await batch([(g, k, "first", None) for k in cases for g in groups], first)
    random_ids = random_sample(cases, seed)
    targeted = targeted_sample(list(first.values()), random_ids)
    sampled = random_ids + sorted(targeted)
    write_json(
        folder / "sampling.json",
        {
            "seed": seed,
            "method": "sorted topics; seeded choice of sorted case IDs",
            "random": random_ids,
            "targeted": targeted,
        },
    )
    # Fresh contexts, without first scores, group labels or reasons.
    await batch(
        [(g, k, "second", None) for k in sampled for g in groups], second
    )
    adjudicated = {g: {} for g in groups}

    def disagreement(a, b):
        return any(
            a[k] != b[k]
            for k in ("score", "status", "major_error", "major_error_clause")
        )

    await batch(
        [
            (g, k, "adjudication", [first[g][k], second[g][k]])
            for k in sampled
            for g in groups
            if disagreement(first[g][k], second[g][k])
        ],
        adjudicated,
    )
    for group in groups:
        for key in cases:
            row = first[group][key]
            decided = dict(row)
            review = second[group].get(key)
            if review is not None:
                differs = disagreement(row, review)
                if differs:
                    decided = adjudicated[group][key]
                decided["double_scored"] = {
                    "first_score": row["score"],
                    "second_score": review["score"],
                    "delta": (
                        review["score"] - row["score"]
                        if row["score"] is not None
                        and review["score"] is not None
                        else None
                    ),
                    "first_major_error": row["major_error"],
                    "second_major_error": review["major_error"],
                    "selection": "random" if key in random_ids else "targeted",
                    "adjudication_reason": (
                        decided["reason"]
                        if differs
                        else "独立复评同分且关键错误判定一致"
                    ),
                }
            else:
                decided["double_scored"] = None
            decided.update(
                schema_version="qa_scoring_v1_2",
                reference_version=references[key].get(
                    "reference_version", "unknown"
                ),
            )
            final[group][key] = decided
        path = folder / group / "scored_results.jsonl"
        path.write_text(
            "".join(
                json.dumps(r, ensure_ascii=False) + "\n"
                for r in final[group].values()
            ),
            encoding="utf-8",
        )
    audit = {
        "scoring_finished_at": datetime.now(timezone.utc).isoformat(),
        "calibrated": True,
        "random_ids": random_ids,
        "targeted": targeted,
        "independent_contexts": True,
        "different_judge_models": False,
        "agreement": {
            g: agreement(first[g], second[g], random_ids) for g in groups
        },
        "score_hashes": {
            g: file_hash(folder / g / "scored_results.jsonl") for g in groups
        },
    }
    write_json(folder / "audit.json", audit)
    return final, audit
