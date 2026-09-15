"""Frozen, conversation-grouped train/test splits for a named QA round."""

import json
from collections import Counter
import re
from pathlib import Path

from pydantic import Field

from .analyzer import digest
from .qa_data import (
    HistoryEvidence,
    StrictModel,
    load_episodes,
    validate_history,
)
from .qa_eval_data import file_hash
from .qa_storage import safe_name, transcript_event, write_json


class RecordLink(StrictModel):
    record_ids: list[str] = Field(min_length=2)
    reason: str = Field(min_length=1)
    evidence: list[HistoryEvidence] = Field(min_length=2)


class HistoryGroups(StrictModel):
    links: list[RecordLink]
    limitations: list[str]


def normalized(text):
    return re.sub(r"[\W_]+", "", str(text)).casefold()


def read(path):
    return json.loads(Path(path).read_text())


def name_for(source, explicit=None):
    return safe_name(explicit or Path(source).stem)


def write_rows(path, rows):
    from .qa_pipeline import save_export

    text = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    save_export(Path(path), text)


def validate_groups(value, rows):
    for link in value.links:
        ids = set(link.record_ids)
        if not ids <= rows.keys():
            raise ValueError("分组包含未知记录")
        validate_history(link.evidence, rows)
        cited = {e.record_id for e in link.evidence}
        if cited != ids:
            raise ValueError(
                "分组依据必须覆盖关联的每条记录；"
                f"缺少证据的记录：{sorted(ids - cited)}；"
                f"不属于此关联的引用：{sorted(cited - ids)}。"
                "为缺少的记录补充原文证据，或删除无法证明的关联。"
            )


def grouped_split(rows, links=(), seed=42):
    """Keep threads, duplicates and inferred feedback links together."""
    parents = {k: k for k in rows}

    def find(k):
        while parents[k] != k:
            parents[k] = parents[parents[k]]
            k = parents[k]
        return k

    def union(a, b):
        parents[find(a)] = find(b)

    identities, messages, questions = {}, {}, {}
    for key, row in rows.items():
        for field in ("session_id", "conversation_id", "thread_id"):
            if row.get(field):
                # Scope thread IDs to the source/channel where available.
                ident = (row.get("channel"), field, str(row[field]))
                if ident in identities:
                    union(key, identities[ident])
                identities[ident] = key
        if row.get("message_id"):
            mid = str(row["message_id"])
            if mid in messages:
                union(key, messages[mid])
            messages[mid] = key
        question = normalized(row["input"]["question"])
        if question:
            if question in questions:
                union(key, questions[question])
            questions[question] = key
    for key, row in rows.items():
        for field in ("reply_to", "reply_to_message_id", "parent_message_id"):
            value = row.get(field)
            if isinstance(value, (str, int)) and str(value) in messages:
                union(key, messages[str(value)])
        # A prior question copied into messages is also shared context.
        for message in row.get("input", {}).get("messages", []) or []:
            if isinstance(message, dict) and message.get("role") == "user":
                text = message.get("content", "")
                if isinstance(text, str) and normalized(text) in questions:
                    union(key, questions[normalized(text)])
    for link in links:
        for key in link.record_ids[1:]:
            union(link.record_ids[0], key)
    groups = {}
    for key in rows:
        groups.setdefault(find(key), []).append(key)
    groups = sorted(
        (sorted(g) for g in groups.values()), key=lambda g: digest([seed, g])
    )
    if len(groups) < 2:
        raise ValueError(
            "不足两个独立对话组，无法产生非空训练集和测试集；请补充历史"
        )
    # Approximate 20% by whole groups; use supplied labels to break ties.
    labels = {
        k: (str(r.get("topic") or "unknown"), str(r.get("kind") or "unknown"))
        for k, r in rows.items()
    }
    totals = Counter(labels.values())

    def imbalance(counts):
        return sum(abs(counts[t] - total * 0.2) for t, total in totals.items())

    choices, label_counts = {0: ()}, {0: Counter()}
    for i, group in enumerate(groups):
        addition = Counter(labels[k] for k in group)
        previous = [(n, ids, label_counts[n]) for n, ids in choices.items()]
        for count, selected, prior_counts in previous:
            n = count + len(group)
            candidate = prior_counts + addition
            if n < len(rows) and (
                n not in choices
                or imbalance(candidate) < imbalance(label_counts[n])
            ):
                choices[n] = (*selected, i)
                label_counts[n] = candidate
    count = min(
        (n for n in choices if n),
        key=lambda n: (
            abs(n - len(rows) * 0.2),
            imbalance(label_counts[n]),
            n,
        ),
    )
    test = {k for i in choices[count] for k in groups[i]}
    return (
        [k for k in rows if k not in test],
        [k for k in rows if k in test],
        groups,
    )


def check_split(folder):
    folder = Path(folder)
    manifest = read(folder / "split.json")
    for kind in ("original", "train", "test"):
        entry = manifest[kind]
        path = folder / entry["file"]
        if path.parent != folder or file_hash(path) != entry["sha256"]:
            raise ValueError(f"切分文件已改变：{path}；请使用新的批次名称")
    train, _ = load_episodes(folder / manifest["train"]["file"])
    test, _ = load_episodes(folder / manifest["test"]["file"])
    if (
        set(train) & set(test)
        or set(train) != set(manifest["train"]["record_ids"])
        or set(test) != set(manifest["test"]["record_ids"])
    ):
        raise ValueError("训练集/测试集归属不一致")
    return manifest


async def infer_groups(config, model, skill, task, rows, folder):
    from ..providers.retry_chat_model import (
        RetryChatModel,
        StreamIdleTimeoutError,
    )
    from .qa_benchmark import quiet_call

    if isinstance(model, RetryChatModel):
        model = model.with_minimum_stream_timeouts(120)
    for attempt in range(2):
        try:
            return await quiet_call(
                config,
                model,
                skill,
                task,
                HistoryGroups,
                [],
                folder if attempt == 0 else folder / "retry-1",
                lambda value: validate_groups(value, rows),
            )
        except TimeoutError as exc:
            # Partial output is never usable. Retry with a fresh agent only
            # for a stalled provider stream, not invalid evidence or the
            # stage's overall deadline. Keep both traces in the checkpoint.
            cause = exc.__cause__ or exc
            if attempt or not isinstance(cause, StreamIdleTimeoutError):
                raise
            transcript_event(
                "准备训练与测试数据", "模型输出中断，正在重试（1/1）。"
            )


async def prepare_split(
    root, source, config, model, store, name, skills_dir=None
):
    from . import qa_pipeline

    source = Path(source).resolve()
    folder = Path(root).parent / "analyze/history_jsonl" / name
    folder.mkdir(parents=True, exist_ok=True)
    original = folder / (name + ".jsonl")
    if source != original:
        from .qa_workflow import immutable_copy

        immutable_copy(source, original)
    if (folder / "split.json").exists():
        return folder, check_split(folder)
    rows, duplicates = load_episodes(original)
    if len(rows) < 2:
        raise ValueError("至少需要两条独立历史记录才能切分")
    # Grouping sees history only, without future references or TXT.
    skill = (
        Path(skills_dir or qa_pipeline.SKILLS) / "qa-history-split/SKILL.md"
    ).read_text()
    binding = {"source": file_hash(original), "skill": skill, "version": 2}
    previous = store.get("split_binding", binding)
    cached = store.get("groups")
    if previous != binding:
        # A failed grouping has no committed membership to preserve. Allow a
        # code/Skill repair on the same input; never reuse old inferred links.
        if previous.get("source") != binding["source"] or cached is not None:
            raise ValueError(
                "未完成切分的输入或 Skill 已改变，请使用新批次名称"
            )
    store.set("split_binding", binding)
    if cached is None:
        value = await infer_groups(
            config,
            model,
            skill,
            {
                "records": [
                    {
                        **{
                            field: value
                            for field, value in r.items()
                            if field not in {"trace", "config", "request_id"}
                        },
                        "record_id": k,
                    }
                    for k, r in rows.items()
                ],
                "instruction": (
                    "分组所需的原始问题、历史消息、回答、反馈和额外字段已直接提供，"
                    "保持原 JSON Pointer。只省略执行 trace、模型 config 和不能用于"
                    "会话识别的 request_id。不要逐条读取或核查回答正确性；"
                    "只输出有证据的跨记录关联，没有额外关联可直接返回空 links。"
                ),
            },
            rows,
            store.work / "grouping",
        )
        store.set("groups", value.model_dump())
    else:
        value = HistoryGroups.model_validate(cached)
        validate_groups(value, rows)
    train, test, groups = grouped_split(rows, value.links)
    manifest = {
        "schema_version": 1,
        "name": name,
        "seed": 42,
        "requested_test_ratio": 0.2,
        "stratification": "provided topic/kind when available",
        "actual_test_ratio": len(test) / len(rows),
        "groups": groups,
        "inferred_links": value.model_dump(),
        "duplicates": duplicates,
        "original": {"file": original.name, "sha256": file_hash(original)},
    }
    for kind, ids in (("train", train), ("test", test)):
        path = folder / f"{name}_{kind}.jsonl"
        # Explicit IDs preserve identity after splitting and reloading.
        write_rows(path, [dict(rows[k], record_id=k) for k in ids])
        manifest[kind] = {
            "file": path.name,
            "sha256": file_hash(path),
            "record_ids": ids,
        }
    write_json(folder / "split.json", manifest)
    return folder, manifest
