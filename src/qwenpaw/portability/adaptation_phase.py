# -*- coding: utf-8 -*-
"""Execution contracts for Pawport compatibility phases."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from ..agents.acp.meta import ACP_EPHEMERAL_META_KEY
from ..agents.tools.agent_management import MAX_SPAWN_BATCH_CONCURRENCY
from ..app.agent_context import get_current_session_id
from ..schemas import AgentRequest
from ..utils.io_utils import run_async_to_completion
from .adaptation_prompts import repair_prompt, triage_prompt
from .compatibility import AssetZone, CompatibilityAsset
from .providers.base import report_progress

_MAX_REACT_ITERATIONS = 4_000
_HEARTBEAT_SECONDS = 12


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


class PhaseRunner:
    """Run isolated phase workers through the normal Agent interface."""

    def __init__(
        self,
        workspace: Any,
        context: Any,
        guard: AdaptationAccessGuard,
    ) -> None:
        self.workspace = workspace
        self.context = context
        self.guard = guard

    @staticmethod
    def _request(
        asset: CompatibilityAsset,
        phase: PhaseSpec,
        session_id: str,
        agent_id: str,
    ) -> AgentRequest:
        iterations = min(
            _MAX_REACT_ITERATIONS,
            max(80, (asset.tool_budget - asset.tool_calls) * 2),
        )
        return AgentRequest.model_validate(
            {
                "input": [
                    {
                        "role": "user",
                        "type": "message",
                        "content": [
                            {
                                "type": "text",
                                "text": phase.prompt(asset),
                            },
                        ],
                    },
                ],
                "session_id": session_id,
                "user_id": session_id,
                "agent_id": agent_id,
                "channel": "console",
                "request_context": {
                    "source": "portability_adaptation",
                    "portability_phase": phase.name,
                    "max_react_iterations": iterations,
                    ACP_EPHEMERAL_META_KEY: True,
                    "approval_level": "off",
                    "subagent_allowed_tools": list(phase.tools),
                    "subagent_skills": [],
                },
            },
        )

    async def run_asset(
        self,
        asset: CompatibilityAsset,
        phase: PhaseSpec,
        *,
        session_id: str,
        label: str,
    ) -> None:
        async def consume() -> None:
            request = self._request(
                asset,
                phase,
                session_id,
                self.workspace.agent_id,
            )
            async for _event in self.workspace.stream_query(request):
                pass

        with self.guard.bind(
            session_id,
            self.context,
            phase,
            asset.asset_key,
        ):
            task = asyncio.create_task(consume())
            try:
                while not task.done():
                    done, _ = await asyncio.wait(
                        {task},
                        timeout=_HEARTBEAT_SECONDS,
                    )
                    if task not in done:
                        await report_progress(
                            self.context.progress,
                            f"{label}仍在运行："
                            f"{self.context.activity(session_id)}",
                        )
                await task
            finally:
                if not task.done():
                    task.cancel()
                    try:
                        await run_async_to_completion(task)
                    except (
                        asyncio.CancelledError,
                        Exception,
                    ):  # pylint: disable=broad-except
                        pass
                self.context.clear_activity(session_id)

    @staticmethod
    async def run_batch(
        keys: list[str],
        worker: Callable[[str], Awaitable[None]],
    ) -> None:
        semaphore = asyncio.Semaphore(MAX_SPAWN_BATCH_CONCURRENCY)

        async def run(key: str) -> None:
            async with semaphore:
                await worker(key)

        results = await asyncio.gather(
            *(run(key) for key in keys),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result


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
    "PhaseRunner",
    "PhaseSpec",
    "REPAIR_PHASE",
    "TRIAGE_PHASE",
]
