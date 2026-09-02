# -*- coding: utf-8 -*-
"""Crash recovery for one PawPort import transaction."""

from __future__ import annotations

import shutil
from pathlib import Path

from ..config.utils import get_plugins_dir
from ..utils.io_utils import (
    read_json_async,
    run_sync_io,
    unlink_async,
    write_json_atomic_async,
)


def _paths(workspace: Path, plan_id: str) -> tuple[Path, Path]:
    root = workspace / ".qwenpaw/imports/transactions"
    return root / f"{plan_id}.json", root / f"{plan_id}.rollback"


def _managed_target(workspace: Path, target: Path) -> bool:
    """Only recover paths owned by this workspace or QwenPaw plugins."""
    for root in (workspace, get_plugins_dir().resolve()):
        try:
            target.relative_to(root)
            return True
        except ValueError:
            pass
    return False


def _snapshot(source: Path, target: Path) -> str:
    if not source.exists() and not source.is_symlink():
        return "missing"
    if source.is_dir() and not source.is_symlink():
        shutil.copytree(source, target, symlinks=True)
        return "dir"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target, follow_symlinks=False)
    return "file"


def _restore(target: Path, backup: Path, kind: str) -> None:
    if target.is_dir() and not target.is_symlink():
        shutil.rmtree(target)
    elif target.exists() or target.is_symlink():
        target.unlink()
    if kind == "dir":
        shutil.copytree(backup, target, symlinks=True)
    elif kind == "file":
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(backup, target, follow_symlinks=False)


class ImportTransactionJournal:
    """Durable pre-mutation snapshots for a single import Plan."""

    def __init__(self, workspace: Path, plan_id: str) -> None:
        self.workspace = workspace.resolve()
        self.plan_id = plan_id
        self.path, self.backup_root = _paths(self.workspace, plan_id)
        self.entries: list[dict[str, str]] = []

    async def begin(self) -> None:
        await self._save()

    async def watch(self, target: Path) -> None:
        target = target.resolve(strict=False)
        if not _managed_target(self.workspace, target):
            return
        if any(item["target"] == str(target) for item in self.entries):
            return
        backup = self.backup_root / str(len(self.entries))
        kind = await run_sync_io(_snapshot, target, backup)
        self.entries.append(
            {"target": str(target), "backup": str(backup), "kind": kind},
        )
        await self._save()

    async def discard(self) -> None:
        await unlink_async(self.path, missing_ok=True)
        if self.backup_root.exists():
            await run_sync_io(shutil.rmtree, self.backup_root)

    async def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        await write_json_atomic_async(
            self.path,
            {"plan_id": self.plan_id, "entries": self.entries},
            sort_keys=True,
            new_file_mode=0o600,
        )


async def recover_import_transactions(workspaces: list[Path]) -> list[str]:
    """Restore every uncommitted transaction before plugins are loaded."""
    recovered: list[str] = []
    for workspace in workspaces:
        root = workspace / ".qwenpaw/imports/transactions"
        try:
            journals = sorted(root.glob("*.json"))
        except OSError:
            continue
        for path in journals:
            try:
                value = await read_json_async(path)
                plan_id = str(value["plan_id"])
                entries = value["entries"]
                if not isinstance(entries, list):
                    raise ValueError("entries must be a list")
                plan = workspace / ".qwenpaw/imports/plans" / f"{plan_id}.json"
                plan_value = await read_json_async(plan)
                journal = ImportTransactionJournal(workspace, plan_id)
                journal.entries = [dict(item) for item in entries]
                for item in journal.entries:
                    target = Path(item["target"]).resolve(strict=False)
                    backup = Path(item["backup"]).resolve(strict=False)
                    if (
                        item.get("kind") not in {"missing", "file", "dir"}
                        or not _managed_target(journal.workspace, target)
                        or backup.parent != journal.backup_root.resolve()
                    ):
                        raise ValueError("unsafe transaction entry")
                if plan_value.get("state") == "applied":
                    await journal.discard()
                    continue
                for item in reversed(journal.entries):
                    await run_sync_io(
                        _restore,
                        Path(item["target"]),
                        Path(item["backup"]),
                        item["kind"],
                    )
                plan_value["state"] = "ready"
                await write_json_atomic_async(
                    plan,
                    plan_value,
                    sort_keys=True,
                    new_file_mode=0o600,
                )
                await journal.discard()
                recovered.append(plan_id)
            except Exception as exc:
                raise RuntimeError(
                    f"无法恢复未完成的导入事务 {path.name}: {exc}",
                ) from exc
    return recovered


__all__ = ["ImportTransactionJournal", "recover_import_transactions"]
