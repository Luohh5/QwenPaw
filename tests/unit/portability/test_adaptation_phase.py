# -*- coding: utf-8 -*-
"""Execution contracts for Pawport's two compatibility phases."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from qwenpaw.agents.acp.meta import ACP_EPHEMERAL_META_KEY
from qwenpaw.app.agent_context import scoped_session_id
from qwenpaw.portability.adaptation_prompts import (
    repair_prompt,
    triage_prompt,
)
from qwenpaw.portability.compatibility import (
    AssetType,
    AssetZone,
    CompatibilityAsset,
)


def _asset(*, tool_budget: int = 32, tool_calls: int = 0):
    return CompatibilityAsset(
        asset_key="skills:demo",
        asset_type=AssetType.SKILL,
        source_id="demo",
        name="demo",
        tool_budget=tool_budget,
        tool_calls=tool_calls,
        updated_at=datetime.now(timezone.utc),
    )


class _Context:
    progress = None

    def __init__(self) -> None:
        self.cleared: list[str] = []

    @staticmethod
    def activity(_session_id: str) -> str:
        return "reading files"

    def clear_activity(self, session_id: str) -> None:
        self.cleared.append(session_id)


class _Workspace:
    agent_id = "agent"

    def __init__(self, action=None) -> None:
        self.action = action
        self.requests = []

    async def stream_query(self, request):
        self.requests.append(request)
        with scoped_session_id(request.session_id):
            if self.action:
                await self.action()
        if not self.requests:
            yield None


def test_phase_specs_preserve_prompt_tools_and_source_zones() -> None:
    from qwenpaw.portability.adaptation_phase import (
        REPAIR_PHASE,
        TRIAGE_PHASE,
    )

    assert (
        TRIAGE_PHASE.name,
        TRIAGE_PHASE.mutable,
        TRIAGE_PHASE.source_zone,
        TRIAGE_PHASE.prompt,
        TRIAGE_PHASE.tools,
    ) == (
        "triage",
        False,
        AssetZone.STAGING,
        triage_prompt,
        (
            "migration_compat_inspect",
            "migration_compat_read_file",
            "migration_compat_classify",
        ),
    )
    assert (
        REPAIR_PHASE.name,
        REPAIR_PHASE.mutable,
        REPAIR_PHASE.source_zone,
        REPAIR_PHASE.prompt,
        REPAIR_PHASE.tools,
    ) == (
        "mission_repair",
        True,
        AssetZone.REPAIR,
        repair_prompt,
        (
            "migration_compat_inspect",
            "migration_compat_read_file",
            "migration_compat_write_file",
            "migration_compat_update",
            "migration_compat_test",
            "migration_compat_classify",
        ),
    )


def test_access_guard_is_asset_scoped_and_cleans_after_error() -> None:
    from qwenpaw.portability.adaptation_phase import (
        AdaptationAccessGuard,
        TRIAGE_PHASE,
    )

    guard = AdaptationAccessGuard()
    context = SimpleNamespace(name="migration")

    with scoped_session_id("session-a"):
        with pytest.raises(PermissionError, match="unavailable"):
            guard.current()
        with pytest.raises(RuntimeError, match="worker failed"):
            with guard.bind(
                "session-a",
                context,
                TRIAGE_PHASE,
                "skills:demo",
            ):
                binding = guard.current(expected_context=context)
                assert binding.asset_key == "skills:demo"
                assert binding.phase is TRIAGE_PHASE
                with pytest.raises(PermissionError, match="context"):
                    guard.current(expected_context=object())
                raise RuntimeError("worker failed")
        with pytest.raises(PermissionError, match="unavailable"):
            guard.current()


def test_access_guard_rejects_duplicate_live_session() -> None:
    from qwenpaw.portability.adaptation_phase import (
        AdaptationAccessGuard,
        TRIAGE_PHASE,
    )

    guard = AdaptationAccessGuard()
    with guard.bind("session-a", object(), TRIAGE_PHASE, "skills:first"):
        with pytest.raises(PermissionError, match="already bound"):
            with guard.bind(
                "session-a",
                object(),
                TRIAGE_PHASE,
                "skills:second",
            ):
                pass


@pytest.mark.asyncio
async def test_phase_runner_builds_scoped_ephemeral_request() -> None:
    from qwenpaw.portability.adaptation_phase import (
        AdaptationAccessGuard,
        PhaseRunner,
        TRIAGE_PHASE,
    )

    guard = AdaptationAccessGuard()
    context = _Context()

    async def assert_binding() -> None:
        binding = guard.current(expected_context=context)
        assert binding.phase is TRIAGE_PHASE
        assert binding.asset_key == "skills:demo"

    workspace = _Workspace(assert_binding)
    runner = PhaseRunner(workspace, context, guard)
    await runner.run_asset(
        _asset(tool_budget=50, tool_calls=10),
        TRIAGE_PHASE,
        session_id="phase-session",
        label="checking demo",
    )

    request = workspace.requests[0].model_dump()
    assert request["session_id"] == "phase-session"
    assert request["agent_id"] == "agent"
    assert request["request_context"] == {
        "source": "portability_adaptation",
        "portability_phase": "triage",
        "max_react_iterations": 80,
        ACP_EPHEMERAL_META_KEY: True,
        "approval_level": "off",
        "subagent_allowed_tools": list(TRIAGE_PHASE.tools),
        "subagent_skills": [],
    }
    assert context.cleared == ["phase-session"]
    with scoped_session_id("phase-session"):
        with pytest.raises(PermissionError, match="unavailable"):
            guard.current()


@pytest.mark.asyncio
async def test_phase_runner_cleans_binding_when_stream_fails() -> None:
    from qwenpaw.portability.adaptation_phase import (
        AdaptationAccessGuard,
        PhaseRunner,
        REPAIR_PHASE,
    )

    async def fail() -> None:
        raise RuntimeError("stream failed")

    guard = AdaptationAccessGuard()
    context = _Context()
    runner = PhaseRunner(_Workspace(fail), context, guard)

    with pytest.raises(RuntimeError, match="stream failed"):
        await runner.run_asset(
            _asset(),
            REPAIR_PHASE,
            session_id="phase-session",
            label="repairing demo",
        )
    assert context.cleared == ["phase-session"]
    with scoped_session_id("phase-session"):
        with pytest.raises(PermissionError, match="unavailable"):
            guard.current()


@pytest.mark.asyncio
async def test_phase_runner_uses_native_three_worker_pool() -> None:
    from qwenpaw.portability.adaptation_phase import (
        AdaptationAccessGuard,
        PhaseRunner,
    )

    runner = PhaseRunner(_Workspace(), _Context(), AdaptationAccessGuard())
    active = 0
    maximum = 0

    async def worker(_key: str) -> None:
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.01)
        active -= 1

    await runner.run_batch(["a", "b", "c", "d"], worker)
    assert maximum == 3
