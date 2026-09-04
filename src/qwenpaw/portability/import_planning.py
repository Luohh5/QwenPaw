# -*- coding: utf-8 -*-
"""Discovery, dry-run, and lifecycle orchestration for provider imports."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from ..utils import io_utils
from ..utils.io_utils import get_path_lock, read_json_async
from .import_support import (
    MemoryPayloads,
    _PLAN_ID_PATTERN,
    _prepare_memory_payloads,
)
from .models import (
    ImportReceipt,
    ImportSelection,
    MigrationPlan,
    ProviderInventory,
)
from .planner import build_migration_plan, tool_asset_fingerprints
from .providers.base import ProgressReporter, report_progress as _report
from .selection import select_inventory
from .transaction_journal import ImportTransactionJournal

logger = logging.getLogger(__name__)

_MAX_SESSIONS = 500


def _imports_root(workspace: Any) -> Path:
    return Path(workspace.workspace_dir) / ".qwenpaw" / "imports"


def _source_home(value: str) -> Path | None:
    return Path(value) if value else None


def _expected_fingerprints(
    fingerprints: dict[str, str],
    current: dict[str, str],
) -> dict[str, str | None]:
    return {key: fingerprints.get(key) for key in current}


class ImportPlanningMixin:
    """Discover sources and manage replay-safe migration plans."""

    async def _execute_plan(
        self,
        plan: MigrationPlan,
        inventory: ProviderInventory,
        *,
        started_at: datetime,
        progress: ProgressReporter | None,
        retry_of_migration_id: str = "",
        memory_payloads: MemoryPayloads | None = None,
    ) -> ImportReceipt:
        """Mark plan lifecycle around independent asset writes."""
        transaction = ImportTransactionJournal(
            Path(self._workspace.workspace_dir),
            plan.plan_id,
        )
        try:
            await transaction.begin()
            plan.state = "applying"
            await self._write_plan(plan)
            receipt = await self._apply(
                inventory,
                started_at=started_at,
                plan_id=plan.plan_id,
                progress=progress,
                retry_of_migration_id=retry_of_migration_id,
                memory_payloads=memory_payloads or {},
            )
        except BaseException:
            plan.state = "ready"
            try:
                await self._write_plan(plan)
                await transaction.discard()
            except Exception:  # pylint: disable=broad-except
                logger.exception("Failed to restore migration plan state")
            raise
        plan.state = "applied"
        plan.migration_id = receipt.migration_id
        try:
            await self._write_plan(plan)
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception("Failed to finalize migration plan state")
            receipt.warnings.append(
                "迁移已成功，但计划状态回写失败：" f"{type(exc).__name__}: {exc}",
            )
        else:
            try:
                await transaction.discard()
            except Exception:  # pylint: disable=broad-except
                logger.exception("Failed to clean completed import journal")
        return receipt

    async def plan_from(
        self,
        source: str,
        *,
        source_home: Path | None = None,
        progress: ProgressReporter | None = None,
    ) -> MigrationPlan:
        """Create and persist a dry-run migration plan without importing."""
        lock_path = _imports_root(self._workspace)
        await _report(progress, "正在生成迁移预演，不会修改现有数据…")
        async with get_path_lock(lock_path):
            inventory = await self._inventory(
                source,
                source_home=source_home,
                progress=progress,
            )
            plan = await build_migration_plan(
                self._workspace,
                inventory,
                source_home=str(source_home or ""),
            )
            await self._write_plan(plan)
        await _report(progress, "迁移预演已生成；尚未导入任何内容。")
        return plan

    async def apply_selection(
        self,
        plan_id: str,
        selection: ImportSelection,
        *,
        progress: ProgressReporter | None = None,
    ) -> ImportReceipt:
        """Revalidate a plan, then apply a dependency-complete subset."""
        return await self._apply_stored_plan(
            plan_id,
            progress=progress,
            selection=selection,
        )

    async def retry_selection(
        self,
        parent_plan_id: str,
        selection: ImportSelection,
        *,
        progress: ProgressReporter | None = None,
    ) -> tuple[MigrationPlan, ImportReceipt]:
        """Retry failed tools without replacing any existing QwenPaw asset."""
        if selection.sessions or not any(
            getattr(selection, field)
            for field in ("memory", "cron", "skills", "mcp", "plugins")
        ):
            raise ValueError(
                "retry requires at least one tool and no sessions",
            )
        if not _PLAN_ID_PATTERN.fullmatch(parent_plan_id):
            raise ValueError("迁移计划编号格式无效。")
        lock_path = _imports_root(self._workspace)
        async with get_path_lock(lock_path):
            parent = await self._read_plan(parent_plan_id)
            if parent.agent_id != self._workspace.agent_id:
                raise ValueError(
                    "该迁移计划属于另一个智能体，不能在这里执行。",
                )
            if parent.state != "applied":
                raise ValueError("只能重试已完成迁移中的失败工具。")
            source_home = _source_home(parent.source_home)
            await _report(progress, "正在重新读取来源并准备失败工具重试…")
            inventory = await self._inventory(
                parent.source,
                source_home=source_home,
                progress=progress,
                include_sessions=False,
            )
            plan = await build_migration_plan(
                self._workspace,
                inventory,
                source_home=str(source_home or ""),
            )
            await self._write_plan(plan)
            inventory = select_inventory(inventory, selection)
            memory_payloads, file_payloads = await io_utils.run_sync_io(
                _prepare_memory_payloads,
                inventory.provider_id,
                inventory.memory_projects,
            )
            current = await io_utils.run_sync_io(
                tool_asset_fingerprints,
                inventory,
                file_payloads=file_payloads,
            )
            expected = _expected_fingerprints(plan.asset_fingerprints, current)
            if current != expected:
                raise ValueError(
                    "来源数据在预演后发生了变化。请重新扫描后重试。",
                )
            receipt = await self._execute_plan(
                plan,
                inventory,
                started_at=datetime.now(timezone.utc),
                progress=progress,
                retry_of_migration_id=parent.migration_id,
                memory_payloads=memory_payloads,
            )
        return plan, receipt

    async def _apply_stored_plan(
        self,
        plan_id: str,
        *,
        progress: ProgressReporter | None,
        selection: ImportSelection | None = None,
    ) -> ImportReceipt:
        if not _PLAN_ID_PATTERN.fullmatch(plan_id):
            raise ValueError("迁移计划编号格式无效。")
        lock_path = _imports_root(self._workspace)
        await _report(progress, "正在重新核对迁移计划和来源数据…")
        async with get_path_lock(lock_path):
            plan = await self._read_plan(plan_id)
            if plan.agent_id != self._workspace.agent_id:
                raise ValueError(
                    "该迁移计划属于另一个智能体，不能在这里执行。",
                )
            if plan.state != "ready":
                raise ValueError(
                    f"该迁移计划当前状态为 {plan.state!r}，不能重复执行。",
                )
            source_home = _source_home(plan.source_home)
            include_sessions = selection is None or selection.sessions
            session_ids = (
                {
                    action.source_id
                    for action in plan.actions
                    if action.asset_type == "session"
                }
                if include_sessions
                else None
            )
            inventory = await self._inventory(
                plan.source,
                source_home=source_home,
                progress=progress,
                include_sessions=include_sessions,
                session_ids=session_ids,
            )
            if selection is not None:
                inventory = select_inventory(inventory, selection)
            memory_payloads, file_payloads = await io_utils.run_sync_io(
                _prepare_memory_payloads,
                inventory.provider_id,
                inventory.memory_projects,
            )
            current = await io_utils.run_sync_io(
                tool_asset_fingerprints,
                inventory,
                file_payloads=file_payloads,
            )
            expected = _expected_fingerprints(
                plan.asset_fingerprints,
                current,
            )
            if current != expected:
                message = "来源数据在预演后发生了变化。请重新扫描，"
                message += "确认新计划后再执行。"
                raise ValueError(message)
            return await self._execute_plan(
                plan,
                inventory,
                started_at=datetime.now(timezone.utc),
                progress=progress,
                memory_payloads=memory_payloads,
            )

    async def _inventory(
        self,
        source: str,
        *,
        source_home: Path | None,
        progress: ProgressReporter | None,
        require_detected: bool = True,
        include_sessions: bool = True,
        session_ids: set[str] | None = None,
    ) -> ProviderInventory:
        await _report(progress, f"正在检测 {source} 并读取可迁移内容…")
        # Keep compatibility with tests and integrations that patch the old
        # two-argument factory while still supporting explicit source roots.
        if source_home is None:
            provider = self._create_provider(source)
        else:
            provider = self._create_provider(
                source,
                source_home=source_home,
            )
        inventory = await provider.inventory(
            limit=_MAX_SESSIONS,
            progress=progress,
            include_sessions=include_sessions,
            session_ids=session_ids,
        )
        if require_detected and not inventory.detected:
            detail = "; ".join(inventory.warnings) or "未检测到来源数据"
            raise ValueError(
                f"未找到 {inventory.provider_name} 的可迁移数据 "
                f"(source not found)：{detail}",
            )
        return inventory

    def _plan_path(self, plan_id: str) -> Path:
        return (
            Path(self._workspace.workspace_dir)
            / ".qwenpaw"
            / "imports"
            / "plans"
            / f"{plan_id}.json"
        )

    async def _write_plan(self, plan: MigrationPlan) -> None:
        path = self._plan_path(plan.plan_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        await io_utils.write_json_atomic_async(
            path,
            plan.model_dump(mode="json"),
            sort_keys=True,
            new_file_mode=0o600,
        )

    async def _read_plan(self, plan_id: str) -> MigrationPlan:
        path = self._plan_path(plan_id)
        try:
            value = await read_json_async(path)
            return MigrationPlan.model_validate(value)
        except FileNotFoundError as exc:
            raise ValueError(f"找不到迁移计划：{plan_id}") from exc
        except (OSError, ValueError, TypeError) as exc:
            raise ValueError(f"迁移计划已损坏或无法读取：{plan_id}") from exc
