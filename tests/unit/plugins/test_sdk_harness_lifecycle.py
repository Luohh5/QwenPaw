# -*- coding: utf-8 -*-
"""Real loader admission covers producer, persistence, and workspace owners."""

import asyncio

import pytest

# Pytest registers this imported fixture under its declared name, loader.
# pylint: disable-next=unused-import
from .test_sdk_entry_contract import plugin_loader

# Imported fixture discovery is intentional; helpers are called below.
# pylint: disable-next=unused-import
from .harness_bridge_fixture import (
    SOURCE,
    collect,
    harness_fixture,
    load_backend,
    request,
    until,
)


async def test_unload_busy_during_stream_consumed_from_another_task(harness):
    from qwenpaw.plugins import PluginBusyError
    from qwenpaw.schemas import TextContent

    runtime = harness.runtime()
    stream = runtime.stream("fake-backend", request("hold"))

    async def first_text():
        async for item in stream:
            if isinstance(item, TextContent) and item.text == "first":
                return item

    first = asyncio.create_task(first_text())
    await asyncio.wait_for(first, 5)
    assert first.done()
    backend = harness.plugin.created[0]
    try:
        with pytest.raises(PluginBusyError):
            await harness.loader.unload_plugin("bridge-owner")
        assert not backend.stream_closed.is_set()
    finally:
        await asyncio.create_task(stream.aclose())
    assert backend.stream_closed.is_set()
    report = await harness.loader.unload_plugin("bridge-owner")
    assert report.quiescent and report.clean


async def test_cancel_waits_for_terminal_save(tmp_path, harness):
    from qwenpaw.app.chats.session import SafeJSONSession
    from qwenpaw.plugins import PluginBusyError

    class BlockedSession(SafeJSONSession):
        def __init__(self):
            super().__init__(str(tmp_path / "history"))
            self.entered, self.release = asyncio.Event(), asyncio.Event()

        async def save_session_state(self, *args, **kwargs):
            self.entered.set()
            await self.release.wait()
            await super().save_session_state(*args, **kwargs)

    session = BlockedSession()
    runtime = harness.runtime(session=session)
    consumer = asyncio.create_task(collect(runtime, "hold"))
    try:
        await until(
            lambda: harness.plugin.created
            and harness.plugin.created[0].entered.is_set(),
        )
        consumer.cancel()
        await asyncio.wait_for(session.entered.wait(), 5)
        assert not consumer.done()
        with pytest.raises(PluginBusyError):
            await harness.loader.unload_plugin("bridge-owner")
    finally:
        session.release.set()
        with pytest.raises(asyncio.CancelledError):
            await consumer
    assert harness.plugin.created[0].stream_closed.is_set()
    saved = await session.get_session_state_dict(
        "same-session",
        "user",
        "console",
    )
    assert saved["agent"]["state"]["context"]


async def test_workspace_close_only_closes_own_backend(harness):
    one, two = harness.runtime("one"), harness.runtime("two")
    await one.call("fake-backend", "status")
    await two.call("fake-backend", "status")
    first, second = harness.plugin.created
    await one.close()
    assert first.closed and not second.closed
    assert (await two.call("fake-backend", "status")).installed
    assert len(harness.plugin.created) == 2


async def test_config_rebuild_waits_for_close_and_revokes_old_context(harness):
    from qwenpaw.plugins import ResourceError, StaleContextError

    runtime = harness.runtime()
    await runtime.call("fake-backend", "status", {"model": "one"})
    old = harness.plugin.created[0]
    old.close_release.clear()
    changing = asyncio.create_task(
        runtime.call("fake-backend", "status", {"model": "two"}),
    )
    try:
        await asyncio.wait_for(old.close_entered.wait(), 5)
        assert not changing.done() and len(harness.plugin.created) == 1
    finally:
        old.close_release.set()
        await changing
    assert old.closed and len(harness.plugin.created) == 2
    with pytest.raises((ResourceError, StaleContextError)):
        old.context.resources.defer(lambda: None)


async def test_failed_close_blocks_new_generation(harness):
    from qwenpaw.plugins import StaleContextError

    runtime = harness.runtime()
    await runtime.call("fake-backend", "status")
    original = harness.loader.get_loaded_plugin("bridge-owner")
    generation = harness.loader.lifecycle.get_instance(
        "bridge-owner",
    ).generation
    harness.plugin.close_fail = True
    try:
        result = await harness.loader.lifecycle.reload("bridge-owner")
        assert not result.ok and result.needs_restart and result.errors
        assert harness.loader.get_loaded_plugin("bridge-owner") is original
        assert (
            harness.loader.lifecycle.get_instance("bridge-owner").generation
            == generation
        )
        assert len(harness.plugin.created) == 1
        with pytest.raises(StaleContextError):
            await runtime.call("fake-backend", "status")
    finally:
        harness.plugin.close_fail = False


async def test_archive_holds_activity_until_exit_and_preserves_metadata(
    harness,
):
    from qwenpaw.plugins import PluginBusyError, StaleContextError

    runtime = harness.runtime()
    async with runtime.archive("fake-backend") as archive:
        rows = await archive.list_external_threads(limit=3, archived=True)
        assert rows == (
            {
                "id": "native-thread",
                "archived": True,
                "vendor_metadata": {"kept": True},
            },
        )
        assert harness.plugin.created[0].calls[-1].runtime_capabilities is None
        with pytest.raises(PluginBusyError):
            await harness.loader.unload_plugin("bridge-owner")
    with pytest.raises(StaleContextError):
        await archive.list_external_threads(limit=3, archived=True)
    result = await harness.loader.unload_plugin("bridge-owner")
    assert result.clean and result.quiescent


@pytest.mark.parametrize(
    "case",
    ["missing", "unsupported", "missing-protocol"],
)
async def test_archive_rejects_unavailable_or_unsupported(
    tmp_path,
    harness,
    case,
):
    from qwenpaw.plugins import SDKError
    from qwenpaw.plugins.harnesses import HarnessOperationNotSupportedError

    backend = "unavailable"
    if case != "missing":
        backend = "unsupported"
        source = (
            SOURCE.replace("external_history=True", "external_history=False")
            if case == "unsupported"
            else SOURCE.replace(
                "async def read_external_thread(",
                "async def missing_read_external_thread(",
            )
        )
        await load_backend(
            tmp_path,
            harness.loader,
            owner="archive-owner",
            backend=backend,
            source=source,
        )
    runtime = harness.runtime()
    try:
        with pytest.raises(
            (SDKError, HarnessOperationNotSupportedError),
            match="unavailable|unsupported|support|archive|history",
        ):
            async with runtime.archive(backend):
                pytest.fail("unavailable archive yielded a facade")
    finally:
        if case != "missing":
            await harness.loader.unload_plugin("archive-owner")
