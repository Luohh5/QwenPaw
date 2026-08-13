# -*- coding: utf-8 -*-
"""Read-only boundary for external Agent Harness migration sources."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from ..models import ProviderInventory

ProgressReporter = Callable[[str], Awaitable[None]]


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


__all__ = ["MigrationProvider", "ProgressReporter"]
