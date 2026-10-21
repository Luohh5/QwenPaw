# -*- coding: utf-8 -*-
"""SDK stop preparation through the real manager and workspace barriers."""

import asyncio

import pytest

from qwenpaw.plugins import SDKError
from qwenpaw.plugins.channels import ChannelMessage
from qwenpaw.plugins.lifecycle import QuiescenceError
from qwenpaw.plugins.messages import ContentBlock
from qwenpaw.plugins.testing import PluginTestHost

from .sdk_author_fixture import make_plugin

SOURCE = """
import asyncio
from qwenpaw.plugins.channels import ChannelSpec
from qwenpaw.plugins.commands import CommandSpec, CommandResult
from qwenpaw.plugins.messages import ContentBlock, Message
class Plugin:
    def register(self, plugin):
        class Transport:
            async def start(self, call): pass
            async def send(self, call, message):
                plugin.storage.write_json("sending.json", True)
                try:
                    while not plugin.storage.read_json("release.json", False):
                        await asyncio.sleep(.01)
                finally:
                    plugin.storage.write_json("cleanup-entered.json", True)
                    try:
                        while plugin.storage.read_json("hold-cleanup.json", False):
                            await asyncio.sleep(.01)
                    except asyncio.CancelledError:
                        plugin.storage.write_json("cleanup-cancelled.json", True)
                        raise
                    plugin.storage.write_json("finished.json", True)
            async def stop(self, call):
                plugin.storage.write_json("stopped.json",
                    plugin.storage.read_json("finished.json", False))
                return True
        async def reply(call, args):
            return CommandResult("reply", Message("assistant", (ContentBlock(text="answer"),)))
        plugin.register_command(CommandSpec("stopprobe", reply))
        plugin.register_channel(ChannelSpec("stop-probe", "Stop probe", Transport))
        plugin.register_channel(ChannelSpec("other-probe", "Other probe", Transport))
plugin = Plugin()
"""


def message():
    return ChannelMessage("sender", (ContentBlock(text="/stopprobe"),))


async def wait_sending(ctx):
    async with asyncio.timeout(3):
        while not ctx.storage.read_json("sending.json", False):
            await asyncio.sleep(0.01)


@pytest.fixture
async def running_channel(tmp_path):
    async with PluginTestHost(tmp_path / "host") as host:
        ctx = await host.load(
            make_plugin(tmp_path, SOURCE, ["channels", "commands", "storage"]),
        )
        await host.start_channel("stop-probe")
        workspace = await host._workspace("test")
        try:
            yield host, ctx, workspace
        finally:
            try:
                current = host.context("extension-contract")
            except SDKError:
                current = None
            if current is ctx:
                ctx.storage.write_json("release.json", True)
                ctx.storage.write_json("hold-cleanup.json", False)


@pytest.mark.parametrize("stop_workspace", [False, True])
async def test_shutdown_prepares_hung_sdk_send(
    running_channel,
    stop_workspace,
):
    host, ctx, workspace = running_channel
    await host.receive_channel("stop-probe", message())
    await wait_sending(ctx)
    if stop_workspace:
        # PluginTestHost builds real workspaces without starting every service.
        workspace._start_attempted = True
        shutdown = workspace.stop()
    else:
        shutdown = workspace.channel_manager.stop_all()
    await asyncio.wait_for(shutdown, 3)
    assert ctx.storage.read_json("finished.json", False)
    assert ctx.storage.read_json("stopped.json", False)
    assert await workspace.channel_manager.get_channel("stop-probe") is None
    host.assert_quiescent("extension-contract")


async def test_ordinary_quiesce_does_not_cancel_sdk_send(running_channel):
    host, ctx, workspace = running_channel
    manager = workspace.channel_manager
    await host.receive_channel("stop-probe", message())
    await wait_sending(ctx)
    assert not await manager.quiesce(timeout=0.03)
    assert not ctx.storage.read_json("finished.json", False)
    assert ctx.storage.read_json("stopped.json", None) is None
    ctx.storage.write_json("release.json", True)
    assert await manager.quiesce(timeout=3)
    assert ctx.storage.read_json("finished.json", False)
    assert ctx.storage.read_json("stopped.json", None) is None
    await host.stop_channel("stop-probe")


@pytest.mark.parametrize("stop_other", [False, True])
async def test_overlapping_stops_preserve_sdk_call_cleanup(
    running_channel,
    monkeypatch,
    stop_other,
):
    host, ctx, workspace = running_channel
    manager = workspace.channel_manager
    if stop_other:
        await host.start_channel("other-probe")
    ctx.storage.write_json("hold-cleanup.json", True)
    await host.receive_channel("stop-probe", message())
    await wait_sending(ctx)
    stopping_one = asyncio.create_task(manager.stop_one("stop-probe"))
    async with asyncio.timeout(3):
        while not ctx.storage.read_json("cleanup-entered.json", False):
            await asyncio.sleep(0.01)
    channel = await manager.get_channel("stop-probe")
    prepare = channel.prepare_stop
    second_preparing = asyncio.Event()

    async def observe_prepare():
        second_preparing.set()
        return await prepare()

    monkeypatch.setattr(channel, "prepare_stop", observe_prepare)
    stopping_all = asyncio.create_task(manager.stop_all())
    try:
        await asyncio.wait_for(second_preparing.wait(), 3)
        done, _ = await asyncio.wait(
            {stopping_one, stopping_all},
            timeout=0.05,
        )
        assert not done
        assert not ctx.storage.read_json("cleanup-cancelled.json", False)
        assert ctx.storage.read_json("stopped.json", None) is None
        if stop_other:
            assert (await manager.stop_one("other-probe")).stopped
    finally:
        ctx.storage.write_json("hold-cleanup.json", False)
        results = await asyncio.wait_for(
            asyncio.gather(stopping_one, stopping_all),
            3,
        )
    assert results[0].stopped
    assert ctx.storage.read_json("finished.json", False)
    assert ctx.storage.read_json("stopped.json", False)
    if stop_other:
        await manager.start_all()
        await host.start_channel("other-probe")
        await host.stop_channel("other-probe")


@pytest.mark.parametrize("stop_all", [False, True])
async def test_prepare_failure_retains_sdk_owner_for_retry(
    running_channel,
    monkeypatch,
    stop_all,
):
    host, ctx, workspace = running_channel
    manager = workspace.channel_manager
    channel = await manager.get_channel("stop-probe")
    prepare = channel.prepare_stop
    await host.receive_channel("stop-probe", message())
    await wait_sending(ctx)

    async def fail_after_preparing():
        await prepare()
        raise SDKError("prepare interrupted")

    monkeypatch.setattr(channel, "prepare_stop", fail_after_preparing)
    if stop_all:
        with pytest.raises(QuiescenceError, match="prepare interrupted"):
            await manager.stop_all()
    else:
        receipt = await manager.stop_one("stop-probe")
        assert not receipt.stopped and "prepare interrupted" in receipt.detail
    assert await manager.get_channel("stop-probe") is channel
    assert ctx.storage.read_json("finished.json", False)
    assert ctx.storage.read_json("stopped.json", None) is None
    with pytest.raises(SDKError):
        await host.receive_channel("stop-probe", message())
    assert not (await host.reload("extension-contract")).ok
    assert host.context("extension-contract") is ctx
    monkeypatch.setattr(channel, "prepare_stop", prepare)
    assert (await manager.stop_one("stop-probe")).stopped
    assert await manager.get_channel("stop-probe") is None
    result = await host.reload("extension-contract")
    assert result.ok, result.errors


@pytest.mark.parametrize("prepare_also", [False, True])
async def test_external_consumer_cancellation_propagates(
    running_channel,
    prepare_also,
):
    _, ctx, workspace = running_channel
    channel = await workspace.channel_manager.get_channel("stop-probe")
    request = channel.build_agent_request_from_native(message())
    consumer = asyncio.create_task(channel.consume_one(request))
    try:
        await wait_sending(ctx)
        consumer.cancel()
        if prepare_also:
            await channel.prepare_stop()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(consumer, 3)
        assert consumer.cancelled()
        await channel.prepare_stop()
        assert ctx.storage.read_json("finished.json", False)
        assert ctx.storage.read_json("stopped.json", None) is None
    finally:
        if not consumer.done():
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
