# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from qwenpaw.runtime.commands.control.base import ControlContext
from qwenpaw.portability.models import (
    ImportReceipt,
    MigrationDoctorCheck,
    MigrationDoctorReport,
    MigrationPlan,
)
from qwenpaw.runtime.commands.control.portability_handler import (
    ImportCommandHandler,
)


def _context(raw: str) -> ControlContext:
    return ControlContext(
        workspace=SimpleNamespace(),
        payload=None,
        channel=None,
        session_id="session",
        user_id="user",
        agent_id="agent",
        args={"_raw_args": raw},
    )


def _remote_context(raw: str) -> ControlContext:
    context = _context(raw)
    context.payload = SimpleNamespace(channel="telegram")
    return context


@pytest.mark.asyncio
async def test_import_requires_from_syntax() -> None:
    text = await ImportCommandHandler().handle(_context("codex"))
    assert "Usage: `/import from <source>`" in text


@pytest.mark.asyncio
async def test_import_rejects_unexpected_positional_arguments() -> None:
    with pytest.raises(ValueError, match="Unexpected import argument"):
        await ImportCommandHandler().handle(_context("from codex extra"))


@pytest.mark.asyncio
async def test_import_inspect_shows_cross_user_source_resolution() -> None:
    text = await ImportCommandHandler().handle(_context("inspect"))

    assert "本机迁移来源定位" in text
    assert "**codex**" in text
    assert "**qoder**" in text
    assert "数据目录" in text
    assert "编辑器数据目录" in text


@pytest.mark.asyncio
async def test_import_dry_run_returns_reviewable_plan(
    monkeypatch,
) -> None:
    plan = MigrationPlan(
        plan_id="plan-" + "a" * 32,
        source="codex",
        source_home="/tmp/custom-codex",
        agent_id="agent",
        created_at=datetime.now(timezone.utc),
        inventory_fingerprint="fingerprint",
        inventory_counts={"sessions": 3, "manual_review": 1},
    )

    async def _plan_from(
        _self,
        source,
        *,
        source_home,
        progress,
    ):
        del progress
        assert source == "codex"
        assert str(source_home) == "/tmp/custom-codex"
        return plan

    monkeypatch.setattr(
        "qwenpaw.portability.ProviderImportService.plan_from",
        _plan_from,
    )

    text = await ImportCommandHandler().handle(
        _context("from codex --dry-run --source-home /tmp/custom-codex"),
    )

    assert "迁移预演完成（尚未导入）" in text
    assert plan.plan_id in text
    assert "会话：3" in text
    assert f"/import apply {plan.plan_id}" in text


@pytest.mark.asyncio
async def test_import_apply_displays_doctor_entirely_in_chinese(
    monkeypatch,
) -> None:
    now = datetime.now(timezone.utc)
    receipt = ImportReceipt(
        migration_id="migration-test",
        plan_id="plan-" + "b" * 32,
        source="codex",
        agent_id="agent",
        started_at=now,
        completed_at=now,
        doctor_report=MigrationDoctorReport(
            status="warning",
            summary_zh="迁移完成，核心数据可用，但仍有需要人工确认的项目。",
            checked_at=now,
            checks=[
                MigrationDoctorCheck(
                    category="mcp",
                    status="warning",
                    title_zh="MCP DriverCard",
                    detail_zh="配置已保存并保持禁用，启用前需要重新授权。",
                ),
            ],
        ),
    )

    async def _apply_plan(_self, plan_id, *, progress):
        del progress
        assert plan_id == receipt.plan_id
        return receipt

    monkeypatch.setattr(
        "qwenpaw.portability.ProviderImportService.apply_plan",
        _apply_plan,
    )

    text = await ImportCommandHandler().handle(
        _context(f"apply {receipt.plan_id}"),
    )

    assert "迁移后体检（中文）" in text
    assert receipt.doctor_report.summary_zh in text
    assert "MCP DriverCard" in text
    assert "配置已保存并保持禁用" in text


@pytest.mark.asyncio
async def test_import_rejects_remote_channels() -> None:
    with pytest.raises(PermissionError, match="local Console/ACP"):
        await ImportCommandHandler().handle(_remote_context("from codex"))
