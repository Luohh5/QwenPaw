# -*- coding: utf-8 -*-
"""iLink login, native messages, media and polling lifecycle."""
import asyncio
import base64
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

from qwenpaw.channel_protocol.models import SendParams
from tests.channel_support.driver import bind, module

pytestmark = pytest.mark.asyncio


def message(number, *, group="", text="hello"):
    return {
        "message_type": 1,
        "from_user_id": "user-A",
        "to_user_id": "bot",
        "context_token": f"context-{number}",
        "msg_id": str(number),
        "group_id": group,
        "item_list": [{"type": 1, "text_item": {"text": text}}],
    }


@pytest_asyncio.fixture
async def runtime(tmp_path, monkeypatch):
    requests, clients = [], []
    real_client = httpx.AsyncClient
    polling, qr_release = asyncio.Event(), asyncio.Event()
    upload = {}

    async def transport(request):
        requests.append(request)
        path = request.url.path
        if path.endswith("/getupdates"):
            await polling.wait()
            return httpx.Response(200, json={"ret": 0, "msgs": []})
        if path.endswith("/get_bot_qrcode"):
            value = {
                "qrcode": "scan-code",
                "qrcode_img_content": "https://login.test/scan",
            }
        elif path.endswith("/get_qrcode_status"):
            await qr_release.wait()
            value = {
                "status": "confirmed",
                "bot_token": "fresh-token",
                "baseurl": "https://assigned.test",
            }
        elif path.endswith("/getuploadurl"):
            upload.update(json.loads(request.content))
            value = {"upload_full_url": "https://cdn.test/upload"}
        elif path == "/upload":
            return httpx.Response(
                200,
                headers={"x-encrypted-param": "uploaded-image"},
            )
        elif path == "/image":
            return httpx.Response(200, content=b"image-download")
        elif path.endswith("/getconfig"):
            value = {"typing_ticket": "ticket"}
        else:
            assert path.endswith(("/sendmessage", "/sendtyping")), path
            value = {"ret": 0}
        return httpx.Response(200, json=value)

    def external_client(**kwargs):
        client = real_client(
            **kwargs,
            transport=httpx.MockTransport(transport),
            trust_env=False,
        )
        clients.append(client)
        return client

    monkeypatch.setattr(
        module("wechat", "client").httpx,
        "AsyncClient",
        external_client,
    )
    drivers = []

    async def start(*, token="existing-token"):
        driver = module("wechat").WeChatDriver()
        peer, identity = bind("wechat", driver, tmp_path)
        drivers.append(driver)
        await driver.start(
            {
                "bot_token": token,
                "base_url": "https://ilink.test",
                "bot_token_file": str(tmp_path / "token"),
                "workspace_dir": str(tmp_path),
            },
        )
        return SimpleNamespace(
            driver=driver,
            peer=peer,
            identity=identity.to_mapping(),
        )

    yield SimpleNamespace(
        start=start,
        requests=requests,
        qr_release=qr_release,
        upload=upload,
    )
    qr_release.set()
    for driver in drivers:
        await driver.stop()
    assert all(client.is_closed for client in clients)


def sent(runtime):
    return [
        json.loads(r.content)["msg"]
        for r in runtime.requests
        if r.url.path.endswith("/sendmessage")
    ]


async def send(rt, handle, parts):
    return await rt.peer.methods["channel.send"](
        SendParams.from_mapping(
            {
                **rt.identity,
                "to_handle": handle,
                "delivery_id": "reply",
                "operation": "message.create",
                "content_parts": parts,
            },
        ),
        None,
    )


async def test_existing_login_connects_without_qr(runtime):
    rt = await runtime.start()
    assert rt.driver._connected
    assert rt.driver._platform._client.bot_token == "existing-token"
    assert not any("qrcode" in r.url.path for r in runtime.requests)


async def test_first_scan_transitions_from_auth_required_to_connected(
    runtime,
    tmp_path,
):
    starting = asyncio.create_task(runtime.start(token=""))
    async with asyncio.timeout(2):
        while not any(
            r.url.path.endswith("/get_qrcode_status") for r in runtime.requests
        ):
            await asyncio.sleep(0.005)
    assert not starting.done()
    runtime.qr_release.set()
    rt = await starting
    assert rt.driver._connected
    assert (tmp_path / "token").read_text() == "fresh-token"
    assert rt.driver._platform._client.base_url == "https://assigned.test"
    notices = [
        params
        for name, params in rt.peer.notifications
        if name == "channel.auth.required"
    ]
    assert notices[0]["scan_url"] == "https://login.test/scan"


@pytest.mark.parametrize(
    "group,session,kind",
    [
        ("", "wechat:user-A", "dm"),
        ("group-A", "wechat:group:group-A", "group"),
    ],
)
async def test_native_dm_and_group_routes_send_with_context(
    runtime,
    group,
    session,
    kind,
):
    rt = await runtime.start()
    await rt.driver._platform._message(message(1, group=group))
    event = rt.peer.batches[0]["events"][0]
    assert event["sender_id"] == event["acl_sender_id"] == "user-A"
    assert event["metadata"]["session_id"] == session
    assert event["conversation"]["type"] == kind
    assert event["content_parts"] == [{"type": "text", "text": "hello"}]
    assert (
        await send(
            rt,
            event["response_handle"],
            [{"type": "text", "text": "answer"}],
        )
    )["state"] == "acknowledged"
    assert sent(runtime)[-1]["to_user_id"] == "user-A"
    assert sent(runtime)[-1]["context_token"] == "context-1"
    assert sent(runtime)[-1]["item_list"] == [
        {"type": 1, "text_item": {"text": "answer"}},
    ]


async def test_outbound_image_encrypts_and_sends_native_media(
    runtime,
    tmp_path,
):
    rt = await runtime.start()
    await rt.driver._platform._message(message(2))
    event = rt.peer.batches[0]["events"][0]
    path = tmp_path / "picture.png"
    raw = b"\x89PNG\r\n\x1a\nimage-body"
    path.write_bytes(raw)
    result = await send(
        rt,
        event["response_handle"],
        [{"type": "image", "image_url": str(path)}],
    )
    assert result["state"] == "acknowledged"
    upload_request = next(
        r for r in runtime.requests if r.url.path == "/upload"
    )
    key = base64.b64encode(bytes.fromhex(runtime.upload["aeskey"])).decode()
    assert (
        module("wechat", "utils").aes_ecb_decrypt(upload_request.content, key)
        == raw
    )
    assert runtime.upload["rawfilemd5"] == hashlib.md5(raw).hexdigest()
    item = sent(runtime)[-1]["item_list"][0]
    assert item["type"] == 2
    assert (
        item["image_item"]["media"]["encrypt_query_param"] == "uploaded-image"
    )


async def test_stop_cancels_poll_and_closes_clients(runtime):
    rt = await runtime.start()
    platform = rt.driver._platform
    poll = platform._poll_task
    await rt.driver.stop()
    assert poll.done()
    assert platform._client is platform._poll_client is None
    assert rt.driver._platform is None and not rt.driver._connected
