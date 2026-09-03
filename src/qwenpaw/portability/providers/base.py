# -*- coding: utf-8 -*-
"""Read-only boundary for external Agent Harness migration sources."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

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


async def report_result(
    progress: ProgressReporter | None,
    kind: str,
    *values: Any,
) -> None:
    await report_progress(
        progress,
        f"\x1e{kind}\t" + "\t".join(map(str, values)),
    )


def progress_milestone(index: int, total: int) -> bool:
    step = max(1, total // 20)
    return total <= 20 or index in {1, total} or index % step == 0


def make_inventory(
    provider_id: str,
    **values: Any,
) -> ProviderInventory:
    return ProviderInventory(
        provider_id=provider_id,
        provider_name=provider_id.title(),
        **values,
    )


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
