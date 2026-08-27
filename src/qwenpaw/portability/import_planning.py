# -*- coding: utf-8 -*-
"""Discovery, dry-run, and lifecycle orchestration for provider imports."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from ..utils import io_utils
from ..utils.io_utils import get_path_lock, read_json_async
from .import_support import _PLAN_ID_PATTERN
from .models import (
    ImportReceipt,
    ImportSelection,
    MigrationPlan,
    ProviderInventory,
)
from .planner import build_migration_plan, inventory_fingerprint
from .providers.base import ProgressReporter, report_progress as _report
from .selection import select_inventory

logger = logging.getLogger(__name__)

_MAX_SESSIONS = 500


class ImportPlanningMixin:
    """Discover sources and manage replay-safe migration plans."""

    async def import_from(
        self,
        source: str,
        *,
        source_home: Path | None = None,
        progress: ProgressReporter | None = None,
    ) -> ImportReceipt:
        """Inventory, persist a plan, and commit one provider migration."""
        started_at = datetime.now(timezone.utc)
        lock_path = (
            Path(self._workspace.workspace_dir) / ".qwenpaw" / "imports"
        )
        await _report(progress, "正在等待迁移锁，避免重复导入…")
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
            await _report(
                progress,
                "读取完成："
                f"{len(inventory.sessions)} 个会话、"
                f"{len(inventory.skills)} 个 Skill、"
                f"{len(inventory.mcp_servers)} 个 MCP、"
                f"{len(inventory.memory_projects)} 组 Memory、"
                f"{len(inventory.plugins)} 个插件、"
                f"{len(inventory.scheduled_tasks)} 个定时任务；"
                "正在写入 QwenPaw…",
            )
            return await self._execute_plan(
                plan,
                inventory,
                started_at=started_at,
                progress=progress,
            )

    async def _execute_plan(
        self,
        plan: MigrationPlan,
        inventory: ProviderInventory,
        *,
        started_at: datetime,
        progress: ProgressReporter | None,
    ) -> ImportReceipt:
        """Mark plan lifecycle around the rollback-capable data write."""
        plan.state = "applying"
        await self._write_plan(plan)
        try:
            receipt = await self._apply(
                inventory,
                started_at=started_at,
                plan_id=plan.plan_id,
                progress=progress,
            )
        except BaseException:
            plan.state = "ready"
            try:
                await self._write_plan(plan)
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
        return receipt

    async def inspect(
        self,
        source: str,
        *,
        source_home: Path | None = None,
        progress: ProgressReporter | None = None,
    ) -> ProviderInventory:
        """Read and normalize a source without changing QwenPaw state."""
        return await self._inventory(
            source,
            source_home=source_home,
            progress=progress,
            require_detected=False,
        )

    async def plan_from(
        self,
        source: str,
        *,
        source_home: Path | None = None,
        progress: ProgressReporter | None = None,
    ) -> MigrationPlan:
        """Create and persist a dry-run migration plan without importing."""
        lock_path = (
            Path(self._workspace.workspace_dir) / ".qwenpaw" / "imports"
        )
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

    async def apply_plan(
        self,
        plan_id: str,
        *,
        progress: ProgressReporter | None = None,
    ) -> ImportReceipt:
        """Revalidate a persisted plan and apply the unchanged source."""
        return await self._apply_stored_plan(plan_id, progress=progress)

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

    async def _apply_stored_plan(
        self,
        plan_id: str,
        *,
        progress: ProgressReporter | None,
        selection: ImportSelection | None = None,
    ) -> ImportReceipt:
        if not _PLAN_ID_PATTERN.fullmatch(plan_id):
            raise ValueError("迁移计划编号格式无效。")
        lock_path = (
            Path(self._workspace.workspace_dir) / ".qwenpaw" / "imports"
        )
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
            source_home = Path(plan.source_home) if plan.source_home else None
            inventory = await self._inventory(
                plan.source,
                source_home=source_home,
                progress=progress,
            )
            if inventory_fingerprint(inventory) != plan.inventory_fingerprint:
                message = "来源数据在预演后发生了变化。请重新运行 --dry-run，"
                message += "确认新计划后再执行。"
                raise ValueError(message)
            if selection is not None:
                inventory = select_inventory(inventory, selection)
            return await self._execute_plan(
                plan,
                inventory,
                started_at=datetime.now(timezone.utc),
                progress=progress,
            )

    async def _inventory(
        self,
        source: str,
        *,
        source_home: Path | None,
        progress: ProgressReporter | None,
        require_detected: bool = True,
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
