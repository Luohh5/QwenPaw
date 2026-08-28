# -*- coding: utf-8 -*-
"""Minimal ChannelDriver used by the CH-1-001 release fixture."""

from __future__ import annotations

from typing import Any

from qwenpaw.channel_protocol import RunnerLifecycleSpec


class EmptyDriver:
    """Provide a dependency-free driver entrypoint for artifact tests."""

    def create_lifecycle_spec(
        self,
        identity: Any,
        *,
        secret_handle_consumer: Any | None,
    ) -> RunnerLifecycleSpec:
        """Return the smallest lifecycle specification accepted by bootstrap.

        The fixture is metadata-only and is not intended to start a Runner.
        """
        del identity, secret_handle_consumer
        raise RuntimeError("The CH-1-001 fixture is metadata-only")
