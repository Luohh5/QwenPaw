"""Explicit user exclusion of insufficiently evidenced FAQ topics."""

from datetime import datetime, timezone
import json
import hashlib
import re
from pathlib import Path
import sqlite3
import zlib

from .analyzer import digest
from .qa_data import FAQDraft, RECORD_PATTERN, export_text
from .qa_eval_data import file_hash
from .qa_storage import ArtifactStore, workspace_lock, write_json


def outcomes_from_store(store):
    saved = store.get("compact_outcomes")
    if saved is not None:
        return saved
    # Older completed runs kept drafts in stage traces, not in metadata.
    files = dict(store.db.execute("SELECT path, data FROM files"))
    outcomes = []
    for path, data in files.items():
        if not path.startswith("compact/") or not path.endswith("/input.json"):
            continue
        request = json.loads(zlib.decompress(data)).get("task", {})
        topic = request.get("task")
        if not isinstance(topic, dict) or "signal" not in topic:
            continue
        cached = store.get("compact_faq:" + digest(topic))
        if cached:
            outcomes.append(cached)
            continue
        raw = files.get(path.replace("/input.json", "/trace.json"))
        trace = json.loads(zlib.decompress(raw)) if raw else {}
        replies = trace.get("replies", [])
        last = replies[-1] if replies else {}
        draft = last.get("structured_output") or {}
        if (
            not trace.get("error")
            and not trace.get("exception")
            and not last.get("error")
            and draft.get("status") == "needs_evidence"
        ):
            draft = FAQDraft.model_validate(draft).model_dump()
            outcomes.append({"topic": topic, "draft": draft})
        else:
            outcomes.append(
                {"topic": topic, "error": "未保存成功结果，请重试原命令"}
            )
    if not outcomes:
        raise ValueError("缺少主题执行记录，不能确认跳过范围")
    return outcomes


def original_export(reader, outcomes, expected):
    saved = reader.get("compact_user_export")
    if saved:
        text = saved["text"]
    else:
        ordered = sorted(
            outcomes,
            key=lambda v: (
                -len({e["record_id"] for e in v["topic"]["evidence"]}),
                v["topic"]["question"],
            ),
        )
        accepted = [
            (FAQDraft.model_validate(v["draft"]), v["sources"])
            for v in ordered
            if v.get("draft", {}).get("status") == "ready"
        ]
        text, _ = export_text(accepted, reader.get("compact_date"))
    if hashlib.sha256(text.encode()).hexdigest() != expected:
        raise ValueError("无法恢复原 TXT 快照，不能登记修改")
    return text


def skip_topics(
    root, source, skip_all=False, topic=(), accept_edited_txt=False
):
    from .qa_datasets import check_split, read
    from .qa_benchmark import check_benchmark

    root, source = Path(root).resolve(), Path(source).resolve()
    with workspace_lock(root):
        matches = []
        for path in (root.parent / "analyze/history_jsonl").glob(
            "*/round.json"
        ):
            record = read(path)
            if record.get("txt", {}).get("path") == str(source):
                matches.append((path, record))
        if len(matches) != 1:
            raise ValueError("TXT 没有唯一的批次记录，不能跳过")
        path, record = matches[0]
        current_bytes = source.read_bytes()
        current_hash = hashlib.sha256(current_bytes).hexdigest()
        current_text = current_bytes.decode("utf-8")
        edited = current_hash != record["txt"]["sha256"]
        if edited and not accept_edited_txt:
            raise ValueError(
                "TXT 已修改。确认使用当前修改版时，请在原命令末尾添加 "
                "--accept-edited-txt；会保留修改前后记录，不重跑模型。"
            )
        if edited and record.get("ready_for_evaluation"):
            raise ValueError(
                "本批已完成，请另开批次登记新的 TXT，保留评测溯源"
            )
        check_split(path.parent)
        if file_hash(path.parent / "split.json") != record["split_sha256"]:
            raise ValueError("切分记录已改变")
        benchmark_ready = (
            record.get("branches", {}).get("benchmark") == "completed"
        )
        if benchmark_ready:
            benchmark = check_benchmark(path.parent / "specialized")
            if (
                file_hash(path.parent / "specialized/manifest.json")
                != record["benchmark_sha256"]
                or benchmark["split_sha256"] != record["split_sha256"]
            ):
                raise ValueError("测试标准关联已改变")
        db = (
            root.parent
            / "analyze/.state"
            / (path.parent.name + "-dataset.sqlite")
        )
        if not db.is_file():
            raise ValueError("缺少执行记录，不能确认跳过")
        transcript = (
            root.parent / "analyze/sessions" / (path.parent.name + ".md")
        )

        # Preview never creates a store or changes the database.
        class Reader:
            def __init__(self, connection):
                self.db = connection

            def get(self, key, default=None):
                row = self.db.execute(
                    "SELECT value FROM meta WHERE key=?", (key,)
                ).fetchone()
                return json.loads(row[0]) if row else default

        connection = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)
        try:
            reader = Reader(connection)
            outcomes = outcomes_from_store(reader)
            accepted_export = reader.get("compact_user_export")
            expected_digest = (
                digest(accepted_export["text"])
                if accepted_export
                else reader.get("compact_partial")
            )
            if not edited and digest(current_text) != expected_digest:
                raise ValueError("TXT 与已保存导出不一致")
            prior_text = (
                original_export(reader, outcomes, record["txt"]["sha256"])
                if edited
                else None
            )
            skipped = reader.get("compact_skipped", {})
        finally:
            connection.close()
        candidates = {
            digest(v["topic"]): v
            for v in outcomes
            if not v.get("error")
            and v.get("draft", {}).get("status") == "needs_evidence"
            and digest(v["topic"]) not in skipped
        }
        if not skip_all and not topic:
            lines = [
                f"{k}：{v['topic']['question']}\n"
                f"原因：{'；'.join(v['draft']['limitations'])}"
                for k, v in candidates.items()
            ]
            return (
                "可跳过的证据不足主题：\n"
                + "\n\n".join(lines)
                + f'\n确认全部跳过：/selflearn qa-skip "{source}" --all'
                + (" --accept-edited-txt" if edited else "")
                if lines
                else "没有可跳过的证据不足主题。"
            )
        selected = set(candidates) if skip_all else set(topic)
        if selected - (set(candidates) | set(skipped)):
            raise ValueError(
                "主题 ID 不属于可跳过项；执行失败不能通过此命令跳过"
            )
        if not selected - set(skipped) and not edited:
            return "没有新的主题需要跳过。"
        stamp = datetime.now(timezone.utc).isoformat()
        for key in selected - set(skipped):
            value = candidates[key]
            skipped[key] = {
                "topic_id": key,
                "question": value["topic"]["question"],
                "reason": value["draft"]["limitations"],
                "decision": "user_skip",
                "decided_at": stamp,
                "history_evidence": value["topic"]["evidence"],
            }
        unresolved = [
            v
            for v in outcomes
            if v.get("error")
            or (
                v.get("draft", {}).get("status") == "needs_evidence"
                and digest(v["topic"]) not in skipped
            )
        ]
        txt_ready = not unresolved
        # Partial traces cannot prove a failed branch succeeded.
        if record.get("branches", {}).get("txt") not in {
            "completed",
            "completed_with_errors",
        }:
            raise ValueError("TXT 支线存在执行失败，请先重试")
        if edited:
            count = len(re.findall(RECORD_PATTERN, current_text))
            if not count or not current_text.lstrip().startswith(
                "'create_time':"
            ):
                raise ValueError("修改后的 TXT 缺少 FAQ 记录头，请检查格式")
            edit = {
                "decision": "user_accept_edited_txt",
                "decided_at": stamp,
                "before_sha256": record["txt"]["sha256"],
                "after_sha256": current_hash,
                "before_snapshot": (
                    f"txt_versions/{record['txt']['sha256']}.txt"
                ),
                "after_snapshot": f"txt_versions/{current_hash}.txt",
            }
            record.setdefault("txt_edits", []).append(edit)
            record["txt"]["sha256"] = current_hash
            record["exported"] = count
        record["skipped_topics"] = list(skipped.values())
        record["training_state"] = (
            "completed" if txt_ready else "completed_with_errors"
        )
        record["branches"]["txt"] = record["training_state"]
        record["ready_for_evaluation"] = (
            txt_ready and benchmark_ready and not record.get("errors")
        )
        store = ArtifactStore(db, transcript)
        with store.working():
            if file_hash(source) != current_hash:
                raise ValueError("TXT 在确认期间又被修改，请重新执行命令")
            if edited:
                store.put(edit["before_snapshot"], prior_text.encode())
                store.put(edit["after_snapshot"], current_bytes)
                store.set(
                    "compact_user_export",
                    {"text": current_text, "sha256": current_hash},
                )
            if txt_ready:
                store.set(
                    "compact_export",
                    {
                        "digest": digest(current_text),
                        "result": {
                            "state": "completed",
                            "exported": record["exported"],
                            "pending": 0,
                            "output": str(source),
                        },
                    },
                )
            store.set("compact_skipped", skipped)
            write_json(path, record)
            status = {
                "state": (
                    "completed"
                    if record["ready_for_evaluation"]
                    else "completed_with_errors"
                ),
                "phase": "done",
                "branches": record["branches"],
                "output": str(source),
                "exported": record["exported"],
                "pending": len(unresolved),
                "ready_for_evaluation": record["ready_for_evaluation"],
                "errors": record.get("errors", {}),
            }
            write_json(root / "qa_status.json", status)
            message = (
                (
                    "已登记当前修改版 TXT，修改前后快照已保留。\n"
                    if edited
                    else ""
                )
                + f"已按你的决定跳过 {len(selected)} 个主题，不纳入 TXT。\n"
                + "\n".join(skipped[k]["question"] for k in sorted(selected))
                + f"\n保留 {record['exported']} 条 FAQ。"
                + (
                    "可以继续评测。"
                    if record["ready_for_evaluation"]
                    else "仍有未完成项，请重试原命令。"
                )
            )
            store.append("### 用户确认跳过主题\n\n" + message)
        return message
