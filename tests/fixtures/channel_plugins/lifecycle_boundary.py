# -*- coding: utf-8 -*-
# pylint: disable=protected-access,redefined-outer-name
"""External boundaries for real Feishu channel, SDK and thread lifecycles."""

import asyncio
import importlib
import threading
from types import SimpleNamespace as NS

import httpx
import pytest_asyncio

from registration_suite import channel_class, unused_process
from qwenpaw.config.config import FeishuConfig


async def eventually(predicate, timeout=2.0):
    """Bound observation without fixed sleeps or hanging a failed suite."""
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


async def wait_healthy(channel):
    """Socket allocation precedes the channel's connected acknowledgement."""
    async with asyncio.timeout(2):
        while (await channel.health_check())["status"] != "healthy":
            await asyncio.sleep(0.005)


class SocketBoundary:
    """A socket with real loop affinity and controllable remote disconnect."""

    def __init__(self):
        self.loop = asyncio.get_running_loop()
        self.inbox = asyncio.Queue()
        self.closed = False
        self.receiving = threading.Event()
        self.receive_loops = []
        self.close_count = 0
        self.close_error = None
        self.close_gate = None

    async def recv(self):
        self.receive_loops.append(asyncio.get_running_loop())
        self.receiving.set()
        if self.receive_loops[-1] is not self.loop:
            raise RuntimeError("Socket received on another workspace's loop")
        item = await self.inbox.get()
        if isinstance(item, BaseException):
            raise item
        return item

    async def send(self, _message):
        if self.closed:
            raise RuntimeError("Socket is closed")

    async def close(self):
        self.close_count += 1
        if self.close_error is not None:
            raise self.close_error
        if self.close_gate is not None:
            await eventually(self.close_gate.is_set)
        self.closed = True

    def drop(self):
        self.loop.call_soon_threadsafe(
            self.inbox.put_nowait,
            ConnectionError("remote disconnected"),
        )


@pytest_asyncio.fixture
async def network_runtime(load_plugin, tmp_path, monkeypatch):
    """Keep platform methods and SDK connect real; replace external I/O."""
    cls = channel_class(load_plugin)
    module = importlib.import_module("plugin_feishu.platform")
    boundary = NS(
        module=module,
        channels=[],
        platforms=[],
        callbacks=[],
        sockets=[],
        http_clients=[],
        http_requests=[],
        connection_attempts=0,
        connect_error=None,
        connect_gate=None,
        token=lambda _config: "tenant-test-token",
        http_class=httpx.AsyncClient,
    )
    original_http = httpx.AsyncClient
    original_connect = module.LarkOapiPlatform.connect

    async def response(request):
        boundary.http_requests.append(request)
        if request.url.path.endswith("/bot/v3/info"):
            return httpx.Response(
                200,
                json={"code": 0, "bot": {"open_id": "ou_bot"}},
            )
        return httpx.Response(
            200,
            json={"code": 0, "data": {"message_id": "sent-1"}},
        )

    def http_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(response)
        client = original_http(*args, **kwargs)
        boundary.http_clients.append(client)
        return client

    async def connect_socket(_url, **_kwargs):
        boundary.connection_attempts += 1
        if boundary.connect_gate is not None:
            await eventually(boundary.connect_gate.is_set)
        if boundary.connect_error is not None:
            raise boundary.connect_error
        socket = SocketBoundary()
        boundary.sockets.append(socket)
        return socket

    async def capture_connect(platform, on_message, on_card):
        boundary.platforms.append(platform)
        boundary.callbacks.append((on_message, on_card))
        return await original_connect(platform, on_message, on_card)

    def get_token(config):
        # Resolve dynamically: tests replace token behavior after setup.
        return boundary.token(config)

    monkeypatch.setattr(module.httpx, "AsyncClient", http_client)
    monkeypatch.setattr(
        module.TokenManager,
        "get_self_tenant_token",
        get_token,
    )
    monkeypatch.setattr(
        module.lark_ws.Client,
        "_get_conn_url",
        lambda _self: "wss://test.invalid/?device_id=test-device&service_id=1",
    )
    monkeypatch.setattr(module.lark_ws.websockets, "connect", connect_socket)
    monkeypatch.setattr(module.LarkOapiPlatform, "connect", capture_connect)
    monkeypatch.setattr(module, "_WS_THREAD_JOIN_TIMEOUT", 0.08)
    monkeypatch.setattr(module, "_WS_DISCONNECT_TIMEOUT", 0.08)
    monkeypatch.setattr(module, "_WS_PENDING_CANCEL_TIMEOUT", 0.08)
    monkeypatch.setattr(module, "_WS_HEALTH_CHECK_INTERVAL", 0.02)

    def channel(name="workspace", **settings):
        instance = cls.from_config(
            unused_process,
            FeishuConfig(
                enabled=True,
                app_id="cli_test",
                app_secret="secret",
                **settings,
            ),
            workspace_dir=tmp_path / name,
        )
        boundary.channels.append(instance)
        return instance

    boundary.channel = channel
    yield boundary
    if boundary.connect_gate is not None:
        boundary.connect_gate.set()
    for instance in boundary.channels:
        try:
            await asyncio.wait_for(instance.stop(), 2)
        except (RuntimeError, TimeoutError):
            pass
    for platform in boundary.platforms:
        loop = platform._ws_loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        thread = platform._ws_thread
        if thread is not None:
            await asyncio.to_thread(thread.join, 1)
    for client in boundary.http_clients:
        if not client.is_closed:
            await client.aclose()
