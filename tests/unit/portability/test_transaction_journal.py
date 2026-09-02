# -*- coding: utf-8 -*-
from __future__ import annotations

from pathlib import Path

import pytest

from qwenpaw.portability.transaction_journal import (
    ImportTransactionJournal,
    recover_import_transactions,
)
from qwenpaw.utils.io_utils import read_json_async, write_json_atomic_async


async def _plan(workspace: Path, plan_id: str, state: str) -> Path:
    path = workspace / ".qwenpaw/imports/plans" / f"{plan_id}.json"
    await write_json_atomic_async(path, {"state": state, "plan_id": plan_id})
    return path


@pytest.mark.asyncio
async def test_recovery_restores_uncommitted_workspace_files(tmp_path: Path):
    plan_id = "plan-" + "a" * 32
    target = tmp_path / "skills/example.txt"
    target.parent.mkdir(parents=True)
    target.write_text("before")
    journal = ImportTransactionJournal(tmp_path, plan_id)
    await journal.begin()
    await journal.watch(target)
    plan_path = await _plan(tmp_path, plan_id, "applying")
    target.write_text("after")

    assert await recover_import_transactions([tmp_path]) == [plan_id]
    assert target.read_text() == "before"
    assert (await read_json_async(plan_path))["state"] == "ready"
    assert not journal.path.exists()


@pytest.mark.asyncio
async def test_recovery_keeps_a_committed_transaction(tmp_path: Path):
    plan_id = "plan-" + "b" * 32
    target = tmp_path / "skills/example.txt"
    target.parent.mkdir(parents=True)
    target.write_text("before")
    journal = ImportTransactionJournal(tmp_path, plan_id)
    await journal.begin()
    await journal.watch(target)
    await _plan(tmp_path, plan_id, "applied")
    target.write_text("after")

    assert await recover_import_transactions([tmp_path]) == []
    assert target.read_text() == "after"
    assert not journal.path.exists()
