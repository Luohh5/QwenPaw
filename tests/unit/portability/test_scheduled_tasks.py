# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from qwenpaw.portability.models import SourceScheduledTask
from qwenpaw.app.crons.manager import CronManager
from qwenpaw.portability.scheduled_tasks import (
    build_imported_job,
    imported_job_id,
    imported_job_source,
)


def test_build_imported_job_is_stable_disabled_and_safe(tmp_path) -> None:
    task = SourceScheduledTask(
        source_id="source-task",
        name="Daily report",
        schedule_type="cron",
        cron="0 9 * * 1",
        timezone="Asia/Shanghai",
        prompt="Create the daily report",
        cwd=str(tmp_path),
        enabled=True,
        metadata={"model": "source-only-model"},
    )

    first = build_imported_job("codex", task)
    second = build_imported_job("codex", task)

    assert first.id == second.id == imported_job_id("codex", "source-task")
    assert first.enabled is False
    assert first.schedule.cron == "0 9 * * mon"
    assert first.runtime.tool_safety is True
    assert first.runtime.share_session is False
    assert first.dispatch.silent is True
    assert first.request is not None
    assert first.request.model_dump()["request_context"]["project_dir"] == str(
        tmp_path.resolve(),
    )
    assert imported_job_source(first) == ("codex", "source-task")
    assert first.meta["portability"]["source_enabled"] is True
    assert first.meta["portability"]["requires_review"] is True


def test_mission_reviewed_job_is_disabled_without_review_gate(
    tmp_path,
) -> None:
    task = SourceScheduledTask(
        source_id="approved",
        name="Approved",
        schedule_type="cron",
        cron="0 9 * * *",
        prompt="Create the report",
        cwd=str(tmp_path),
    )

    job = build_imported_job("codex", task, reviewed=True)

    assert job.enabled is False
    assert job.runtime.tool_safety is True
    assert job.meta["portability"]["requires_review"] is False
    assert job.meta["portability"]["safety"] == "reviewed_disabled"
    assert job.request.request_context["portability_review_required"] is False
    assert CronManager.requires_portability_review(job) is False


def test_build_imported_job_rejects_unsupported_and_expired_tasks() -> None:
    unsupported = SourceScheduledTask(
        source_id="complex",
        name="Complex",
        schedule_type="unsupported",
        prompt="Run",
        metadata={"unsupported_reason": "RRULE contains BYSECOND"},
    )
    expired = SourceScheduledTask(
        source_id="past",
        name="Past",
        schedule_type="once",
        run_at=datetime.now(timezone.utc) - timedelta(days=1),
        prompt="Run once",
    )

    with pytest.raises(ValueError, match="BYSECOND"):
        build_imported_job("codex", unsupported)
    with pytest.raises(ValueError, match="已经过期"):
        build_imported_job("qoder", expired)


def test_build_imported_job_does_not_bind_missing_source_cwd(tmp_path) -> None:
    task = SourceScheduledTask(
        source_id="missing-cwd",
        name="Missing cwd",
        schedule_type="cron",
        cron="*/30 * * * *",
        prompt="Check status",
        cwd=str(tmp_path / "gone"),
    )

    job = build_imported_job("qoder", task)

    assert job.request is not None
    assert "project_dir" not in job.request.model_dump()["request_context"]
    assert job.meta["portability"]["source_cwd_available"] is False


@pytest.mark.parametrize(
    "metadata",
    [
        {"source_target_remote_authority": "ssh-remote+host"},
        {"target_remote_authority": "dev-container"},
        {"remote_unverified": True},
        {"workspace_status": "remote_unverified"},
        {"execution_environment": "cloud"},
    ],
)
def test_build_imported_job_never_binds_remote_cwd_that_exists_locally(
    tmp_path,
    metadata,
) -> None:
    task = SourceScheduledTask(
        source_id="remote-cwd",
        name="Remote workspace",
        schedule_type="cron",
        cron="0 9 * * *",
        prompt="Review the remote repository",
        cwd=str(tmp_path),
        metadata=metadata,
    )

    job = build_imported_job("qoder", task)

    assert job.request is not None
    assert "project_dir" not in job.request.model_dump()["request_context"]
    portability = job.meta["portability"]
    assert portability["source_cwd_available"] is False
    assert portability["source_cwd_remote_or_unverified"] is True
    assert portability["source_cwd_binding"] == (
        "omitted_remote_or_unverified"
    )
    assert portability["requires_review"] is True


def test_build_imported_job_keeps_provenance_bounded(tmp_path) -> None:
    task = SourceScheduledTask(
        source_id="bounded",
        name="Bounded provenance",
        schedule_type="cron",
        cron="0 9 * * *",
        prompt="Run",
        cwd=str(tmp_path),
        metadata={
            "long": "x" * 5000,
            "controls": "before\x00after",
            "many": list(range(100)),
        },
    )

    job = build_imported_job("codex", task)
    source_metadata = job.meta["portability"]["source_metadata"]

    assert len(source_metadata["long"]) == 2048
    assert "\x00" not in source_metadata["controls"]
    assert len(source_metadata["many"]) == 64
