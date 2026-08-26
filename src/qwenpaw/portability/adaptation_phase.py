# -*- coding: utf-8 -*-
"""Execution contracts for Pawport compatibility phases."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from ..app.agent_context import get_current_session_id
from .adaptation_prompts import repair_prompt, triage_prompt
from .compatibility import AssetZone, CompatibilityAsset


@dataclass(frozen=True)
class PhaseSpec:
    name: str
    source_zone: AssetZone
    tools: tuple[str, ...]
    prompt: Callable[[CompatibilityAsset], str]
    mutable: bool


@dataclass(frozen=True)
class PhaseOutcome:
    completed: bool
    remaining: int = 0
    reason: str = ""


@dataclass(frozen=True)
class AccessBinding:
    context: Any
    phase: PhaseSpec
    asset_key: str


class AdaptationAccessGuard:
    """Bind private migration tools to one session, phase, and asset."""

    def __init__(self) -> None:
        self._bindings: dict[str, AccessBinding] = {}

    @contextmanager
    def bind(
        self,
        session_id: str,
        context: Any,
        phase: PhaseSpec,
        asset_key: str,
    ) -> Iterator[AccessBinding]:
        if session_id in self._bindings:
            raise PermissionError("migration session is already bound")
        binding = AccessBinding(context, phase, asset_key)
        self._bindings[session_id] = binding
        try:
            yield binding
        finally:
            if self._bindings.get(session_id) is binding:
                self._bindings.pop(session_id, None)

    def current(self, *, expected_context: Any = None) -> AccessBinding:
        binding = self._bindings.get(get_current_session_id() or "")
        if binding is None:
            raise PermissionError(
                "migration compatibility tools are unavailable",
            )
        if (
            expected_context is not None
            and binding.context is not expected_context
        ):
            raise PermissionError("migration request context mismatch")
        return binding


TRIAGE_PHASE = PhaseSpec(
    name="triage",
    source_zone=AssetZone.STAGING,
    tools=(
        "migration_compat_inspect",
        "migration_compat_read_file",
        "migration_compat_classify",
    ),
    prompt=triage_prompt,
    mutable=False,
)
REPAIR_PHASE = PhaseSpec(
    name="mission_repair",
    source_zone=AssetZone.REPAIR,
    tools=(
        "migration_compat_inspect",
        "migration_compat_read_file",
        "migration_compat_write_file",
        "migration_compat_update",
        "migration_compat_test",
        "migration_compat_classify",
    ),
    prompt=repair_prompt,
    mutable=True,
)


__all__ = [
    "AdaptationAccessGuard",
    "PhaseOutcome",
    "PhaseSpec",
    "REPAIR_PHASE",
    "TRIAGE_PHASE",
]
