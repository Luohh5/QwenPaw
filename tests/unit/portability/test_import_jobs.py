# -*- coding: utf-8 -*-
# pylint: disable=protected-access
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from qwenpaw.portability.import_jobs import (
    ImportProviderSnapshot,
    PortabilityImportJobManager,
)
from qwenpaw.portability.importer import ImportRollbackError
from qwenpaw.portability.models import (
    ImportAssetResult,
    ImportAssetState,
    ImportReceipt,
    ImportSelection,
    MigrationAssetPlan,
    MigrationPlan,
)


def _workspace(tmp_path: Path, agent_id: str = "agent-1"):
    return SimpleNamespace(
        workspace_dir=tmp_path / agent_id,
        agent_id=agent_id,
    )


class _FakeServices:
    def __init__(self) -> None:
        self.active_scans = 0
        self.max_active_scans = 0
        self.block_apply = asyncio.Event()
        self.block_apply.set()
        self.apply_started = asyncio.Event()
        self.fail_apply: set[str] = set()
        self.fail_rollback = False
        self.retries: list[tuple[str, ImportSelection]] = []

    def factory(self, workspace):
        owner = self

        class Service:
            async def plan_from(self, source, *, progress=None, **_kwargs):
                owner.active_scans += 1
                owner.max_active_scans = max(
                    owner.max_active_scans,
                    owner.active_scans,
                )
                if progress:
                    await progress(f"正在检测 {source}")
                await asyncio.sleep(0.01)
                owner.active_scans -= 1
                return MigrationPlan(
                    plan_id=f"plan-{source[0] * 32}",
                    source=source,
                    agent_id=workspace.agent_id,
                    created_at=datetime.now(timezone.utc),
                    inventory_fingerprint=source,
                    inventory_counts={"sessions": 2},
                    actions=[
                        MigrationAssetPlan(
                            asset_type="session",
                            source_id=f"{source}-thread",
                            name="Conversation",
                            action="import_history",
                            fidelity="converted_with_loss",
                        ),
                        MigrationAssetPlan(
                            asset_type="skill",
                            source_id=f"{source}-skill",
                            name=f"{source.title()} Skill",
                            action="agent_mission_test_and_adapt",
                            fidelity="mission_repair",
                        ),
                    ],
                )

            async def apply_selection(
                self,
                _plan_id,
                selection,
                *,
                progress=None,
            ):
                source = "codex" if _plan_id.endswith("c" * 32) else "qoder"
                assert isinstance(selection, ImportSelection)
                owner.apply_started.set()
                try:
                    await owner.block_apply.wait()
                except asyncio.CancelledError as exc:
                    if owner.fail_rollback:
                        raise ImportRollbackError(
                            [
                                "Skill codex-skill: OSError: "
                                "cleanup unavailable",
                            ],
                            cancelled=True,
                        ) from exc
                    raise
                if progress:
                    await progress("正在写入会话：1/2（聊天记录阶段）")
                    await progress(f"正在修复 Skill「{source.title()} Skill」")
                    await progress("api_key=sk-test-secret-1234567890")
                if source in owner.fail_apply:
                    raise RuntimeError(f"{source} failed")
                now = datetime.now(timezone.utc)
                return ImportReceipt(
                    migration_id=f"migration-{source}",
                    plan_id=_plan_id,
                    source=source,
                    agent_id=workspace.agent_id,
                    started_at=now,
                    completed_at=now,
                    imported_sessions=[f"{source}-thread"],
                    imported_skills=[f"{source.title()} Skill"],
                )

            async def retry_selection(
                self,
                plan_id,
                selection,
                *,
                progress=None,
            ):
                owner.retries.append((plan_id, selection))
                source = "codex" if plan_id.endswith("c" * 32) else "qoder"
                if progress:
                    await progress(f"正在修复 Skill「{source.title()} Skill」")
                now = datetime.now(timezone.utc)
                return (
                    await self.plan_from(source),
                    ImportReceipt(
                        migration_id=f"migration-retry-{source}",
                        plan_id=f"plan-{source[0] * 32}",
                        source=source,
                        agent_id=workspace.agent_id,
                        started_at=now,
                        completed_at=now,
                        imported_skills=[f"{source.title()} Skill"],
                        retry_of_migration_id=f"migration-{source}",
                    ),
                )

        return Service()


def test_only_materialization_milestone_updates_session_progress() -> None:
    provider = ImportProviderSnapshot(
        source="qoder",
        sessions_total=2,
        assets=[
            ImportAssetResult(
                asset_type="skill",
                source_id="qoder-skill",
                name="Qoder Skill",
            ),
        ],
    )

    PortabilityImportJobManager._project_progress(
        provider,
        "正在扫描 Qoder 会话：2/2",
    )
    assert (provider.sessions_processed, provider.sessions_total) == (0, 2)

    PortabilityImportJobManager._project_progress(
        provider,
        "\x1esessions\t1\t2\t1\t0",
    )
    PortabilityImportJobManager._project_progress(
        provider,
        "\x1easset\tskill\tsucceeded\t0\tqoder-skill",
    )
    PortabilityImportJobManager._project_progress(
        provider,
        "正在修复 Skill「Qoder Skill」",
    )
    assert (
        provider.sessions_processed,
        provider.sessions_total,
        provider.sessions_imported,
        provider.sessions_skipped,
    ) == (1, 2, 1, 0)
    assert provider.assets[0].state is ImportAssetState.SUCCEEDED
    assert provider.assets[0].enabled is False
    provider.assets[0].state = ImportAssetState.REPAIRING
    PortabilityImportJobManager._project_progress(
        provider,
        "Skill「Qoder Skill」兼容性优化完成，已进入待迁移区。",
    )
    assert provider.assets[0].reason_code == "ready_to_import"


@pytest.mark.asyncio
async def test_scan_is_concurrent_and_persisted(tmp_path: Path) -> None:
    services = _FakeServices()
    workspace = _workspace(tmp_path)
    manager = PortabilityImportJobManager(service_factory=services.factory)

    created = await manager.create(workspace, ["codex", "qoder"])
    await manager.wait(created.job_id)
    snapshot = await manager.snapshot(workspace, created.job_id)

    assert services.max_active_scans == 2
    assert snapshot.state == "awaiting_selection"
    assert [item.source for item in snapshot.providers] == ["codex", "qoder"]
    assert all(item.sessions_total == 2 for item in snapshot.providers)
    assert all(
        item.assets[0].state is ImportAssetState.PENDING
        for item in snapshot.providers
    )
    assert (
        workspace.workspace_dir
        / ".qwenpaw/imports/jobs"
        / f"{created.job_id}.json"
    ).is_file()

    restored = PortabilityImportJobManager(service_factory=services.factory)
    assert (
        await restored.snapshot(workspace, created.job_id)
    ).state == "awaiting_selection"
    assert (await restored.current(workspace)).job_id == created.job_id


@pytest.mark.asyncio
async def test_apply_projects_progress_and_replays_terminal_event(
    tmp_path: Path,
) -> None:
    services = _FakeServices()
    workspace = _workspace(tmp_path)
    manager = PortabilityImportJobManager(service_factory=services.factory)
    created = await manager.create(workspace, ["codex"])
    await manager.wait(created.job_id)

    await manager.start(
        workspace,
        created.job_id,
        {
            "codex": ImportSelection(
                sessions=True,
                skills=["codex-skill"],
            ),
        },
    )
    await manager.wait(created.job_id)
    snapshot = await manager.snapshot(workspace, created.job_id)
    events = [
        event async for event in manager.subscribe(workspace, created.job_id)
    ]

    assert snapshot.state == "completed"
    assert snapshot.providers[0].sessions_processed == 2
    assert snapshot.providers[0].sessions_imported == 1
    assert snapshot.providers[0].assets[0].state is ImportAssetState.SUCCEEDED
    assert snapshot.providers[0].assets[0].enabled is False
    assert "sk-test-secret" not in "\n".join(snapshot.logs)
    assert events[-1]["snapshot"]["state"] == "completed"
    assert [event["seq"] for event in events] == sorted(
        event["seq"] for event in events
    )


@pytest.mark.asyncio
async def test_only_one_active_import_runs_per_agent(tmp_path: Path) -> None:
    services = _FakeServices()
    services.block_apply.clear()
    workspace = _workspace(tmp_path)
    manager = PortabilityImportJobManager(service_factory=services.factory)
    first = await manager.create(workspace, ["codex"])
    with pytest.raises(RuntimeError, match="already active"):
        await manager.create(workspace, ["qoder"])
    await manager.cancel(workspace, first.job_id)


@pytest.mark.asyncio
async def test_empty_selection_is_rejected_and_active_job_can_cancel(
    tmp_path: Path,
) -> None:
    services = _FakeServices()
    workspace = _workspace(tmp_path)
    manager = PortabilityImportJobManager(service_factory=services.factory)
    created = await manager.create(workspace, ["codex"])
    await manager.wait(created.job_id)

    with pytest.raises(ValueError, match="select at least"):
        await manager.start(
            workspace,
            created.job_id,
            {"codex": ImportSelection(sessions=False)},
        )
    assert (await manager.snapshot(workspace, created.job_id)).state == (
        "awaiting_selection"
    )

    services.block_apply.clear()
    await manager.start(
        workspace,
        created.job_id,
        {"codex": ImportSelection(skills=["codex-skill"])},
    )
    await services.apply_started.wait()

    assert (await manager.cancel(workspace, created.job_id)).state == (
        "interrupted"
    )


@pytest.mark.asyncio
async def test_shutdown_cancels_active_jobs_and_rejects_new_ones(
    tmp_path: Path,
) -> None:
    services = _FakeServices()
    services.block_apply.clear()
    workspace = _workspace(tmp_path)
    manager = PortabilityImportJobManager(service_factory=services.factory)
    created = await manager.create(workspace, ["codex"])
    await manager.wait(created.job_id)
    await manager.start(
        workspace,
        created.job_id,
        {"codex": ImportSelection(skills=["codex-skill"])},
    )
    await services.apply_started.wait()

    await manager.shutdown()

    assert (
        await manager.snapshot(workspace, created.job_id)
    ).state == "interrupted"
    with pytest.raises(RuntimeError, match="shutting down"):
        await manager.create(workspace, ["qoder"])


@pytest.mark.asyncio
async def test_cancel_with_rollback_failure_marks_job_failed(
    tmp_path: Path,
) -> None:
    services = _FakeServices()
    services.fail_rollback = True
    workspace = _workspace(tmp_path)
    manager = PortabilityImportJobManager(service_factory=services.factory)
    created = await manager.create(workspace, ["codex"])
    await manager.wait(created.job_id)

    services.block_apply.clear()
    await manager.start(
        workspace,
        created.job_id,
        {"codex": ImportSelection(skills=["codex-skill"])},
    )
    await services.apply_started.wait()

    snapshot = await manager.cancel(workspace, created.job_id)

    assert snapshot.state == "failed"
    assert snapshot.providers[0].state == "failed"
    assert "cleanup unavailable" in snapshot.providers[0].error
    assert snapshot.providers[0].assets[0].message == ("回滚未完成，请根据错误信息人工检查。")


@pytest.mark.asyncio
async def test_provider_failure_does_not_discard_other_result(
    tmp_path: Path,
) -> None:
    services = _FakeServices()
    services.fail_apply.add("qoder")
    workspace = _workspace(tmp_path)
    manager = PortabilityImportJobManager(service_factory=services.factory)
    created = await manager.create(workspace, ["codex", "qoder"])
    await manager.wait(created.job_id)

    await manager.start(
        workspace,
        created.job_id,
        {
            "codex": ImportSelection(skills=["codex-skill"]),
            "qoder": ImportSelection(skills=["qoder-skill"]),
        },
    )
    await manager.wait(created.job_id)
    snapshot = await manager.snapshot(workspace, created.job_id)

    assert snapshot.state == "completed_with_issues"
    assert snapshot.providers[0].assets[0].state is ImportAssetState.SUCCEEDED
    assert snapshot.providers[1].assets[0].state is ImportAssetState.FAILED
    assert "qoder failed" in snapshot.providers[1].error


@pytest.mark.asyncio
async def test_retry_creates_a_new_job_for_selected_failed_tools(
    tmp_path: Path,
) -> None:
    services = _FakeServices()
    services.fail_apply.update({"codex", "qoder"})
    workspace = _workspace(tmp_path)
    manager = PortabilityImportJobManager(service_factory=services.factory)
    original = await manager.create(workspace, ["codex", "qoder"])
    await manager.wait(original.job_id)
    await manager.start(
        workspace,
        original.job_id,
        {
            source: ImportSelection(sessions=False, skills=[f"{source}-skill"])
            for source in ("codex", "qoder")
        },
    )
    await manager.wait(original.job_id)

    retry = await manager.retry(
        workspace,
        original.job_id,
        {"codex": ImportSelection(sessions=False, skills=["codex-skill"])},
    )
    await manager.wait(retry.job_id)
    snapshot = await manager.snapshot(workspace, retry.job_id)

    assert retry.job_id != original.job_id
    assert (retry.mode, retry.retry_of_job_id) == ("retry", original.job_id)
    assert snapshot.state == "completed_with_issues"
    assert len(snapshot.providers) == 2
    assert snapshot.providers[0].assets[0].state is ImportAssetState.SUCCEEDED
    assert snapshot.providers[1].assets[0].state is ImportAssetState.FAILED

    second_retry = await manager.retry(
        workspace,
        retry.job_id,
        {"qoder": ImportSelection(sessions=False, skills=["qoder-skill"])},
    )
    await manager.wait(second_retry.job_id)
    final = await manager.snapshot(workspace, second_retry.job_id)

    assert final.state == "completed"
    assert all(
        provider.assets[0].state is ImportAssetState.SUCCEEDED
        for provider in final.providers
    )
    assert services.retries == [
        (
            "plan-" + "c" * 32,
            ImportSelection(sessions=False, skills=["codex-skill"]),
        ),
        (
            "plan-" + "q" * 32,
            ImportSelection(sessions=False, skills=["qoder-skill"]),
        ),
    ]
