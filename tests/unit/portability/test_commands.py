# -*- coding: utf-8 -*-
from __future__ import annotations

from types import SimpleNamespace

import pytest

from qwenpaw.runtime.commands.control.base import ControlContext
from qwenpaw.runtime.commands.control.portability_handler import (
    ExportCommandHandler,
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
async def test_export_rejects_removed_profiles() -> None:
    with pytest.raises(ValueError, match="Only `backup` and `trace`"):
        await ExportCommandHandler().handle(_context("to portable"))


@pytest.mark.asyncio
async def test_export_help_exposes_only_two_profiles() -> None:
    text = await ExportCommandHandler().handle(_context("help"))
    assert "/export to backup" in text
    assert "/export to trace" in text
    assert "/export portable" not in text
    assert "/export handoff" not in text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "handler,raw",
    [
        (ImportCommandHandler(), "from codex"),
        (ExportCommandHandler(), "to trace"),
    ],
)
async def test_portability_commands_reject_remote_channels(
    handler,
    raw: str,
) -> None:
    with pytest.raises(PermissionError, match="local Console/ACP"):
        await handler.handle(_remote_context(raw))
