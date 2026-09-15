"""User skips only insufficient evidence, with preserved batch provenance."""

import json

import pytest

from qwenpaw.selflearn.qa_skip import skip_topics
from qwenpaw.selflearn.qa_storage import (
    ArtifactStore,
    write_json,
    workspace_lock,
)
from qwenpaw.selflearn.qa_rounds import checked_round
from qwenpaw.selflearn.analyzer import digest
from qwenpaw.selflearn.qa_command import options
from tests.unit.runtime.test_selflearn_dual import (
    ready_round as _ready_round,
    setup as _setup,
    source_store as _source_store,
    inputs as _inputs,
)

ready_round = _ready_round
setup = _setup
source_store = _source_store
inputs = _inputs


async def pending(ready_round):
    root, _, _, txt, _, _, _ = ready_round
    path, record, _, _ = checked_round(root, txt)
    db = (
        root.parent / "analyze/.state" / (path.parent.name + "-dataset.sqlite")
    )
    transcript = root.parent / "analyze/sessions" / (path.parent.name + ".md")
    store = ArtifactStore(db, transcript)
    with store.working():
        outcomes = store.get("compact_outcomes")
        value = json.loads(json.dumps(outcomes[0]))
        value["topic"]["question"] = "缺少证据的问题"
        value["draft"]["status"] = "needs_evidence"
        value["draft"]["limitations"] = ["未查明依据"]
        outcomes.append(value)
        store.set("compact_outcomes", outcomes)
    record["ready_for_evaluation"] = False
    record["training_state"] = "completed_with_errors"
    record["branches"]["txt"] = "completed_with_errors"
    write_json(path, record)
    return root, txt, path, db, transcript, digest(value["topic"])


async def test_preview_explicit_skip_and_idempotence(ready_round):
    root, txt, path, db, transcript, topic = await pending(ready_round)
    before = {p: p.read_bytes() for p in (txt, path, db, transcript)}
    assert topic in skip_topics(root, txt)
    assert all(p.read_bytes() == b for p, b in before.items())
    with pytest.raises(ValueError, match="尚未完成"):
        checked_round(root, txt)
    with pytest.raises(ValueError, match="主题 ID"):
        skip_topics(root, txt, topic=["unknown"])
    result = skip_topics(root, txt, topic=[topic])
    assert "可以继续评测" in result
    _, record, _, _ = checked_round(root, txt)
    assert record["skipped_topics"][0]["decision"] == "user_skip"
    assert txt.read_bytes() == before[txt]
    assert "用户确认跳过" in transcript.read_text()
    assert "没有新的" in skip_topics(root, txt, topic=[topic])


async def test_error_not_skippable_and_lock_and_tamper(ready_round):
    root, txt, path, db, transcript, _ = await pending(ready_round)
    store = ArtifactStore(db, transcript)
    with store.working():
        values = store.get("compact_outcomes")
        values.append({"topic": {"question": "超时"}, "error": "timeout"})
        store.set("compact_outcomes", values)
    with workspace_lock(root):
        with pytest.raises(ValueError, match="任务执行中"):
            skip_topics(root, txt, skip_all=True)
    assert "仍有未完成项" in skip_topics(root, txt, skip_all=True)
    assert not json.loads(path.read_text())["ready_for_evaluation"]
    txt.write_text("changed")
    with pytest.raises(ValueError, match="TXT 已修改"):
        skip_topics(root, txt, skip_all=True)


def test_command_parser(tmp_path):
    value = options(["qa-skip", "A.txt", "--all"], tmp_path)
    assert value["workflow"] == "qa-skip" and value["skip_all"]
    assert value["source"] == tmp_path / "A.txt"
    with pytest.raises(ValueError):
        options(["qa-skip", "A.txt", "--all", "--topic", "x"], tmp_path)


async def test_skip_does_not_finish_failed_benchmark(ready_round):
    root, txt, path, _, _, _ = await pending(ready_round)
    record = json.loads(path.read_text())
    record["branches"]["benchmark"] = "failed"
    record["errors"] = {"benchmark": "timeout"}
    write_json(path, record)
    assert "仍有未完成项" in skip_topics(root, txt, skip_all=True)
    record = json.loads(path.read_text())
    assert record["branches"]["txt"] == "completed"
    assert record["branches"]["benchmark"] == "failed"
    assert not record["ready_for_evaluation"]


async def test_accept_edit_preserves_versions_and_passes_gate(ready_round):
    import zlib

    root, txt, path, db, transcript, _ = await pending(ready_round)
    original = txt.read_bytes()
    edited = original.replace("参考：".encode(), "参考资料：".encode())
    txt.write_bytes(edited)
    before = path.read_bytes()
    with pytest.raises(ValueError, match="accept-edited-txt"):
        skip_topics(root, txt, skip_all=True)
    assert path.read_bytes() == before
    result = skip_topics(root, txt, skip_all=True, accept_edited_txt=True)
    assert "已登记当前修改版" in result
    assert txt.read_bytes() == edited
    _, record, _, _ = checked_round(root, txt)
    edit = record["txt_edits"][0]
    store = ArtifactStore(db, transcript)
    with store.working():
        for key, data in [
            ("before_snapshot", original),
            ("after_snapshot", edited),
        ]:
            blob = store.db.execute(
                "select data from files where path=?", (edit[key],)
            ).fetchone()[0]
            assert zlib.decompress(blob) == data
        assert store.get("compact_export")["digest"] == digest(edited.decode())
    assert "没有新的" in skip_topics(
        root, txt, skip_all=True, accept_edited_txt=True
    )
    assert len(json.loads(path.read_text())["txt_edits"]) == 1


async def test_accept_flag_alone_is_preview_and_invalid_edit_rejected(
    ready_round,
):
    root, txt, path, _, _, _ = await pending(ready_round)
    txt.write_text(txt.read_text() + "\n")
    before = path.read_bytes()
    skip_topics(root, txt, accept_edited_txt=True)
    assert path.read_bytes() == before
    txt.write_text("invalid")
    with pytest.raises(ValueError, match="记录头"):
        skip_topics(root, txt, skip_all=True, accept_edited_txt=True)
    assert path.read_bytes() == before
