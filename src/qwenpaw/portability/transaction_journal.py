# -*- coding: utf-8 -*-
"""Crash marker for import plans.

Stores write atomically, so recovery only resets an interrupted plan and never
restores a snapshot over data changed by another agent.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..utils.io_utils import (
    read_json_async,
    unlink_async,
    write_json_atomic_async,
)


def _journal_path(workspace: Path, plan_id: str) -> Path:
    return workspace / ".qwenpaw/imports/transactions" / f"{plan_id}.json"


class ImportTransactionJournal:
    """Durable in-flight marker; no whole-store snapshots are taken."""

    def __init__(self, workspace: Path, plan_id: str) -> None:
        if not re.fullmatch(r"plan-[0-9a-f]{32}", plan_id):
            raise ValueError("invalid import plan id")
        self.path = _journal_path(workspace.resolve(), plan_id)
        self.plan_id = plan_id

    async def begin(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        await write_json_atomic_async(
            self.path,
            {"plan_id": self.plan_id, "state": "applying"},
            sort_keys=True,
            new_file_mode=0o600,
        )

    async def discard(self) -> None:
        await unlink_async(self.path, missing_ok=True)


async def recover_import_transactions(workspaces: list[Path]) -> list[str]:
    """Reset interrupted plans without touching live asset stores."""
    recovered: list[str] = []
    for workspace in workspaces:
        root = workspace / ".qwenpaw/imports/transactions"
        try:
            journals = sorted(root.glob("*.json"))
        except OSError:
            continue
        for path in journals:
            if path.is_symlink():
                continue
            try:
                value = await read_json_async(path)
                plan_id = str(value["plan_id"])
                if not re.fullmatch(r"plan-[0-9a-f]{32}", plan_id):
                    raise ValueError("invalid import plan id")
                plan = workspace / ".qwenpaw/imports/plans" / f"{plan_id}.json"
                plan_value = await read_json_async(plan)
                if plan_value.get("state") != "applied":
                    plan_value["state"] = "ready"
                    await write_json_atomic_async(
                        plan,
                        plan_value,
                        sort_keys=True,
                        new_file_mode=0o600,
                    )
                    recovered.append(plan_id)
                await unlink_async(path, missing_ok=True)
            except Exception as exc:
                raise RuntimeError(
                    f"无法恢复未完成的导入事务 {path.name}: {exc}",
                ) from exc
    return recovered


__all__ = ["ImportTransactionJournal", "recover_import_transactions"]
