"""Training-set diagnostics, deliberately separate from baseline promotion."""

import asyncio
from pathlib import Path

from .analyzer import digest
from .qa_benchmark import build_benchmark
from .qa_eval_data import compare, file_hash, load_rows
from .qa_rounds import checked_round
from .qa_storage import ArtifactStore, safe_name


async def train_evaluate(
    root,
    source,
    config,
    old_collection=None,
    new_collection=None,
    name=None,
    concurrency=4,
    skills_dir=None,
    model=None,
):
    from . import qa_workflow as flow
    from ..runtime.builder import AgentBuilder

    root, source = Path(root), Path(source).resolve()
    if not 1 <= concurrency <= 8:
        raise ValueError("concurrency 必须为 1–8")
    round_path, record, split, _ = checked_round(root, source)
    batch = record["name"]
    name = safe_name(name or batch + "_train_eval")
    if bool(old_collection) != bool(new_collection):
        raise ValueError("请同时指定旧库和新库")
    expected = {}
    if not old_collection:
        comparison = flow.read(
            root / "comparisons" / batch / "comparison.json"
        )
        if not comparison:
            raise ValueError(
                "尚无本批新旧库比较记录，请同时指定 --old-collection 和 --new-collection"
            )
        if comparison["binding"]["txt_sha256"] != file_hash(source):
            raise ValueError("本批 TXT 与原新旧库比较记录不一致")
        database = comparison["database"]
        old_collection, new_collection = (
            database["base"],
            database["candidate"],
        )
        expected = {
            old_collection: database["base_fingerprint"],
            new_collection: database["candidate_fingerprint"],
        }
    if old_collection == new_collection:
        raise ValueError("旧库和新库不能是同一个 collection")
    profile = flow.profile_for(root)
    fingerprints = {}
    for collection in (old_collection, new_collection):
        safe_name(collection)
        fp = (
            await flow.legacy.backend(
                profile, "inspect", collection=collection
            )
        )["fingerprint"]
        if collection in expected and fp != expected[collection]:
            raise ValueError("知识库与本批原比较记录不一致")
        fingerprints[collection] = fp
    output = root / "comparisons" / name
    binding = {
        "round_sha256": file_hash(round_path),
        "txt_sha256": file_hash(source),
        "train_sha256": file_hash(round_path.parent / split["train"]["file"]),
        "collections": {"old": old_collection, "new": new_collection},
        "fingerprints": fingerprints,
        "execution": await flow.legacy.backend(
            profile, "config", **flow.legacy.execution_args(profile)
        ),
        "judge": flow.judge_config(config),
    }
    run = flow.read(output / "run.json")
    if run and run != binding:
        raise ValueError(
            "训练评测配置或产物改变，请用 --name 指定新的评测名称"
        )
    flow.write_json(output / "run.json", binding)
    flow.transcript_event(
        "训练集补充评测",
        f"{old_collection} → {new_collection}\n仅作诊断，不更新 baseline。",
    )
    if model is None:
        model, _ = await asyncio.to_thread(AgentBuilder().build_model, config)
    store = ArtifactStore(output / ".checkpoint.sqlite", output / "session.md")
    with store.working():
        references, manifest = await build_benchmark(
            round_path.parent,
            split,
            profile,
            config,
            model,
            store,
            repo=profile.get("repo"),
            skills_dir=skills_dir,
            concurrency=concurrency,
            suite="train",
            destination=round_path.parent / "train_eval" / name,
            benchmark_name=name,
        )
    p = dict(
        profile,
        cases=str(references / manifest["cases_file"]),
        references=str(references),
        baseline_answers=None,
    )
    method = flow.score_method(references, config, skills_dir)
    completed = output / "comparison.json"
    saved = flow.read(completed)
    if saved:
        if saved["binding"] != binding or saved["method_hash"] != digest(
            method
        ):
            raise ValueError("评分方法已变化，请用 --name 指定新的评测名称")
        for path, sha in saved["artifacts"].items():
            if file_hash(path) != sha:
                raise ValueError("训练评测产物已改变")
        flow.transcript_event(
            "复用训练集评测", (output / "REPORT.md").read_text()
        )
        return completed
    answers, scores, score_paths, artifacts = {}, {}, {}, {}
    for label, collection in (
        ("old", old_collection),
        ("new", new_collection),
    ):
        flow.transcript_event("评测训练集", f"{label}：{collection}")
        answers[label] = await flow.publish_answers(root, p, collection)
        score_name = answers[label].stem
        existing = flow.registry(root)["scores"].get(score_name)
        if existing and existing["method_hash"] != digest(method):
            score_name += "--" + digest(method)[:8]
        scores[label] = flow.checked_score(
            root, answers[label], method
        ) or await flow.score_answers(
            root,
            answers[label],
            references,
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
            and v["answers"] == str(answers[label])
            and v["method_hash"] == digest(method)
        )
        score_paths[label] = entry["score_file"]
        for section, key in (
            ("answers", answers[label].stem),
            ("scores", Path(entry["score_file"]).parent.name),
        ):
            value = flow.registry(root)[section][key]
            flow.record(
                root,
                section,
                key,
                dict(value, suite="train", batch=batch, diagnostic_only=True),
            )
        artifacts[str(answers[label])] = file_hash(answers[label])
        artifacts[entry["score_file"]] = file_hash(entry["score_file"])
    checked_round(root, source)
    if file_hash(round_path) != binding["round_sha256"] or digest(
        flow.score_method(references, config, skills_dir)
    ) != digest(method):
        raise ValueError("评测期间批次或评分标准发生变化")
    for collection, fp in fingerprints.items():
        if (
            await flow.legacy.backend(
                profile, "inspect", collection=collection
            )
        )["fingerprint"] != fp:
            raise ValueError("评测期间知识库发生变化")
    cases = flow.legacy.enrich_cases(
        load_rows(p["cases"]), load_rows(references / "references.jsonl")
    )
    result = compare(
        cases,
        load_rows(answers["old"]),
        load_rows(answers["new"]),
        scores["old"]["scores"],
        scores["new"]["scores"],
    )
    result.update(
        schema_version=1,
        suite="train",
        diagnostic_only=True,
        promoted=False,
        binding=binding,
        method_hash=digest(method),
        old_score_file=score_paths["old"],
        new_score_file=score_paths["new"],
        references=str(references),
        excluded=manifest["excluded"],
        artifacts=artifacts,
    )
    text = (
        "# 训练集补充评测\n\n仅用于诊断；训练历史参与过知识生成，不代表泛化能力，不更新 baseline。\n\n"
        "| 题数 | Old | New | 变化 |\n|---:|---:|---:|---:|\n"
        f"| {manifest['count']} | {result['baseline']['total']} | "
        f"{result['candidate']['total']} | {result['delta']} |\n\n"
        f"改善题：{result['improved']}\n\n退步题：{result['regressed']}\n\n"
        f"新增关键错误：{result['new_major_errors']}\n\n"
        f"未纳入题数：{len(manifest['excluded'])}\n\n"
        f"旧评分：{score_paths['old']}\n\n新评分：{score_paths['new']}\n"
    )
    flow.qa_pipeline.save_export(output / "REPORT.md", text)
    flow.write_json(completed, result)
    flow.transcript_event("训练集评测结论", text)
    return completed
