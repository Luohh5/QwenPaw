# -*- coding: utf-8 -*-
"""Manager races must preserve cleanup and latest source ownership."""

import asyncio
from contextlib import suppress

import pytest

from qwenpaw.drivers.contracts import DriverCard
from qwenpaw.drivers.credentials.store import AsyncCredentialStore
from qwenpaw.drivers.constants import DRIVER_SCOPE_CONTEXT_KEY
from qwenpaw.drivers.errors import DriverNotFoundError
from qwenpaw.drivers.handler import DriverHandler
from qwenpaw.drivers.manager import DriverManager


@pytest.fixture(name="setup_manager")
def _setup_manager(tmp_path):
    created, closed = [], []
    fast_ready, slow_ready, release = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )

    class Handler(DriverHandler):
        async def _setup(self):
            created.append((self.name, self.card.config.get("label", "")))
            if self.card.config.get("wait"):
                slow_ready.set()
                await release.wait()
            else:
                fast_ready.set()

        async def _teardown(self):
            if self.card.config.get("fail_close") and not release.is_set():
                raise RuntimeError("synthetic generic close failure")
            closed.append((self.name, self.card.config.get("label", "")))

    manager = DriverManager(
        tmp_path / "drivers",
        AsyncCredentialStore(tmp_path / "credentials.yaml"),
    )
    manager.register_handler_type("race", Handler)
    return manager, created, closed, fast_ready, slow_ready, release


def card(name, *, wait=False, label=""):
    return DriverCard(
        name=name,
        protocol="race",
        endpoint={},
        config={"wait": wait, "label": label},
    )


async def test_bulk_cancel_closes_successful_unpublished_and_pending_handler(
    setup_manager,
):
    manager, created, closed, fast_ready, slow_ready, release = setup_manager
    await manager.card_store.save(card("fast"))
    await manager.card_store.save(card("slow", wait=True))
    pending = asyncio.create_task(manager.build_drivers())
    try:
        await asyncio.wait_for(fast_ready.wait(), 2)
        await asyncio.wait_for(slow_ready.wait(), 2)
        # Give the fast init coroutine its return boundary, while gather is
        # still suspended on its slow sibling and nothing is published.
        await asyncio.sleep(0)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        await manager.shutdown_all()
        assert sorted(created) == [("fast", ""), ("slow", "")]
        assert sorted(closed) == sorted(created)
    finally:
        release.set()
        if not pending.done():
            pending.cancel()
            with suppress(asyncio.CancelledError):
                await pending
        await manager.shutdown_all()


async def test_old_revision_cannot_publish_after_new_registration(
    setup_manager,
):
    manager, created, closed, _, slow_ready, release = setup_manager
    pending = asyncio.create_task(
        manager.register_driver(
            card("same", wait=True, label="old"),
        ),
    )
    try:
        await asyncio.wait_for(slow_ready.wait(), 2)
        await manager.register_driver(card("same", label="new"))
        release.set()
        # Refusing stale work may raise, but it must never replace the new
        # runtime or rewrite the latest persisted card.
        with suppress(RuntimeError):
            await pending
        stored = await manager.card_store.load("same", protocol="race")
        assert stored.config["label"] == "new"
        assert ("same", "old") in closed
        assert ("same", "new") not in closed
        assert created == [("same", "old"), ("same", "new")]
    finally:
        release.set()
        await manager.shutdown_all()


async def test_bulk_old_revision_cannot_replace_new_registration(
    setup_manager,
):
    manager, _, closed, _, slow_ready, release = setup_manager
    await manager.card_store.save(card("same", wait=True, label="old"))
    pending = asyncio.create_task(manager.build_drivers())
    try:
        await asyncio.wait_for(slow_ready.wait(), 2)
        await manager.register_driver(card("same", label="new"))
        release.set()
        with suppress(RuntimeError):
            await pending
        assert ("same", "old") in closed
        assert ("same", "new") not in closed
        assert (
            await manager.card_store.load(
                "same",
                protocol="race",
            )
        ).config["label"] == "new"
    finally:
        release.set()
        await manager.shutdown_all()


async def test_generic_close_failure_retains_owner_for_shutdown_retry(
    setup_manager,
):
    manager, created, closed, _, _, release = setup_manager
    source = card("same", label="owned")
    source.config["fail_close"] = True
    await manager.register_driver(source)
    try:
        for _ in range(2):
            with pytest.raises(RuntimeError, match="(?i)cleanup|close|stop"):
                await manager.shutdown_all()
            assert not closed
        assert created == [("same", "owned")]
    finally:
        release.set()
        await manager.shutdown_all()
    assert closed == [("same", "owned")]
    await manager.shutdown_all()
    assert closed == [("same", "owned")]


async def test_transient_scope_removal_revokes_pending_initialization(
    setup_manager,
):
    manager, created, closed, fast_ready, slow_ready, release = setup_manager
    pending = asyncio.create_task(
        manager.replace_transient_drivers(
            "scope-pending",
            [card("fast"), card("slow", wait=True)],
        ),
    )
    try:
        await asyncio.wait_for(fast_ready.wait(), 2)
        await asyncio.wait_for(slow_ready.wait(), 2)
        await manager.remove_transient_drivers("scope-pending")
        release.set()
        with pytest.raises((asyncio.CancelledError, RuntimeError)):
            await pending
        assert sorted(created) == [("fast", ""), ("slow", "")]
        assert sorted(closed) == sorted(created)
        for name in ("fast", "slow"):
            with pytest.raises(DriverNotFoundError):
                await manager.list_driver_capabilities(
                    name,
                    request_context={
                        DRIVER_SCOPE_CONTEXT_KEY: "scope-pending",
                    },
                )
    finally:
        release.set()
        if not pending.done():
            pending.cancel()
            with suppress(asyncio.CancelledError, RuntimeError):
                await pending
        await manager.shutdown_all()
