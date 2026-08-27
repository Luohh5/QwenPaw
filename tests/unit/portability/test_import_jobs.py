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
from qwenpaw.portability.models import (
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
        self.fail_apply: set[str] = set()

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
                            fidelity="agent_decision",
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
                await owner.block_apply.wait()
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

        return Service()


def test_only_materialization_milestone_updates_session_progress() -> None:
    provider = ImportProviderSnapshot(source="qoder", sessions_total=1)

    PortabilityImportJobManager._project_progress(
        provider,
        "正在扫描 Qoder 会话：2/2",
    )
    assert (provider.sessions_processed, provider.sessions_total) == (0, 1)

    PortabilityImportJobManager._project_progress(
        provider,
        "正在写入会话：1/1（聊天记录阶段）",
    )
    assert (provider.sessions_processed, provider.sessions_total) == (1, 1)


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
async def test_only_one_apply_runs_per_agent(tmp_path: Path) -> None:
    services = _FakeServices()
    services.block_apply.clear()
    workspace = _workspace(tmp_path)
    manager = PortabilityImportJobManager(service_factory=services.factory)
    first = await manager.create(workspace, ["codex"])
    second = await manager.create(workspace, ["qoder"])
    await asyncio.gather(
        manager.wait(first.job_id),
        manager.wait(second.job_id),
    )
    await manager.start(
        workspace,
        first.job_id,
        {"codex": ImportSelection(skills=["codex-skill"])},
    )

    with pytest.raises(RuntimeError, match="already running"):
        await manager.start(
            workspace,
            second.job_id,
            {"qoder": ImportSelection(skills=["qoder-skill"])},
        )

    services.block_apply.set()
    await manager.wait(first.job_id)


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
