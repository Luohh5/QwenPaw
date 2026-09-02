# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from qwenpaw.portability.compatibility import AssetZone, CompatibilityStore
from qwenpaw.portability.doctor import run_migration_doctor
from qwenpaw.portability.models import (
    ImportReceipt,
    ProviderInventory,
    SourceScheduledTask,
)
from qwenpaw.portability.scheduled_tasks import build_imported_job
from qwenpaw.utils.io_utils import write_json_atomic


class _CronManager:
    def __init__(self, jobs) -> None:
        self.jobs = list(jobs)

    async def list_jobs(self):
        return list(self.jobs)


def _task(tmp_path: Path) -> SourceScheduledTask:
    return SourceScheduledTask(
        source_id="remote-task",
        name="Remote task",
        schedule_type="cron",
        cron="0 9 * * *",
        prompt="Review the remote workspace",
        cwd=str(tmp_path),
        metadata={
            "source_target_remote_authority": "ssh-remote+example",
            "workspace_status": "remote_unverified",
        },
    )


def _receipt(
    workspace_dir: Path,
    task: SourceScheduledTask,
    *,
    discovered: int = 1,
    zone: AssetZone | None = AssetZone.REPAIR,
) -> ImportReceipt:
    manifest_path = workspace_dir / ".qwenpaw" / "compatibility.json"
    store = CompatibilityStore(manifest_path)
    store.prepare(
        migration_id="migration-1",
        source="qoder",
        scheduled_tasks=[task],
    )
    if zone is not None:
        if zone is AssetZone.MIGRATE:
            asset_key = f"scheduled_tasks:{task.source_id}"
            store.finalize(
                asset_key,
                passed=True,
                summary="fixture",
                reason="fixture",
            )
        store.finish()
    now = datetime.now(timezone.utc)
    receipt = ImportReceipt(
        migration_id="migration-1",
        source="qoder",
        agent_id="agent-1",
        started_at=now,
        completed_at=now,
        imported_scheduled_tasks=[task.source_id],
        discovered_scheduled_task_count=discovered,
        adaptation_manifest=str(manifest_path),
    )
    receipt_path = (
        workspace_dir / ".qwenpaw" / "imports" / f"{receipt.migration_id}.json"
    )
    write_json_atomic(receipt_path, receipt.model_dump(mode="json"))
    return receipt


@pytest.fixture(name="doctor_case")
def _doctor_case(tmp_path: Path):
    def make(*, discovered: int = 1, zone=AssetZone.REPAIR):
        task = _task(tmp_path)
        job = build_imported_job("qoder", task)
        workspace_dir = tmp_path / "workspace"
        workspace_dir.mkdir()
        return SimpleNamespace(
            job=job,
            workspace=SimpleNamespace(
                workspace_dir=workspace_dir,
                cron_manager=_CronManager([job]),
            ),
            inventory=ProviderInventory(
                provider_id="qoder",
                provider_name="Qoder",
                detected=True,
                scheduled_tasks=[task],
                discovered_scheduled_task_count=discovered,
            ),
            receipt=_receipt(
                workspace_dir,
                task,
                discovered=discovered,
                zone=zone,
            ),
        )

    return make


def _schedule_check(report):
    return next(
        item for item in report.checks if item.category == "scheduled_tasks"
    )


@pytest.mark.asyncio
async def test_doctor_reconciles_ready_remote_job_and_review_gate(
    doctor_case,
) -> None:
    case = doctor_case()
    report = await run_migration_doctor(
        case.workspace,
        case.inventory,
        case.receipt,
    )

    check = _schedule_check(report)
    assert check.status == "pass"
    assert "兼容清单确认已分类 1/1" in check.detail_zh
    assert "远程或未验证工作区未绑定本机目录 1/1" in check.detail_zh
    assert "来源发现 1 个、规范化 1 个、回执导入 1 个" in check.detail_zh


@pytest.mark.asyncio
async def test_doctor_fails_if_remote_job_was_bound_to_local_project_dir(
    doctor_case,
    tmp_path: Path,
) -> None:
    case = doctor_case()
    assert case.job.request is not None
    request = case.job.request.model_copy(
        update={
            "request_context": {
                "source": "cron",
                "portability_review_required": True,
                "project_dir": str(tmp_path),
            },
        },
    )
    case.workspace.cron_manager.jobs[0] = case.job.model_copy(
        update={"request": request},
    )
    report = await run_migration_doctor(
        case.workspace,
        case.inventory,
        case.receipt,
    )

    check = _schedule_check(report)
    assert check.status == "fail"
    assert "远程或未验证工作区未绑定本机目录 0/1" in check.detail_zh


@pytest.mark.asyncio
async def test_doctor_warns_when_source_reader_rejected_raw_definitions(
    doctor_case,
) -> None:
    case = doctor_case(discovered=2)
    report = await run_migration_doctor(
        case.workspace,
        case.inventory,
        case.receipt,
    )

    check = _schedule_check(report)
    assert check.status == "warning"
    assert "另有 1 个来源记录未进入可迁移定义" in check.detail_zh
    assert "已结束、已取消、损坏或规则无法等价转换" in check.detail_zh


@pytest.mark.asyncio
async def test_doctor_fails_if_imported_job_loses_review_gate(
    doctor_case,
) -> None:
    case = doctor_case()
    case.job.meta["portability"]["requires_review"] = False
    report = await run_migration_doctor(
        case.workspace,
        case.inventory,
        case.receipt,
    )

    check = _schedule_check(report)
    assert check.status == "fail"
    assert "状态与兼容分区一致 0/1" in check.detail_zh


@pytest.mark.asyncio
async def test_doctor_accepts_reviewed_migrate_job_while_disabled(
    tmp_path: Path,
) -> None:
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    task = SourceScheduledTask(
        source_id="reviewed-task",
        name="Reviewed task",
        schedule_type="cron",
        cron="0 9 * * *",
        prompt="Review the local workspace",
        cwd=str(tmp_path),
    )
    job = build_imported_job("qoder", task, reviewed=True)
    receipt = _receipt(workspace_dir, task, zone=AssetZone.MIGRATE)
    workspace = SimpleNamespace(
        workspace_dir=workspace_dir,
        cron_manager=_CronManager([job]),
    )
    inventory = ProviderInventory(
        provider_id="qoder",
        provider_name="Qoder",
        detected=True,
        scheduled_tasks=[task],
    )

    report = await run_migration_doctor(workspace, inventory, receipt)

    check = _schedule_check(report)
    assert check.status == "pass"
    assert "状态与兼容分区一致 1/1" in check.detail_zh

    workspace.cron_manager.jobs[0] = job.model_copy(
        update={"enabled": True},
    )
    report = await run_migration_doctor(workspace, inventory, receipt)

    assert _schedule_check(report).status == "fail"
