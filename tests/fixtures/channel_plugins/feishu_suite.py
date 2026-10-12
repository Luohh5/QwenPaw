# -*- coding: utf-8 -*-
# pylint: disable=protected-access,redefined-outer-name
"""Plugin message contracts, derived from the built-in channel and platform."""

import asyncio
import importlib
import io
import json
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from registration_suite import channel_class, unused_process

from qwenpaw.schemas import (
    TextContent,
    ImageContent,
    FileContent,
    RefusalContent,
    Message,
    RunStatus,
)
from qwenpaw.config.config import FeishuConfig
from qwenpaw.schemas import AgentRequest
from qwenpaw.app.approvals.service import (
    ApprovalIdentityPolicy,
    ApprovalService,
    PendingApproval,
)
from qwenpaw.runtime.commands.control.base import ControlContext
from qwenpaw.runtime.commands.control.approval_handler import (
    ApprovalCommandHandler,
)


def make_channel(load_plugin, tmp_path, **settings):
    return channel_class(load_plugin).from_config(
        unused_process,
        FeishuConfig(
            enabled=True,
            app_id="cli_test",
            app_secret="secret",
            **settings,
        ),
        workspace_dir=tmp_path,
    )


def test_native_request_preserves_session_and_real_identity(
    load_plugin,
    tmp_path,
):
    channel = make_channel(load_plugin, tmp_path)
    meta = {
        "feishu_sender_id": "ou_alice",
        "feishu_chat_id": "oc_group",
        "feishu_chat_type": "group",
        "feishu_message_id": "om_original",
    }
    request = channel.build_agent_request_from_native(
        {
            "sender_id": "Alice#1234",
            "session_id": "existing-session",
            "content_parts": [TextContent(type="text", text="hello")],
            "meta": meta,
        },
    )
    assert isinstance(request, AgentRequest)
    assert request.session_id == "existing-session"
    assert request.user_id == "ou_alice"
    assert request.channel_meta == meta
    assert (
        channel.get_to_handle_from_request(request)
        == "feishu:sw:existing-session"
    )
    assert channel.resolve_session_id("ou_alice", meta) == "test_oc_group"


def sdk_event(
    mid,
    *,
    sender="ou_alice",
    chat="oc_room",
    group=False,
    mentioned=False,
    thread="",
    kind="text",
    content=None,
):
    """SDK-shaped event: conversion stays in the real platform adapter."""
    return NS(
        header=NS(event_id=mid),
        event=NS(
            sender=NS(sender_id=NS(open_id=sender), name="Alice"),
            message=NS(
                message_id=mid,
                chat_id=chat,
                chat_type="group" if group else "p2p",
                thread_id=thread,
                message_type=kind,
                content=json.dumps(
                    content
                    or {"text": "@_user_1 hello" if mentioned else "hello"},
                ),
                mentions=(
                    [NS(id=NS(open_id="ou_bot"), key="@_user_1")]
                    if mentioned
                    else []
                ),
            ),
        ),
    )


class SdkBoundary:
    """Fake SDK calls; run real request builders and delivery code."""

    def __init__(self):
        def ok(**data):
            return NS(success=lambda: True, data=NS(**data))

        self.create = AsyncMock(return_value=ok(message_id="sent-1"))
        self.reply = AsyncMock(return_value=ok(message_id="reply-1"))
        self.image = AsyncMock(return_value=ok(image_key="img-uploaded"))
        self.file = AsyncMock(return_value=ok(file_key="file-uploaded"))
        self.resource = AsyncMock(
            side_effect=lambda request: NS(
                success=lambda: True,
                file=io.BytesIO(b"media-content"),
            ),
        )
        self.card = AsyncMock(return_value=ok(card_id="card-1"))
        self.update = AsyncMock(return_value=ok())
        self.settings = AsyncMock(return_value=ok())
        self.element = AsyncMock(return_value=ok())
        self.reaction = AsyncMock(return_value=ok())
        self.client = NS(
            im=NS(
                v1=NS(
                    message=NS(acreate=self.create, areply=self.reply),
                    image=NS(acreate=self.image),
                    file=NS(acreate=self.file),
                    message_resource=NS(aget=self.resource),
                    message_reaction=NS(acreate=self.reaction),
                ),
            ),
            cardkit=NS(
                v1=NS(
                    card=NS(acreate=self.card, asettings=self.settings),
                    card_element=NS(acontent=self.update, apatch=self.element),
                ),
            ),
        )


@pytest_asyncio.fixture
async def runtime(load_plugin, tmp_path, monkeypatch):
    channels = []

    async def start(**settings):
        channel = make_channel(load_plugin, tmp_path, **settings)
        module = importlib.import_module("plugin_feishu.platform")
        sdk = SdkBoundary()
        captured = NS(channel=channel, sdk=sdk, queued=[], module=module)

        async def prepare(platform, config, secret, media_work_dir):
            platform._config = dict(config)
            platform._secret = dict(secret)
            platform._media_work_dir = media_work_dir
            media_work_dir.mkdir(parents=True, exist_ok=True)
            platform._bot_open_id = "ou_bot"
            platform._client = sdk.client

        async def connect(platform, on_message, on_card):
            captured.platform = platform
            captured.on_message = on_message
            captured.on_card = on_card

        monkeypatch.setattr(module.LarkOapiPlatform, "prepare", prepare)
        monkeypatch.setattr(module.LarkOapiPlatform, "connect", connect)
        channel.set_enqueue(captured.queued.append)
        await channel.start()
        channels.append(channel)

        async def dispatch(event):
            await captured.platform._dispatch_native_message(
                event,
                captured.on_message,
            )

        captured.dispatch = dispatch
        return captured

    yield start
    for channel in channels:
        await channel.stop()


def post_text(request):
    return json.loads(request.request_body.content)["zh_cn"]["content"][0][0][
        "text"
    ]


@pytest.mark.asyncio
async def test_platform_dispatch_dm_group_mention_and_dedup(runtime):
    rt = await runtime(require_mention=True)
    for item in [
        sdk_event("dm"),
        sdk_event("unmentioned", group=True),
        sdk_event("mentioned", group=True, mentioned=True),
        sdk_event("mentioned", group=True, mentioned=True),
        sdk_event("self", sender="ou_bot"),
    ]:
        await rt.dispatch(item)
    assert [item["meta"]["feishu_message_id"] for item in rt.queued] == [
        "dm",
        "mentioned",
    ]
    assert rt.queued[1]["acl_sender_id"] == "ou_alice"
    assert rt.queued[1]["meta"]["bot_mentioned"] is True
    assert (
        rt.channel.build_agent_request_from_native(rt.queued[1])
        .input[0]
        .content[0]
        .text
        == "hello"
    )
    assert (
        rt.channel.build_agent_request_from_native(rt.queued[0]).user_id
        == "ou_alice"
    )


@pytest.mark.asyncio
async def test_host_acl_uses_real_sender_for_shared_session(
    runtime,
    monkeypatch,
):
    rt = await runtime(access_control_group=True, share_session_in_group=True)
    await rt.dispatch(sdk_event("allowed", group=True))
    payload = rt.queued[0]
    assert payload["acl_sender_id"] == "ou_alice"
    assert (
        rt.channel.build_agent_request_from_native(payload).user_id == "group"
    )
    checked = []
    store = NS(
        is_whitelisted=lambda channel, sender: checked.append(
            (channel, sender),
        )
        or False,
        is_blacklisted=lambda channel, sender: True,
    )
    monkeypatch.setattr(
        "qwenpaw.app.channels.base.get_access_control_store",
        lambda *args, **kwargs: store,
    )
    await rt.channel.consume_one(payload)
    assert checked == [("feishu", "ou_alice")]
    assert (
        rt.sdk.create.await_count == 1
    )  # Host gate sends denial; unused_process must never run.
    assert rt.sdk.create.call_args.args[0].request_body.receive_id == "oc_room"


@pytest.mark.asyncio
async def test_active_send_resolves_both_legacy_json_orders(runtime, tmp_path):
    (tmp_path / "feishu_receive_ids.json").write_text(
        json.dumps(
            {
                "old": ["ou_old", "open_id"],
                "new": ["chat_id", "oc_new"],
            },
        ),
    )
    rt = await runtime(bot_prefix="[bot]")
    for handle in [
        "feishu:sw:old",
        "feishu:sw:new",
        "feishu:chat_id:oc_direct",
        "feishu:open_id:ou_direct",
        "oc_raw",
        "ou_raw",
    ]:
        await rt.channel.send(handle, "hello")
    requests = [call.args[0] for call in rt.sdk.create.call_args_list]
    assert [r.request_body.receive_id for r in requests] == [
        "ou_old",
        "oc_new",
        "oc_direct",
        "ou_direct",
        "oc_raw",
        "ou_raw",
    ]
    assert all(post_text(r) == "[bot]hello" for r in requests)
    await rt.channel.send(
        "ignored",
        "override",
        {
            "feishu_receive_id": "oc_meta",
            "feishu_receive_id_type": "chat_id",
            "bot_prefix": "[meta]",
        },
    )
    assert rt.sdk.create.call_args.args[0].request_body.receive_id == "oc_meta"
    assert post_text(rt.sdk.create.call_args.args[0]) == "[meta]override"


@pytest.mark.asyncio
async def test_media_download_conversion_upload_and_refusal(runtime):
    rt = await runtime(bot_prefix="[bot]")
    for kind, content in [
        ("image", {"image_key": "img-in"}),
        ("file", {"file_key": "file-in", "file_name": "report.pdf"}),
    ]:
        await rt.dispatch(sdk_event(kind, kind=kind, content=content))
    requests = [
        rt.channel.build_agent_request_from_native(item) for item in rt.queued
    ]
    image = requests[0].input[0].content[0]
    file = requests[1].input[0].content[0]
    assert isinstance(image, ImageContent) and isinstance(file, FileContent)
    assert (
        Path(image.image_url.removeprefix("file://")).read_bytes()
        == b"media-content"
    )
    assert (
        Path(file.file_url.removeprefix("file://")).read_bytes()
        == b"media-content"
    )
    await rt.channel.send_content_parts(
        "ou_alice",
        [
            TextContent(text="hello"),
            RefusalContent(refusal="cannot comply"),
            image,
            file,
        ],
    )
    sent = [call.args[0] for call in rt.sdk.create.call_args_list]
    assert [r.request_body.msg_type for r in sent] == ["post", "image", "file"]
    assert post_text(sent[0]) == "[bot]  hello\ncannot comply"
    assert json.loads(sent[1].request_body.content) == {
        "image_key": "img-uploaded",
    }
    assert json.loads(sent[2].request_body.content) == {
        "file_key": "file-uploaded",
    }
    assert rt.sdk.image.await_count == rt.sdk.file.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operator,shared,thread,policy,authorized",
    [
        ("ou_alice", False, "", ApprovalIdentityPolicy.EXACT_REQUESTER, True),
        ("ou_bob", False, "", ApprovalIdentityPolicy.EXACT_REQUESTER, False),
        ("ou_alice", True, "", ApprovalIdentityPolicy.EXACT_REQUESTER, False),
        (
            "ou_alice",
            False,
            "omt-topic",
            ApprovalIdentityPolicy.EXACT_REQUESTER,
            False,
        ),
        ("ou_bob", True, "", ApprovalIdentityPolicy.AGENT, True),
        ("ou_bob", False, "omt-topic", ApprovalIdentityPolicy.AGENT, True),
    ],
)
async def test_approval_card_callback_preserves_session_and_operator_acl(
    runtime,
    operator,
    shared,
    thread,
    policy,
    authorized,
):
    rt = await runtime(share_session_in_group=shared)
    await rt.dispatch(sdk_event("approval-source", group=True, thread=thread))
    request = rt.channel.build_agent_request_from_native(rt.queued.pop())
    meta = dict(request.channel_meta)
    if shared:
        assert request.user_id == "group"
    elif thread:
        assert request.user_id == "thread:mt-topic"
    event = Message(
        content=[TextContent(text="Allow this tool?")],
        metadata={
            "metadata": {
                "message_type": "tool_guard_approval",
                "approval_request_id": "approval-1",
                "tool_name": "shell",
                "severity": "high",
            },
        },
    )
    await rt.channel.on_event_message_completed(
        request,
        rt.channel.get_to_handle_from_request(request),
        event,
        meta,
    )
    sent = rt.sdk.create.call_args.args[0]
    assert sent.request_body.msg_type == "interactive"
    card = json.loads(sent.request_body.content)
    action = card["elements"][-1]["actions"][0]["value"]
    assert action["request_id"] == "approval-1"
    assert action["session_ctx"]["session_id"] == request.session_id
    assert action["session_ctx"]["sender_id"] == request.user_id
    rt.platform._runner_loop = asyncio.get_running_loop()
    done = asyncio.Event()

    async def delivered(native):
        await rt.on_card(native)
        done.set()

    response = rt.platform._on_card_sync(
        NS(
            header=NS(event_id="card-event"),
            event=NS(
                operator=NS(open_id=operator),
                action=NS(value=action),
            ),
        ),
        delivered,
    )
    await asyncio.wait_for(done.wait(), 3)
    assert response.toast is not None
    assert len(rt.queued) == 1
    payload = rt.queued[0]
    callback_request = rt.channel.build_agent_request_from_native(payload)
    assert (
        callback_request.input[0].content[0].text
        == "/approval approve approval-1"
    )
    assert callback_request.session_id == request.session_id
    assert payload["meta"]["feishu_receive_id"] == "oc_room"
    assert payload["meta"]["feishu_receive_id_type"] == "chat_id"
    if thread:
        assert payload["meta"]["feishu_thread_id"] == thread
        assert payload["meta"]["feishu_message_id"] == "approval-source"
    # Match the host control-command identity chain, not only channel ACL.
    context = ControlContext(
        workspace=None,
        payload=callback_request,
        channel=rt.channel,
        session_id=callback_request.session_id,
        user_id=callback_request.user_id,
        agent_id="default",
        args={"action": "approve", "request_id": "approval-1"},
    )
    actor = ApprovalCommandHandler._actor(context)
    pending = PendingApproval(
        request_id="approval-1",
        session_id=request.session_id,
        root_session_id=request.session_id,
        owner_agent_id="default",
        user_id=request.user_id,
        channel="feishu",
        agent_id="default",
        tool_name="reme:save",
        created_at=0,
        future=asyncio.get_running_loop().create_future(),
        identity_policy=policy,
    )
    assert ApprovalService.actor_can_resolve(pending, actor) is authorized
    assert callback_request.user_id == operator
    assert payload["acl_sender_id"] == operator


@pytest.mark.asyncio
async def test_streaming_card_updates_and_finalizes_without_duplicate(runtime):
    rt = await runtime(streaming_enabled=True, bot_prefix="[bot]")
    await rt.dispatch(sdk_event("stream"))
    request = rt.channel.build_agent_request_from_native(rt.queued[0])
    target = rt.channel.get_to_handle_from_request(request)
    meta = dict(request.channel_meta)
    await rt.channel.on_streaming_start(request, target, None, meta, "message")
    await rt.channel.on_streaming_delta(
        request,
        target,
        None,
        meta,
        "message",
        "hello",
    )
    await rt.channel.on_streaming_end(
        request,
        target,
        None,
        meta,
        "message",
        "hello world",
    )
    assert rt.sdk.card.await_count == 1
    assert rt.sdk.create.await_count == 1
    assert (
        rt.sdk.update.call_args.args[0].request_body.content
        == "[bot]  hello world"
    )
    assert rt.sdk.settings.await_count == 1
    assert (
        json.loads(rt.sdk.settings.call_args.args[0].request_body.settings)[
            "config"
        ]["streaming_mode"]
        is False
    )
    sequences = [
        call.args[0].request_body.sequence
        for call in rt.sdk.update.call_args_list
    ]
    assert sequences == sorted(set(sequences))


@pytest.mark.asyncio
async def test_definite_stream_creation_failure_falls_back_to_original_target(
    runtime,
):
    rt = await runtime(streaming_enabled=True)
    rt.sdk.card.return_value = NS(success=lambda: False, data=None)
    await rt.dispatch(sdk_event("stream-a", sender="ou_a"))
    request = rt.channel.build_agent_request_from_native(rt.queued[0])
    target, meta = rt.channel.get_to_handle_from_request(request), dict(
        request.channel_meta,
    )
    await rt.channel.on_streaming_start(request, target, None, meta, "message")
    await rt.dispatch(sdk_event("newer", sender="ou_b"))
    await rt.channel.on_streaming_end(
        request,
        target,
        None,
        meta,
        "message",
        "fallback answer",
    )
    assert rt.sdk.create.await_count == 1
    assert rt.sdk.create.call_args.args[0].request_body.receive_id == "ou_a"
    assert post_text(rt.sdk.create.call_args.args[0]) == "fallback answer"


@pytest.mark.asyncio
async def test_concurrent_replies_keep_original_thread(runtime):
    rt = await runtime(streaming_enabled=True)
    for mid in ["first", "second"]:
        await rt.dispatch(sdk_event(mid, group=True, thread="omt_" + mid))
    gates = {mid: asyncio.Event() for mid in ["first", "second"]}
    entered = {mid: asyncio.Event() for mid in gates}

    async def process(request):
        mid = request.channel_meta["feishu_message_id"]
        entered[mid].set()
        await gates[mid].wait()
        yield Message(
            object="message",
            status=RunStatus.Completed,
            role="assistant",
            content=[TextContent(text=mid)],
        )

    rt.channel._process = (
        process  # Host Agent boundary; channel/adapter logic remains real.
    )
    tasks = [
        asyncio.create_task(rt.channel.consume_one(item)) for item in rt.queued
    ]
    await asyncio.wait_for(
        asyncio.gather(*(event.wait() for event in entered.values())),
        3,
    )
    gates["second"].set()
    await tasks[1]
    gates["first"].set()
    await tasks[0]
    assert [
        (call.args[0].message_id, post_text(call.args[0]))
        for call in rt.sdk.reply.call_args_list
    ] == [("second", "second"), ("first", "first")]
    assert rt.sdk.create.await_count == 0
    # Topic stream hooks deliberately fall back to normal threaded replies.
    request = rt.channel.build_agent_request_from_native(rt.queued[0])
    target, meta = rt.channel.get_to_handle_from_request(request), dict(
        request.channel_meta,
    )
    await rt.channel.on_streaming_start(request, target, None, meta, "message")
    await rt.channel.on_streaming_end(
        request,
        target,
        None,
        meta,
        "message",
        "thread fallback",
    )
    assert rt.sdk.card.await_count == 0
    assert rt.sdk.reply.call_args.args[0].message_id == "first"
    assert post_text(rt.sdk.reply.call_args.args[0]) == "thread fallback"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["send", "parts", "approval", "stream"])
async def test_ambiguous_send_does_not_blindly_retry(runtime, operation):
    rt = await runtime(streaming_enabled=True)
    rt.sdk.create.side_effect = TimeoutError(
        "server may have accepted the message",
    )
    error_type = rt.module.FeishuDeliveryError
    with pytest.raises(error_type) as caught:
        if operation == "send":
            await rt.channel.send("ou_alice", "answer")
        elif operation == "parts":
            await rt.channel.send_content_parts(
                "ou_alice",
                [TextContent(text="answer")],
            )
        elif operation == "approval":
            await rt.channel.on_event_message_completed(
                None,
                "ou_alice",
                Message(
                    content=[TextContent(text="approve?")],
                    metadata={
                        "message_type": "tool_guard_approval",
                        "approval_request_id": "req",
                    },
                ),
                {},
            )
        else:
            await rt.channel.on_streaming_start(
                None,
                "ou_alice",
                None,
                {},
                "message",
            )
    assert caught.value.side_effect_possible is True
    assert rt.sdk.create.await_count == 1
    assert rt.sdk.reply.await_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "show_thinking,collapse",
    [(True, True), (True, False), (False, True)],
)
async def test_host_streaming_honors_reasoning_display_and_collapse(
    runtime,
    show_thinking,
    collapse,
):
    rt = await runtime(
        streaming_enabled=True,
        show_thinking=show_thinking,
        auto_collapse_thinking=collapse,
        bot_prefix="[bot]",
    )
    await rt.dispatch(sdk_event("reasoning"))

    async def process(_request):
        for kind, text in [
            ("reasoning", "considering options"),
            ("message", "the answer"),
        ]:
            yield Message(
                object="message",
                id=kind,
                type=kind,
                role="assistant",
                status=RunStatus.InProgress,
            )
            yield Message(
                object="message",
                id=kind,
                type=kind,
                role="assistant",
                status=RunStatus.Completed,
                content=[TextContent(text=text)],
            )

    rt.channel._process = process
    events = [
        item async for item in rt.channel._stream_with_tracker(rt.queued[0])
    ]
    assert events
    assert rt.sdk.card.await_count == (2 if show_thinking else 1)
    assert rt.sdk.create.await_count == rt.sdk.card.await_count
    assert rt.sdk.element.await_count == int(show_thinking and collapse)
    cards = [
        json.loads(call.args[0].request_body.data)
        for call in rt.sdk.card.call_args_list
    ]
    if show_thinking:
        panel = cards[0]["body"]["elements"][0]
        assert (
            panel["tag"] == "collapsible_panel" and panel["expanded"] is True
        )
        assert "💭" in panel["elements"][0]["content"]
        assert (
            rt.sdk.update.call_args_list[0].args[0].request_body.content
            == "[bot]  💭 considering options"
        )
    assert (
        rt.sdk.update.call_args.args[0].request_body.content
        == "[bot]  the answer"
    )
    if show_thinking and collapse:
        patch = rt.sdk.element.call_args.args[0]
        assert json.loads(patch.request_body.partial_element) == {
            "expanded": False,
        }
        assert (
            patch.request_body.sequence
            > rt.sdk.settings.call_args_list[0].args[0].request_body.sequence
        )
    # The host's before-process hook saves a route reusable by proactive sends.
    stored = json.loads(
        (rt.channel._workspace_dir / "feishu_receive_ids.json").read_text(),
    )
    session = rt.channel.build_agent_request_from_native(
        rt.queued[0],
    ).session_id
    assert stored[session] == ["open_id", "ou_alice"]


@pytest.mark.asyncio
async def test_ambiguous_thread_reply_and_stream_update_do_not_create_fallback(
    runtime,
):
    rt = await runtime(streaming_enabled=True)
    await rt.dispatch(sdk_event("thread", group=True, thread="omt-thread"))
    meta = dict(rt.queued[0]["meta"])
    rt.sdk.reply.side_effect = TimeoutError("reply may exist")
    with pytest.raises(rt.module.FeishuDeliveryError) as caught:
        await rt.channel.send_content_parts(
            "unused",
            [TextContent(text="reply")],
            meta,
        )
    assert caught.value.side_effect_possible is True
    assert rt.sdk.reply.await_count == 1
    assert rt.sdk.create.await_count == 0
    stream_meta = {
        "feishu_receive_id": "ou_alice",
        "feishu_receive_id_type": "open_id",
    }
    await rt.channel.on_streaming_start(
        None,
        "ou_alice",
        None,
        stream_meta,
        "message",
    )
    rt.sdk.update.side_effect = TimeoutError("update may exist")
    with pytest.raises(rt.module.FeishuDeliveryError) as caught:
        await rt.channel.on_streaming_end(
            None,
            "ou_alice",
            None,
            stream_meta,
            "message",
            "final",
        )
    assert caught.value.side_effect_possible is True
    assert rt.sdk.create.await_count == 1  # Only the original card message.
    assert rt.sdk.update.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "domain,expected",
    [
        ("feishu", "https://open.feishu.cn"),
        ("lark", "https://open.larksuite.com"),
        ("http://localhost:8000", "http://localhost:8000"),
        ("https://gateway.test", "https://gateway.test"),
    ],
)
async def test_real_prepare_passes_configured_domain_to_sdk(
    load_plugin,
    tmp_path,
    monkeypatch,
    domain,
    expected,
):
    """Old channel._sdk_domain passes custom gateway URLs through unchanged."""
    channel = make_channel(load_plugin, tmp_path, domain=domain)
    module = importlib.import_module("plugin_feishu.platform")
    token_domains = []

    def token_boundary(config):
        # Observe the real SDK client configuration at its first network call.
        token_domains.append(config.domain)
        return "test-tenant-token"

    monkeypatch.setattr(
        module.TokenManager,
        "get_self_tenant_token",
        token_boundary,
    )
    monkeypatch.setattr(module.LarkOapiPlatform, "connect", AsyncMock())
    try:
        await channel.start()
        assert token_domains == [expected]
    finally:
        await channel.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["approve", "deny"])
async def test_card_callback_acknowledges_receipt_without_claiming_resolution(
    runtime,
    action,
):
    """The SDK callback precedes host authorization of Bob."""
    rt = await runtime()
    rt.platform._runner_loop = asyncio.get_running_loop()
    delivered = asyncio.Event()

    async def callback(native):
        await rt.on_card(native)
        delivered.set()

    response = rt.platform._on_card_sync(
        NS(
            header=NS(event_id=f"bob-{action}"),
            event=NS(
                operator=NS(open_id="ou_bob"),
                action=NS(
                    value={
                        "type": "tool_guard_approval",
                        "action": action,
                        "request_id": "alice-request",
                        "tool_name": "reme:save",
                        "session_ctx": {
                            "session_id": "original-session",
                            "sender_id": "ou_alice",
                            "receive_id": "oc_room",
                            "receive_id_type": "chat_id",
                        },
                    },
                ),
            ),
        ),
        callback,
    )
    # No host authorization has run when the synchronous SDK response returns.
    assert not delivered.is_set()
    await asyncio.wait_for(delivered.wait(), 3)
    assert response.card is None
    assert response.toast.type == "info"
    assert "approved" not in response.toast.content.casefold()
    assert "denied" not in response.toast.content.casefold()


@pytest.mark.asyncio
@pytest.mark.parametrize("topic", [False, True])
@pytest.mark.parametrize(
    "command",
    [
        "/approval approve pending",
        " /APPROVE pending ",
        "/deny pending",
        "/authorize-feishu-test approve pending",
    ],
)
async def test_typed_approval_commands_use_operator_with_host_policy(
    runtime,
    monkeypatch,
    topic,
    command,
):
    from qwenpaw.runtime.commands import control

    # Register through the host API so aliases follow handler identity.
    monkeypatch.setattr(
        control,
        "_COMMAND_REGISTRY",
        dict(control._COMMAND_REGISTRY),
    )
    alias = ApprovalCommandHandler()
    alias.command_name = "/authorize-feishu-test"
    control.register_command(alias)
    assert control.is_control_command(command)
    rt = await runtime(share_session_in_group=not topic)
    await rt.dispatch(
        sdk_event(
            "typed-approval",
            sender="ou_bob",
            group=True,
            thread="omt-topic" if topic else "",
            content={"text": command},
        ),
    )
    payload = rt.queued[0]
    original_meta = dict(payload["meta"])
    synthetic_id = "thread:mt-topic" if topic else "group"
    assert original_meta["feishu_sender_id"] == synthetic_id
    request = rt.channel.build_agent_request_from_native(payload)
    context = ControlContext(
        workspace=None,
        payload=request,
        channel=rt.channel,
        session_id=request.session_id,
        user_id=request.user_id,
        agent_id="default",
        args={},
    )
    actor = ApprovalCommandHandler._actor(context)
    pending = PendingApproval(
        request_id="pending",
        session_id=request.session_id,
        root_session_id=request.session_id,
        owner_agent_id="default",
        user_id=synthetic_id,
        channel="feishu",
        agent_id="default",
        tool_name="reme:save",
        created_at=0,
        future=asyncio.get_running_loop().create_future(),
        identity_policy=ApprovalIdentityPolicy.EXACT_REQUESTER,
    )
    assert ApprovalService.actor_can_resolve(pending, actor) is False
    pending.identity_policy = ApprovalIdentityPolicy.AGENT
    assert ApprovalService.actor_can_resolve(pending, actor) is True
    assert request.user_id == "ou_bob"
    assert request.session_id == payload["session_id"]
    assert request.channel_meta == original_meta
    assert payload["meta"] == original_meta


@pytest.mark.asyncio
@pytest.mark.parametrize("topic", [False, True])
async def test_nonapproval_text_preserves_shared_identity(runtime, topic):
    rt = await runtime(share_session_in_group=not topic)
    for index, text in enumerate(
        ["hello", "please /approve pending", "/approval-help", "/stop"],
    ):
        await rt.dispatch(
            sdk_event(
                f"ordinary-{index}",
                sender="ou_bob",
                group=True,
                thread="omt-topic" if topic else "",
                content={"text": text},
            ),
        )
        payload = rt.queued[-1]
        request = rt.channel.build_agent_request_from_native(payload)
        assert request.user_id == ("thread:mt-topic" if topic else "group")
        assert request.session_id == payload["session_id"]
        assert request.channel_meta == payload["meta"]


@pytest.mark.asyncio
@pytest.mark.parametrize("operator", [None, ""])
async def test_typed_approval_without_operator_fails_closed(runtime, operator):
    rt = await runtime(share_session_in_group=True)
    await rt.dispatch(
        sdk_event(
            "missing-operator",
            group=True,
            content={"text": "/approve pending"},
        ),
    )
    payload = rt.queued[0]
    if operator is None:
        payload.pop("acl_sender_id")
    else:
        payload["acl_sender_id"] = operator
    with pytest.raises(ValueError):
        rt.channel.build_agent_request_from_native(payload)
