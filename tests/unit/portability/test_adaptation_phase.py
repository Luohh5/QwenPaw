# -*- coding: utf-8 -*-
"""Execution contracts for Pawport's two compatibility phases."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from qwenpaw.app.agent_context import scoped_session_id
from qwenpaw.portability.adaptation_prompts import (
    repair_prompt,
    triage_prompt,
)
from qwenpaw.portability.compatibility import AssetZone


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
