#!/usr/bin/env python3
"""Audit and migrate an existing unfinished evaluation to the named layout.

No model calls. --apply creates a dated copy of the candidate collection,
archives the old directory tree, and imports validated first-score caches.
Original directories are relocated intact after verification; no original is deleted.
"""

import argparse
import asyncio
from contextlib import ExitStack
from datetime import datetime
import fcntl
import hashlib
import json
from pathlib import Path
import subprocess
import zipfile

from qwenpaw.config.config import load_agent_config
from qwenpaw.selflearn import qa_workflow as flow
from qwenpaw.selflearn.qa_eval_data import AnswerScore, validate_score
from qwenpaw.selflearn.qa_storage import (
    ArtifactStore,
    period_name,
    result_text,
    workspace_lock,
    write_json,
)


def ensure(condition, message):
    if not condition:
        raise ValueError(message)


def clone_candidate(profile, source, target, apply):
    # Use Alias's installed Qdrant dependency, and exactly its copy checks.
    code = """
import importlib.util,json,sys
from pathlib import Path
spec=importlib.util.spec_from_file_location('knowledge_eval',Path(sys.argv[1])/'eval/knowledge_eval.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
db=m.client()
try:
 before=m.snapshot(db,sys.argv[2])
 target=sys.argv[3]
 if sys.argv[4]=='apply':
  if not db.collection_exists(target):m.clone(db,sys.argv[2],target,before)
  after=m.snapshot(db,target)
  m.verify_copy(before,after)
  if before['points']!=after['points']:raise ValueError('Copied points differ')
  print(json.dumps({'fingerprint':after['fingerprint'],'original_fingerprint':before['fingerprint'],'count':after['count']}))
 else:print(json.dumps({'fingerprint':before['fingerprint'],'count':before['count']}))
finally:db.close()
"""
    p = subprocess.run(
        [
            str(Path(profile["alias_root"]) / ".venv/bin/python"),
            "-c",
            code,
            profile["alias_root"],
            source,
            target,
            "apply" if apply else "check",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(p.stdout.strip().splitlines()[-1])


async def main(args):
    workspace = Path(args.workspace).expanduser().resolve()
    root = workspace / "selflearn"
    run = Path(args.legacy_run).expanduser().resolve()
    ensure(
        run.is_relative_to(root / "eval/runs"),
        "Legacy run must be under this workspace",
    )
    record_path = root / "migration.json"
    if record_path.exists():
        print(
            json.dumps(
                {"already_migrated": True, "record": str(record_path)},
                ensure_ascii=False,
            )
        )
        return
    with ExitStack() as stack:
        stack.enter_context(workspace_lock(root))
        for p in [root / "qa.lock", root / "eval/eval.lock"]:
            if p.exists():
                f = stack.enter_context(p.open("rb"))
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        manifest = flow.read(run / "manifest.json")
        profile = flow.read(root / "eval/config.json")
        ensure(
            not manifest["completed"],
            "Use an explicit completed-baseline import for completed runs",
        )
        ensure(profile == manifest["binding"]["profile"], "Profile changed")
        score_binding = flow.read(run / "scoring_binding.json")
        config = load_agent_config(args.agent)
        ensure(
            flow.judge_config(config)
            == {
                k: score_binding["judge"][k] for k in flow.judge_config(config)
            },
            "Judge model/config changed",
        )
        ensure(
            flow.legacy.tree_hash(Path(profile["references"]))
            == score_binding["references"],
            "References changed",
        )
        method = flow.score_method(profile["references"], config)
        database = flow.read(run / "database/result.json")
        execution = await flow.legacy.backend(
            profile, "config", **flow.legacy.execution_args(profile)
        )
        ensure(
            execution == manifest["binding"]["execution"],
            "Answer execution changed",
        )
        for c, fp in [
            (database["base"], database["base_fingerprint"]),
            (database["candidate"], database["candidate_fingerprint"]),
        ]:
            ensure(
                (await flow.legacy.backend(profile, "inspect", collection=c))[
                    "fingerprint"
                ]
                == fp,
                "Collection changed: " + c,
            )
        qa_runs = []
        for p in (root / "qa").glob("*/knowledge_additions.txt"):
            if flow.file_hash(p) == flow.file_hash(run / "A.txt"):
                qa_runs.append(p.parent)
        ensure(
            len(qa_runs) == 1,
            "Cannot uniquely identify the history source for this TXT",
        )
        qa = qa_runs[0]
        qa_manifest = flow.read(qa / "manifest.json")
        history = Path(qa_manifest["source"])
        name = period_name(history, args.name)
        target = "qwenpaw_faq_" + name
        rows = flow.load_rows(run / "cases.jsonl")
        refs = flow.load_rows(Path(profile["references"]) / "references.jsonl")
        ensure(
            rows
            == flow.legacy.enrich_cases(
                flow.load_rows(profile["cases"]), refs
            ),
            "Case content changed",
        )
        prepared = []
        for group, collection in [
            ("group_1", database["base"]),
            ("group_2", target),
        ]:
            source = run / group / "answers.jsonl"
            meta = flow.read(source.with_suffix(".manifest.json"))
            ensure(
                meta["answers_sha256"] == flow.file_hash(source)
                and meta["execution"] == execution,
                "Answer metadata changed",
            )
            answers = flow.load_rows(source)
            flow.match_rows(rows, answers)
            scores = []
            for stage in sorted((run / "scoring" / group / "first").iterdir()):
                if not (stage / "result.json").exists():
                    continue
                result = flow.read(stage / "result.json")
                task = flow.read(stage / "input.json")["task"]
                ensure(
                    flow.read(stage / "result.meta.json")
                    == {
                        "sha256": flow.file_hash(stage / "result.json"),
                        "task_hash": flow.digest(task),
                    },
                    "Score cache invalid",
                )
                ensure(
                    task["new_answer"]
                    == answers[result["case_id"]]["new_answer"],
                    "Score answer differs",
                )
                value = AnswerScore.model_validate(result)
                validate_score(
                    value, answers[result["case_id"]], refs[result["case_id"]]
                )
                ensure(
                    value.model_dump() == result,
                    "Score would require normalization; do not silently rewrite",
                )
                scores.append((stage, result))
            prepared.append((group, collection, source, meta, scores))
        old_directories = [
            p
            for p in [
                root / "qa",
                root / "eval",
                workspace / "eval/scoring_baseline_001",
                workspace / "eval/scoring_v1_2_baseline_001",
            ]
            if p.exists()
        ]
        inventory = {
            str(p.relative_to(workspace)): flow.file_hash(p)
            for d in old_directories
            for p in d.rglob("*")
            if p.is_file() and p.name != ".DS_Store"
        }
        print(
            json.dumps(
                {
                    "ready": True,
                    "name": name,
                    "answers": sum(
                        len(flow.load_rows(x[2])) for x in prepared
                    ),
                    "first_scores": sum(len(x[4]) for x in prepared),
                    "archive_files": len(inventory),
                    "candidate": target,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        if not args.apply:
            return
        archive = (
            root / "archive" / f"{datetime.now().date()}-before-layout.zip"
        )
        archive.parent.mkdir(parents=True, exist_ok=True)
        ensure(
            not archive.exists(),
            "Archive already exists; inspect incomplete migration before retrying",
        )
        with zipfile.ZipFile(
            archive, "w", compression=zipfile.ZIP_DEFLATED
        ) as z:
            for relative in inventory:
                z.write(workspace / relative, relative)
        with zipfile.ZipFile(archive) as z:
            ensure(z.testzip() is None, "Archive CRC failed")
            for relative, h in inventory.items():
                ensure(
                    hashlib.sha256(z.read(relative)).hexdigest() == h,
                    "Archive hash mismatch",
                )
        copied = clone_candidate(profile, database["candidate"], target, True)
        ensure(
            copied["original_fingerprint"]
            == database["candidate_fingerprint"],
            "Candidate changed while copying",
        )
        history_new = workspace / "analyze/history_jsonl" / (name + ".jsonl")
        txt_new = workspace / "analyze/txt" / (name + ".txt")
        flow.immutable_copy(history, history_new)
        flow.immutable_copy(run / "A.txt", txt_new)
        transcript = workspace / "analyze/sessions" / (name + ".md")
        store = ArtifactStore(
            workspace / "analyze/.state" / (name + ".sqlite"), transcript
        )
        with store.working() as work:
            store.append(
                "# 历史问答分析（原运行记录导入）\n\n输入："
                + str(history_new)
                + "\n\nTXT："
                + str(txt_new)
                + "\n\n以下是原运行保存的实际分析和工具记录；没有重新执行模型。"
            )
            # Preserve all non-context evidence in one file; expose results in
            # the transcript, without duplicating every input prompt there.
            for p in sorted(qa.rglob("*.json")):
                if "context" in p.relative_to(qa).parts:
                    continue
                store.put("legacy/" + str(p.relative_to(qa)), p.read_bytes())
                if p.name == "trace.json":
                    for reply in flow.read(p).get("replies", []):
                        value = reply.get("structured_output")
                        if value:
                            task = flow.read(p.parent / "input.json", {}).get(
                                "task", {}
                            )
                            label = (
                                task.get("question")
                                or task.get("record", {})
                                .get("input", {})
                                .get("question")
                                or task.get("task", {}).get("question")
                                or "归纳与核验"
                            )
                            store.append(
                                "## "
                                + label
                                + "\n\n```json\n"
                                + json.dumps(
                                    value, ensure_ascii=False, indent=2
                                )
                                + "\n```"
                            )
                if p.name == "sources.json":
                    store.append(
                        "## 核查来源\n\n```json\n" + p.read_text() + "\n```"
                    )
            outcome = flow.read(qa / "results.json")
            store.set(
                "imported_export",
                {
                    "source_sha256": flow.file_hash(history_new),
                    "output_sha256": flow.file_hash(txt_new),
                    "output": str(txt_new),
                    "exported": outcome["exported"],
                    "errors": len(outcome["errors"]),
                    "archive": str(archive),
                    "original_run": str(qa),
                },
            )
            store.append(
                "## 导入说明\n\n原会话未保存的过程不能补造。完整原始文件已归档；本轮已有 "
                + str(outcome["exported"])
                + " 条通过核验的知识，分析失败记录 "
                + str(len(outcome["errors"]))
                + " 条。"
            )
        mapping = []
        for group, collection, source, meta, scores in prepared:
            answer_id = flow.answer_name(profile, collection)
            destination = workspace / "eval" / (answer_id + ".jsonl")
            flow.immutable_copy(source, destination)
            fingerprint = (
                database["base_fingerprint"]
                if group == "group_1"
                else copied["fingerprint"]
            )
            entry = {
                **meta,
                "path": str(destination),
                "collection": collection,
                "cases_sha256": flow.file_hash(profile["cases"]),
                "original_cases_sha256": meta.get("cases_sha256"),
                "collection_fingerprint": fingerprint,
                "imported_from": str(source),
                "original_collection": meta["collection"],
                "original_collection_fingerprint": meta[
                    "collection_fingerprint"
                ],
                "mapping_note": (
                    "候选为原知识库的逐点一致副本；回答内容与原运行完全相同"
                    if group == "group_2"
                    else "原运行结果原样导入"
                ),
            }
            flow.record(root, "answers", answer_id, entry)
            folder = root / "score" / answer_id
            store = ArtifactStore(
                folder / ".checkpoint.sqlite", folder / "session.md"
            )
            with store.working() as work:
                store.set(
                    "binding",
                    {
                        "answers_sha256": flow.file_hash(destination),
                        "method_hash": flow.digest(method),
                    },
                )
                store.set(
                    "import",
                    {
                        "original_run": str(run),
                        "original_scoring_binding": manifest["binding"][
                            "scoring"
                        ],
                        "first_scores": len(scores),
                        "note": "首评标准、答案和评委配置一致；保留原始首评。新流程仅调整并发、引用格式纠错和文件组织。复评未完成，不能当作最终分数。",
                    },
                )
                store.put(
                    "legacy_method.json",
                    json.dumps(score_binding, ensure_ascii=False).encode(),
                )
                for p in Path(profile["references"]).rglob("*"):
                    if p.is_file():
                        store.put(
                            "references/"
                            + str(p.relative_to(profile["references"])),
                            p.read_bytes(),
                        )
                store.append(
                    "# 评分过程（续接旧运行）\n\n回答："
                    + str(destination)
                    + "\n\n原首评已完成 "
                    + str(len(scores))
                    + " 题，独立复评尚未完成。以下均为原评分，没有重新打分。"
                )
                for stage, result in scores:
                    for p in stage.rglob("*.json"):
                        if "context" not in p.relative_to(stage).parts:
                            store.put(
                                "scoring/answers/first/"
                                + stage.name
                                + "/"
                                + str(p.relative_to(stage)),
                                p.read_bytes(),
                            )
                    store.append(
                        "## "
                        + result["case_id"]
                        + " 首评\n\n"
                        + result_text(AnswerScore.model_validate(result))
                    )
            flow.record(
                root,
                "scores",
                answer_id,
                {
                    "answers": str(destination),
                    "answers_sha256": flow.file_hash(destination),
                    "references": profile["references"],
                    "method_hash": flow.digest(method),
                    "score_file": str(folder / "score.json"),
                    "session": str(folder / "session.md"),
                    "status": "incomplete",
                    "completed_first_scores": len(scores),
                    "summary": None,
                    "imported_from": str(run / "scoring" / group),
                },
            )
            mapping.append(
                {
                    "original": str(source),
                    "answers": str(destination),
                    "score_folder": str(folder),
                    "first_scores": len(scores),
                }
            )
        write_json(root / "config.json", profile)
        write_json(
            root / "baseline.json",
            {
                "collection": database["base"],
                "fingerprint": database["base_fingerprint"],
                "answers": mapping[0]["answers"],
                "score_file": None,
                "summary": None,
                "status": "awaiting_completed_score",
            },
        )
        new_database = {
            **database,
            "candidate": target,
            "candidate_fingerprint": copied["fingerprint"],
        }
        store = ArtifactStore(
            root / ".state" / ("collection-" + name + ".sqlite"), transcript
        )
        with store.working() as work:
            store.set(
                "binding",
                {
                    "source": str(txt_new),
                    "sha256": flow.file_hash(txt_new),
                    "baseline": database["base"],
                    "candidate": target,
                },
            )
            write_json(work / "database/result.json", new_database)
            store.set("result", new_database)
        # Last verification: replacement answers and copied TXT remain byte-identical.
        for item, original in zip(mapping, prepared):
            ensure(
                flow.file_hash(item["answers"]) == flow.file_hash(original[2]),
                "Replacement changed",
            )
        ensure(
            flow.file_hash(txt_new) == flow.file_hash(run / "A.txt"),
            "TXT changed",
        )
        for relative, h in inventory.items():
            ensure(
                flow.file_hash(workspace / relative) == h,
                "Original changed during migration",
            )
        record = {
            "schema_version": 2,
            "date": datetime.now().isoformat(),
            "archive": str(archive),
            "archive_sha256": flow.file_hash(archive),
            "archived_files": inventory,
            "history": str(history_new),
            "txt": str(txt_new),
            "session": str(transcript),
            "candidate": target,
            "original_candidate": database["candidate"],
            "answers": mapping,
            "preserved_original_collection": True,
        }
        write_json(record_path, record)
        originals = (
            root / "archive" / (str(datetime.now().date()) + "-originals")
        )
        for p in old_directories:
            destination = originals / p.relative_to(workspace)
            ensure(
                not destination.exists(),
                "Original archive path already exists",
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            p.rename(destination)
        record["originals_retained_at"] = str(originals)
        write_json(record_path, record)
        write_json(
            root / "qa_status.json",
            {
                "state": "interrupted",
                "phase": "evaluate",
                "current": "目录整理完成；已有回答和首评已导入，等待继续评分",
                "output": str(txt_new),
            },
        )
        print(
            json.dumps(
                {
                    "migrated": True,
                    "record": str(record_path),
                    "txt": str(txt_new),
                    "answers": mapping,
                    "candidate": target,
                },
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--legacy-run", required=True)
    parser.add_argument("--agent", default="default")
    parser.add_argument("--name")
    parser.add_argument("--apply", action="store_true")
    asyncio.run(main(parser.parse_args()))
