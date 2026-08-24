# -*- coding: utf-8 -*-
"""Read-only boundary for external Agent Harness migration sources."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Protocol

from ..models import ProviderInventory

ProgressReporter = Callable[[str], Awaitable[None]]
logger = logging.getLogger(__name__)


async def report_progress(
    progress: ProgressReporter | None,
    message: str,
) -> None:
    """Treat presentation failures as non-fatal to migration work."""
    if progress is None:
        return
    try:
        await progress(message)
    except Exception:  # pylint: disable=broad-except
        logger.debug("Migration progress reporter failed", exc_info=True)


class MigrationProvider(Protocol):
    """A source adapter may inspect external state but never mutate it."""

    provider_id: str

    async def inventory(
        self,
        *,
        limit: int,
        progress: ProgressReporter | None = None,
    ) -> ProviderInventory:
        """Return a bounded, normalized inventory from the source."""


__all__ = ["MigrationProvider", "ProgressReporter", "report_progress"]
