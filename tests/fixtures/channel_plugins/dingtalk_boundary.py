# -*- coding: utf-8 -*-
# pylint: disable=protected-access,redefined-outer-name
"""Network boundaries retaining real SDKs, sessions and worker threads."""

import asyncio
import json
import socket as socket_module
import threading
from types import SimpleNamespace as NS

import aiohttp
from dingtalk_stream import stream
import pytest_asyncio
import requests
from alibabacloud_dingtalk.oauth2_1_0.client import Client as OAuthClient
from alibabacloud_dingtalk.oauth2_1_0 import models as oauth_models
from alibabacloud_dingtalk.robot_1_0.client import Client as RobotClient
from alibabacloud_dingtalk.robot_1_0 import models as robot_models
from alibabacloud_dingtalk.card_1_0.client import Client as CardClient
from alibabacloud_dingtalk.card_1_0 import models as card_models
from dingtalk_registration_suite import channel_class, unused_process
from qwenpaw.config.config import DingTalkConfig


async def eventually(predicate, timeout=3):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


class Socket:
    def __init__(self):
        self.loop = asyncio.get_running_loop()
        self.inbox = asyncio.Queue()
        self.closed = False
        self.open = True
        self.receiving = threading.Event()
        self.close_gate = None
        self.close_started = threading.Event()
        self.sent = []

    def __aiter__(self):
        return self

    async def __anext__(self):
        self.receiving.set()
        assert asyncio.get_running_loop() is self.loop
        value = await self.inbox.get()
        if value is None:
            raise StopAsyncIteration
        return value

    async def ping(self):
        future = self.loop.create_future()
        future.set_result(None)
        return future

    async def send(self, value):
        self.sent.append(json.loads(value))

    async def close(self):
        self.close_started.set()
        if self.close_gate:
            await eventually(self.close_gate.is_set)
        self.closed = True
        self.open = False
        self.inbox.put_nowait(None)

    def receive(self, value):
        self.loop.call_soon_threadsafe(
            self.inbox.put_nowait,
            json.dumps(value),
        )


class HttpResponse:
    status = 200
    headers = {}

    def __init__(self, data, *, text_error=None, raw_text=None):
        self.data = data
        self.text_error = text_error
        self.raw_text = raw_text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        self.release()
        await self.wait_for_close()

    async def read(self):
        return (
            self.data
            if isinstance(self.data, bytes)
            else json.dumps(self.data).encode()
        )

    async def text(self):
        if self.text_error is not None:
            raise self.text_error
        return (
            self.raw_text
            if self.raw_text is not None
            else json.dumps(self.data)
        )

    async def json(self, **_kwargs):
        return self.data

    def release(self):
        pass

    async def wait_for_close(self):
        pass


# One fixture owns all network patches and their matching cleanup.
# pylint: disable=too-many-statements
@pytest_asyncio.fixture
async def runtime(load_plugin, tmp_path, monkeypatch):
    cls, _ = channel_class(load_plugin)
    boundary = NS(
        channels=[],
        sockets=[],
        http=[],
        sync_http=[],
        sdk=[],
        webhook_error=None,
        webhook_body_error=None,
        webhook_raw_text=None,
        webhook_response={"errcode": 0},
        sdk_error={},
        sdk_gate={},
        sdk_entered={},
        connect_gate=None,
        sync_failures=0,
        connect_entered=threading.Event(),
    )

    def sync_post(url, **kwargs):
        boundary.sync_http.append((url, kwargs))
        boundary.connect_entered.set()
        if boundary.sync_failures:
            boundary.sync_failures -= 1
            raise requests.ConnectionError("gateway unreachable")
        if boundary.connect_gate:
            assert boundary.connect_gate.wait(
                5,
            ), "Test did not release network gate"
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(
            {
                "endpoint": "wss://gateway.test/ws",
                "ticket": "test",
                "accessToken": "stream-token",
                "expireIn": 7200,
            },
        ).encode()
        return response

    class Connection:
        def __init__(self, *_args, **_kwargs):
            self.socket = None

        async def __aenter__(self):
            self.socket = Socket()
            boundary.sockets.append(self.socket)
            return self.socket

        async def __aexit__(self, *_args):
            await self.socket.close()

    async def http_request(session, _method, url, **kwargs):
        assert not session.closed
        boundary.http.append((str(url), kwargs))
        if str(url) == "https://download.test/image":
            response = HttpResponse(b"\x89PNG\r\n\x1a\noriginal image")
            response.headers = {"Content-Type": "image/png"}
            return response
        if "/media/upload" in str(url):
            return HttpResponse({"errcode": 0, "media_id": "@media-one"})
        if boundary.webhook_error:
            raise boundary.webhook_error
        return HttpResponse(
            boundary.webhook_response,
            text_error=boundary.webhook_body_error,
            raw_text=boundary.webhook_raw_text,
        )

    async def token(_self, request):
        boundary.sdk.append(("token", request, None, None))
        return oauth_models.GetAccessTokenResponse(
            body=oauth_models.GetAccessTokenResponseBody(
                access_token="api-token",
                expire_in=7200,
            ),
        )

    def endpoint(name):
        async def call(_self, request, headers, options):
            boundary.sdk.append((name, request, headers, options))
            boundary.sdk_entered.setdefault(name, asyncio.Event()).set()
            if boundary.sdk_gate.get(name):
                await boundary.sdk_gate[name].wait()
            if boundary.sdk_error.get(name):
                raise boundary.sdk_error[name]
            if name == "download":
                return robot_models.RobotMessageFileDownloadResponse(
                    body=robot_models.RobotMessageFileDownloadResponseBody(
                        download_url="https://download.test/image",
                    ),
                )
            if name == "deliver":
                return card_models.DeliverCardResponse(
                    body=card_models.DeliverCardResponseBody(),
                )
            return None

        return call

    # SDK local-IP probing normally opens a UDP route; keep it offline.
    monkeypatch.setattr(socket_module.socket, "connect", lambda *_args: None)
    monkeypatch.setattr(requests, "post", sync_post)
    monkeypatch.setattr(stream.websockets, "connect", Connection)
    monkeypatch.setattr(aiohttp.ClientSession, "_request", http_request)
    monkeypatch.setattr(OAuthClient, "get_access_token_async", token)
    for target, method, name in [
        (RobotClient, "org_group_send_with_options_async", "group"),
        (
            RobotClient,
            "robot_message_file_download_with_options_async",
            "download",
        ),
        (RobotClient, "batch_send_otowith_options_async", "dm"),
        (CardClient, "create_card_with_options_async", "create"),
        (CardClient, "deliver_card_with_options_async", "deliver"),
        (CardClient, "streaming_update_with_options_async", "update"),
    ]:
        monkeypatch.setattr(target, method, endpoint(name))

    def channel(name="workspace", **settings):
        instance = cls.from_config(
            unused_process,
            DingTalkConfig(
                enabled=True,
                client_id="client-one",
                client_secret="secret",
                **settings,
            ),
            workspace_dir=tmp_path / name,
        )
        boundary.channels.append(instance)
        return instance

    boundary.channel = channel
    yield boundary
    for gate in boundary.sdk_gate.values():
        gate.set()
    if boundary.connect_gate:
        boundary.connect_gate.set()
    for socket in boundary.sockets:
        if socket.close_gate:
            socket.close_gate.set()
    for channel in boundary.channels:
        try:
            await asyncio.wait_for(channel.stop(), 6)
        except (RuntimeError, TimeoutError):
            pass
        thread = channel._stream_thread
        if thread and thread.is_alive():
            channel._stop_event.set()
            await asyncio.to_thread(thread.join, 6)
            assert not thread.is_alive(), "Worker leaked beyond test teardown"
        if channel._http is not None and not channel._http.closed:
            await channel._http.close()
