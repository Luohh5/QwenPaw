# -*- coding: utf-8 -*-
# pylint: disable=protected-access,redefined-outer-name
"""DingTalk public behavior through real SDK models and network boundaries."""

import asyncio
import importlib
import json
import threading
from types import SimpleNamespace as NS

import aiohttp
import dingtalk_stream
import pytest
import dingtalk_boundary
from dingtalk_boundary import eventually
from qwenpaw.schemas import (
    TextContent,
    ImageContent,
    FileContent,
    RefusalContent,
)
from qwenpaw.exceptions import ChannelError

runtime = dingtalk_boundary.runtime

WEBHOOK = "https://oapi.dingtalk.com/robot/sendBySession?session=old-session"
META = {
    "conversation_id": "cid-conversation",
    "conversation_type": "group",
    "is_group": True,
    "sender_staff_id": "staff-one",
    "sender_dingtalk_id": "ding-one",
    "_api_send": True,
}


def calls(runtime, name):
    return [item for item in runtime.sdk if item[0] == name]


def uncertain_webhook_response(runtime, failure):
    if failure == "timeout":
        runtime.webhook_error = TimeoutError(
            "Remote may have accepted message",
        )
    elif failure == "payload":
        # POST has returned its response; body consumption fails afterward.
        runtime.webhook_body_error = aiohttp.ClientPayloadError(
            "Response payload is not completed",
        )
    elif failure == "json":
        runtime.webhook_raw_text = '{"errcode":'
    else:
        runtime.webhook_raw_text = ""


async def started(runtime, **settings):
    channel = runtime.channel(**settings)
    await channel.start()
    await eventually(
        lambda: runtime.sockets and runtime.sockets[-1].receiving.is_set(),
    )
    return channel


def test_native_request_keeps_original_identity_and_legacy_session(runtime):
    channel = runtime.channel()
    payload = {
        "sender_id": "Alice#1234",
        "content_parts": [TextContent(type="text", text="hi")],
        "session_webhook": WEBHOOK,
        "meta": dict(META),
    }
    request = channel.build_agent_request_from_native(payload)
    assert request.user_id == "Alice#1234"
    assert request.channel == "dingtalk"
    assert request.channel_meta["sender_staff_id"] == "staff-one"
    assert request.channel_meta["sender_dingtalk_id"] == "ding-one"
    assert request.channel_meta["session_webhook"] == WEBHOOK
    assert request.input[0].content[0].text == "hi"
    assert (
        channel.get_to_handle_from_request(request)
        == f"dingtalk:sw:Alice#1234_{request.session_id}"
    )
    assert channel.resolve_session_id("one", {}) == "dingtalk:one"


def test_shared_groups_keep_distinct_conversations_and_dm_identity(runtime):
    channel = runtime.channel(share_session_in_group=True)

    def payload(cid, sender="Alice", group=True):
        return {
            "sender_id": sender,
            "content_parts": [TextContent(type="text", text="hi")],
            "meta": {"conversation_id": cid, "is_group": group},
        }

    first = channel.build_agent_request_from_native(
        payload("prefixA_same_suffix"),
    )
    other = channel.build_agent_request_from_native(
        payload("prefixB_same_suffix"),
    )
    assert first.user_id == other.user_id == "group"
    assert first.session_id != other.session_id
    assert "_" not in first.session_id
    assert channel.get_debounce_key(
        payload("room", "Alice"),
    ) == channel.get_debounce_key(payload("room", "Bob"))
    assert (
        channel.build_agent_request_from_native(
            payload("dm-room", group=False),
        ).user_id
        == "Alice"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["meta", "url", "old-store", "new-store"])
async def test_webhook_send_reads_legacy_routes_without_openapi(
    runtime,
    source,
):
    channel = runtime.channel()
    handle, meta = "dingtalk:sw:legacy", {"_api_send": True}
    if source.endswith("store"):
        channel._workspace_dir.mkdir(parents=True)
        entry = (
            WEBHOOK
            if source == "old-store"
            else {
                "webhook": WEBHOOK,
                "conversation_id": "cid-conversation",
                "conversation_type": "group",
            }
        )
        (channel._workspace_dir / "dingtalk_session_webhooks.json").write_text(
            json.dumps({handle: entry}),
        )
    elif source == "meta":
        meta["sessionWebhook"] = WEBHOOK
    else:
        handle = WEBHOOK
    await channel.start()
    await channel.send(handle, "hello", meta)
    assert len(runtime.http) == 1
    assert runtime.http[0][0] == WEBHOOK
    assert runtime.http[0][1]["json"]["markdown"]["text"].strip() == "hello"
    assert not calls(runtime, "group") and not calls(runtime, "dm")


@pytest.mark.asyncio
@pytest.mark.parametrize("group,method", [(True, "group"), (False, "dm")])
async def test_proactive_openapi_routes_group_and_staff_identity(
    runtime,
    group,
    method,
):
    channel = await started(runtime)
    meta = {
        **META,
        "is_group": group,
        "conversation_type": "group" if group else "dm",
    }
    await channel.send("dingtalk:sw:missing", "hello", meta)
    _, request, headers, _ = calls(runtime, method)[0]
    assert request.robot_code == "client-one"
    assert request.msg_key == "sampleMarkdown"
    assert json.loads(request.msg_param)["text"].strip() == "hello"
    assert headers.x_acs_dingtalk_access_token == "api-token"
    if group:
        assert request.open_conversation_id == "cid-conversation"
    else:
        assert request.user_ids == ["staff-one"]
    assert len(calls(runtime, method)) == 1


@pytest.mark.asyncio
async def test_webhook_known_rejection_falls_back_once(runtime):
    channel = await started(runtime)
    runtime.webhook_response = {"errcode": 310000, "errmsg": "session expired"}
    await channel.send(WEBHOOK, "hello", META)
    assert len(runtime.http) == 1
    assert len(calls(runtime, "group")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("parts", [False, True])
@pytest.mark.parametrize("failure", ["timeout", "payload", "json", "empty"])
async def test_ambiguous_webhook_response_never_resends_through_openapi(
    runtime,
    parts,
    failure,
):
    channel = await started(runtime)
    uncertain_webhook_response(runtime, failure)
    with pytest.raises(
        (
            ChannelError,
            TimeoutError,
            aiohttp.ClientPayloadError,
            json.JSONDecodeError,
        ),
    ):
        if parts:
            await channel.send_content_parts(
                WEBHOOK,
                [TextContent(type="text", text="hello")],
                {**META, "session_webhook": WEBHOOK},
            )
        else:
            await channel.send(WEBHOOK, "hello", META)
    assert len(runtime.http) == 1
    assert not calls(runtime, "group") and not calls(runtime, "dm")


@pytest.mark.asyncio
async def test_strict_send_without_target_reports_failure(runtime):
    channel = await started(runtime)
    with pytest.raises(ChannelError):
        await channel.send("dingtalk:sw:unknown", "hello", {"_api_send": True})
    assert not runtime.http and not calls(runtime, "group")


@pytest.mark.asyncio
async def test_content_parts_preserve_refusal_prefix_and_skip_empty(runtime):
    channel = await started(runtime)
    await channel.send_content_parts(
        WEBHOOK,
        [TextContent(type="text", text="   ")],
        META,
    )
    assert not runtime.http
    await channel.send_content_parts(
        WEBHOOK,
        [
            TextContent(type="text", text="hello"),
            RefusalContent(type="refusal", refusal="cannot comply"),
        ],
        {**META, "bot_prefix": "[bot]"},
    )
    assert (
        runtime.http[0][1]["json"]["markdown"]["text"].strip()
        == "[bot]  hello\ncannot comply"
    )


@pytest.mark.asyncio
async def test_file_content_uploads_and_uses_returned_media_id(
    runtime,
    tmp_path,
):
    channel = await started(runtime, endpoint="https://media-gateway.test")
    source = tmp_path / "report.txt"
    source.write_text("original bytes")
    await channel.send_content_parts(
        WEBHOOK,
        [
            FileContent(
                type="file",
                file_url=source.as_uri(),
                filename="report.txt",
            ),
        ],
        META,
    )
    assert len(runtime.http) == 2
    assert (
        runtime.http[0][0] == "https://media-gateway.test/media/upload"
        "?access_token=api-token&type=file"
    )
    assert runtime.http[1][1]["json"] == {
        "msgtype": "file",
        "file": {
            "mediaId": "@media-one",
            "fileType": "txt",
            "fileName": "report.txt",
        },
    }
    assert source.read_text() == "original bytes"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "payload", "json", "empty"])
async def test_inline_image_unknown_response_does_not_send_file_fallback(
    runtime,
    failure,
):
    channel = await started(runtime)
    uncertain_webhook_response(runtime, failure)
    with pytest.raises(
        (
            ChannelError,
            TimeoutError,
            aiohttp.ClientPayloadError,
            json.JSONDecodeError,
        ),
    ):
        await channel.send_content_parts(
            WEBHOOK,
            [
                ImageContent(
                    type="image",
                    image_url="data:image/png;base64,aGVsbG8=",
                ),
            ],
            {**META, "session_webhook": WEBHOOK},
        )
    assert len(runtime.http) == 2  # upload plus one attempted delivery
    assert runtime.http[-1][1]["json"]["msgtype"] == "markdown"
    assert not calls(runtime, "group")


@pytest.mark.asyncio
async def test_streaming_card_creation_delta_finalization_and_pending_store(
    runtime,
):
    channel = await started(
        runtime,
        message_type="card",
        card_template_id="template",
        streaming_enabled=True,
        at_sender_on_reply=True,
    )
    request = NS()
    meta = dict(META)
    await channel.on_streaming_start(request, WEBHOOK, None, meta, "reasoning")
    assert len(calls(runtime, "create")) == len(calls(runtime, "deliver")) == 1
    create = calls(runtime, "create")[0][1]
    delivery = calls(runtime, "deliver")[0][1]
    assert create.card_template_id == "template"
    assert create.card_at_user_ids == ["staff-one"]
    assert delivery.open_space_id == "dtv1.card//IM_GROUP.cid-conversation"
    assert delivery.out_track_id == create.out_track_id
    pending = json.loads(
        (channel._workspace_dir / "dingtalk-active-cards.json").read_text(),
    )["pending_cards"]
    assert pending[0]["card_instance_id"] == create.out_track_id
    await channel.on_streaming_delta(
        request,
        WEBHOOK,
        None,
        meta,
        "reasoning",
        "answer",
    )
    count = len(calls(runtime, "update"))
    await channel.on_streaming_delta(
        request,
        WEBHOOK,
        None,
        meta,
        "reasoning",
        "answer",
    )
    assert len(calls(runtime, "update")) == count
    await channel.on_streaming_end(
        request,
        WEBHOOK,
        None,
        meta,
        "reasoning",
        "answer",
    )
    update = calls(runtime, "update")[-1][1]
    assert update.is_finalize and update.is_full
    assert update.out_track_id == create.out_track_id
    assert "answer" in update.content and "💭" in update.content
    assert (
        json.loads(
            (
                channel._workspace_dir / "dingtalk-active-cards.json"
            ).read_text(),
        )["pending_cards"]
        == []
    )


@pytest.mark.asyncio
async def test_old_pending_card_recovers_same_card_without_creating_new_one(
    runtime,
):
    channel = runtime.channel(message_type="card", card_template_id="template")
    channel._workspace_dir.mkdir(parents=True)
    path = channel._workspace_dir / "dingtalk-active-cards.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "pending_cards": [
                    {
                        "account_id": "default",
                        "card_instance_id": "legacy-card",
                        "conversation_id": "cid-legacy",
                        "created_at": 1,
                        "last_updated": 1,
                        "state": "2",
                    },
                ],
            },
        ),
    )
    await channel.start()
    assert not calls(runtime, "create") and not calls(runtime, "deliver")
    update = calls(runtime, "update")[0][1]
    assert update.out_track_id == "legacy-card"
    assert update.is_finalize
    assert json.loads(path.read_text())["pending_cards"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "tea", "dara"])
async def test_timeout_creating_card_is_not_retried_by_later_stream_hooks(
    runtime,
    failure,
):
    channel = await started(
        runtime,
        message_type="card",
        card_template_id="template",
        streaming_enabled=True,
    )
    from Tea.exceptions import UnretryableException
    from Tea.request import TeaRequest
    from darabonba.exceptions import RetryError

    error = TimeoutError("Card may already exist")
    if failure == "tea":
        error = UnretryableException(TeaRequest(), error)
    elif failure == "dara":
        error = RetryError("read timeout after request was sent")
    runtime.sdk_error["create"] = error
    request = NS(
        channel_meta=dict(META),
        user_id="Alice",
        session_id="session",
    )
    # This BaseChannel hook is where the original channel pre-creates cards.
    with pytest.raises((ChannelError, TimeoutError, type(error))):
        await channel._before_consume_process(request)
    meta = dict(META)
    for stream_type in ("reasoning", "message"):
        with pytest.raises((ChannelError, TimeoutError, type(error))):
            await channel.on_streaming_start(
                request,
                WEBHOOK,
                None,
                meta,
                stream_type,
            )
    assert len(calls(runtime, "create")) == 1
    assert not calls(runtime, "deliver")


@pytest.mark.asyncio
async def test_workspace_endpoints_timeouts_and_sessions_stay_independent(
    runtime,
):
    import dingtalk_stream.stream as sdk_stream
    import dingtalk_stream.chatbot as sdk_chatbot

    original_url = sdk_stream.DingTalkStreamClient.OPEN_CONNECTION_API
    original_requests = sdk_stream.requests
    original_chat_requests = sdk_chatbot.requests
    first = runtime.channel("first", endpoint="https://one.test")
    second = runtime.channel("second", endpoint="https://two.test")
    await first.start()
    await second.start()
    await eventually(lambda: len(runtime.sockets) == 2)
    assert isinstance(first._client, dingtalk_stream.DingTalkStreamClient)
    assert first._http is not second._http
    assert first._stream_event_loop is not second._stream_event_loop
    assert {url for url, _ in runtime.sync_http} == {
        "https://one.test/v1.0/gateway/connections/open",
        "https://two.test/v1.0/gateway/connections/open",
    }
    assert all(
        options["timeout"] == (10, 30) for _, options in runtime.sync_http
    )
    assert sdk_stream.DingTalkStreamClient.OPEN_CONNECTION_API == original_url
    assert sdk_stream.requests is original_requests
    assert sdk_chatbot.requests is original_chat_requests
    first_http, second_http = first._http, second._http
    await first.stop()
    assert first_http.closed and not second_http.closed
    assert second._stream_thread.is_alive()
    await second.send(WEBHOOK, "still alive", META)
    assert (
        runtime.http[-1][1]["json"]["markdown"]["text"].strip()
        == "still alive"
    )


@pytest.mark.asyncio
async def test_stop_closes_worker_loop_http_and_debounce_without_deleting_data(
    runtime,
):
    channel = await started(runtime)
    http, thread, loop = (
        channel._http,
        channel._stream_thread,
        channel._stream_event_loop,
    )
    data = channel._workspace_dir / "keep.json"
    data.write_text('{"setting": true}')
    cancelled = asyncio.Event()

    async def timer():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    task = asyncio.create_task(timer())
    await asyncio.sleep(0)
    channel._debounce_timers["pending"] = task
    channel._debounce_pending["pending"] = ["message"]
    unrelated = asyncio.create_task(asyncio.Event().wait())
    try:
        await channel.stop()
        assert not thread.is_alive()
        assert loop.is_closed()
        assert http.closed
        assert cancelled.is_set() and task.done()
        assert not unrelated.done()
        assert data.read_text() == '{"setting": true}'
        assert not channel._debounce_timers and not channel._debounce_pending
        await channel.stop()
        assert (await channel.health_check())["status"] == "unhealthy"
    finally:
        unrelated.cancel()
        await asyncio.gather(unrelated, return_exceptions=True)


@pytest.mark.asyncio
async def test_repeated_start_preserves_resources_and_restart_is_fresh(
    runtime,
):
    channel = await started(runtime)
    first = channel._client, channel._stream_thread, channel._http
    await channel.start()
    assert (channel._client, channel._stream_thread, channel._http) == first
    await channel.stop()
    await channel.start()
    await eventually(lambda: len(runtime.sockets) == 2)
    assert channel._client is not first[0]
    assert channel._stream_thread is not first[1]
    assert channel._http is not first[2]


@pytest.mark.asyncio
async def test_blocked_worker_stop_is_responsive_fails_and_keeps_references(
    runtime,
    monkeypatch,
):
    platform = importlib.import_module("plugin_dingtalk.platform")
    monkeypatch.setattr(platform, "_STOP_TIMEOUT_SECONDS", 0.08)
    runtime.connect_gate = threading.Event()
    channel = runtime.channel()
    await channel.start()
    await eventually(runtime.connect_entered.is_set)
    client, thread, http = (
        channel._client,
        channel._stream_thread,
        channel._http,
    )
    ticks = []

    async def tick():
        for _ in range(5):
            await asyncio.sleep(0.005)
            ticks.append(1)

    heartbeat = asyncio.create_task(tick())
    with pytest.raises(RuntimeError):
        await channel.stop()
    assert len(ticks) == 5, "stop blocked the host event loop"
    assert thread.is_alive()
    assert channel._client is client and channel._stream_thread is thread
    assert channel._http is http and not http.closed
    with pytest.raises(RuntimeError):
        await channel.start()
    assert channel._client is client
    runtime.connect_gate.set()
    await eventually(lambda: not thread.is_alive())
    await channel.stop()
    await heartbeat
    assert http.closed


@pytest.mark.asyncio
async def test_cancelled_stop_waiter_does_not_cancel_resource_cleanup(runtime):
    channel = await started(runtime)
    socket = runtime.sockets[-1]
    socket.close_gate = threading.Event()
    thread, http = channel._stream_thread, channel._http
    stopping = asyncio.create_task(channel.stop())
    await eventually(socket.close_started.is_set)
    stopping.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stopping
    assert thread.is_alive() and not http.closed
    socket.close_gate.set()
    await eventually(lambda: not thread.is_alive() and http.closed)
    await channel.stop()


@pytest.mark.asyncio
async def test_remote_disconnect_reconnects_and_stop_quiesces_reconnect(
    runtime,
):
    channel = await started(runtime)
    socket = runtime.sockets[0]
    socket.loop.call_soon_threadsafe(socket.inbox.put_nowait, None)
    await eventually(lambda: len(runtime.sockets) >= 2)
    assert runtime.sockets[1].loop is channel._stream_event_loop
    thread = channel._stream_thread
    await channel.stop()
    count = len(runtime.sync_http)
    await asyncio.sleep(0.02)
    assert not thread.is_alive()
    assert len(runtime.sync_http) == count


@pytest.mark.asyncio
async def test_stop_finalizes_live_card_before_closing_http(runtime):
    channel = await started(
        runtime,
        message_type="card",
        card_template_id="template",
        streaming_enabled=True,
    )
    meta = dict(META)
    await channel.on_streaming_start(NS(), WEBHOOK, None, meta, "reasoning")
    card_id = calls(runtime, "create")[0][1].out_track_id
    await channel.stop()
    update = calls(runtime, "update")[-1][1]
    assert update.out_track_id == card_id and update.is_finalize
    assert (
        json.loads(
            (
                channel._workspace_dir / "dingtalk-active-cards.json"
            ).read_text(),
        )["pending_cards"]
        == []
    )


def wire_message(mid, group=True, mentioned=True):
    return {
        "specVersion": "1.0",
        "type": "CALLBACK",
        "headers": {
            "appId": "client-one",
            "messageId": mid,
            "time": "1700000000000",
            "topic": dingtalk_stream.ChatbotMessage.TOPIC,
            "contentType": "application/json",
        },
        "data": json.dumps(
            {
                "msgId": mid,
                "msgtype": "text",
                "text": {"content": "hello"},
                "senderId": "ding1234",
                "senderNick": "Alice",
                "senderStaffId": "staff-one",
                "conversationId": "cid-original",
                "conversationType": "2" if group else "1",
                "sessionWebhook": WEBHOOK,
                "sessionWebhookExpiredTime": 9999999999999,
                "isInAtList": mentioned,
                "robotCode": "client-one",
                "chatbotUserId": "robot-user",
            },
        ),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("group", [True, False])
async def test_real_sdk_callback_ack_and_identity_preserve_native_message(
    runtime,
    group,
):
    channel = runtime.channel()
    received = []
    channel.set_enqueue(received.append)
    await channel.start()
    await eventually(
        lambda: runtime.sockets and runtime.sockets[0].receiving.is_set(),
    )
    socket = runtime.sockets[0]
    socket.receive(wire_message("message-one", group=group))
    await eventually(lambda: len(received) == 1 and socket.sent)
    native = received[0]
    assert native["sender_id"] == "Alice#1234"
    assert native["acl_sender_id"] == "ding1234"
    assert native["content_parts"][0].text == "hello"
    assert native["session_webhook"] == WEBHOOK
    assert native["meta"]["sender_staff_id"] == "staff-one"
    assert native["meta"]["sender_dingtalk_id"] == "ding1234"
    assert native["meta"]["conversation_type"] == ("group" if group else "dm")
    assert socket.sent[0]["code"] == 200
    socket.receive(wire_message("message-one", group=group))
    await eventually(lambda: len(socket.sent) == 2)
    assert len(received) == 1


@pytest.mark.asyncio
async def test_group_mention_filter_acks_without_dispatch_and_dm_is_allowed(
    runtime,
):
    channel = runtime.channel(require_mention=True)
    received = []
    channel.set_enqueue(received.append)
    await channel.start()
    await eventually(
        lambda: runtime.sockets and runtime.sockets[0].receiving.is_set(),
    )
    socket = runtime.sockets[0]
    socket.receive(wire_message("not-mentioned", mentioned=False))
    await eventually(lambda: socket.sent)
    assert not received
    socket.receive(wire_message("dm", group=False, mentioned=False))
    await eventually(lambda: len(received) == 1)
    assert received[0]["message_id"] == "dm"


@pytest.mark.asyncio
async def test_cancelled_send_remains_owned_until_network_finishes(
    runtime,
    monkeypatch,
):
    channel = await started(runtime)
    platform = importlib.import_module("plugin_dingtalk.platform")
    monkeypatch.setattr(platform, "_STOP_TIMEOUT_SECONDS", 0.8)
    gate = runtime.sdk_gate["group"] = asyncio.Event()
    sending = asyncio.create_task(
        channel.send("dingtalk:sw:missing", "one message", META),
    )
    await eventually(lambda: "group" in runtime.sdk_entered)
    sending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await sending
    http, client = channel._http, channel._client
    with pytest.raises(RuntimeError):
        await channel.stop()
    assert channel._http is http and not http.closed
    assert channel._client is client
    with pytest.raises(RuntimeError):
        await channel.start()
    assert len(calls(runtime, "group")) == 1
    gate.set()
    await channel.stop()
    assert http.closed
    assert len(calls(runtime, "group")) == 1


@pytest.mark.asyncio
async def test_failed_stop_finalization_preserves_card_for_restart(runtime):
    channel = await started(
        runtime,
        message_type="card",
        card_template_id="template",
        streaming_enabled=True,
    )
    await channel.on_streaming_start(
        NS(),
        WEBHOOK,
        None,
        dict(META),
        "reasoning",
    )
    card_id = calls(runtime, "create")[0][1].out_track_id
    runtime.sdk_error["update"] = TimeoutError("final acknowledgement lost")
    await channel.stop()
    path = channel._workspace_dir / "dingtalk-active-cards.json"
    assert (
        json.loads(path.read_text())["pending_cards"][0]["card_instance_id"]
        == card_id
    )
    runtime.sdk_error.clear()
    await channel.start()
    assert calls(runtime, "update")[-1][1].out_track_id == card_id
    assert calls(runtime, "update")[-1][1].is_finalize
    assert len(calls(runtime, "create")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("shared", [False, True])
@pytest.mark.parametrize(
    "command",
    [
        "/approval approve pending",
        " /APPROVE pending ",
        "/deny pending",
        "/authorize-dingtalk-test approve pending",
    ],
)
async def test_typed_approval_uses_authenticated_operator_not_display_or_group(
    runtime,
    monkeypatch,
    shared,
    command,
):
    from qwenpaw.runtime.commands import control
    from qwenpaw.runtime.commands.control.approval_handler import (
        ApprovalCommandHandler,
    )
    from qwenpaw.runtime.commands.control.base import ControlContext
    from qwenpaw.app.approvals.service import (
        ApprovalIdentityPolicy,
        ApprovalService,
        PendingApproval,
    )

    monkeypatch.setattr(
        control,
        "_COMMAND_REGISTRY",
        dict(control._COMMAND_REGISTRY),
    )
    alias = ApprovalCommandHandler()
    alias.command_name = "/authorize-dingtalk-test"
    control.register_command(alias)
    channel = runtime.channel(share_session_in_group=shared)
    payload = {
        "sender_id": "Alice#1234",
        "acl_sender_id": "real-operator-1234",
        "content_parts": [TextContent(type="text", text=command)],
        "meta": dict(META),
    }
    request = channel.build_agent_request_from_native(payload)
    actor = ApprovalCommandHandler._actor(
        ControlContext(
            workspace=None,
            payload=request,
            channel=channel,
            session_id=request.session_id,
            user_id=request.user_id,
            agent_id="default",
            args={},
        ),
    )
    pending = PendingApproval(
        request_id="pending",
        session_id=request.session_id,
        root_session_id=request.session_id,
        owner_agent_id="default",
        user_id="group" if shared else "Alice#1234",
        channel="dingtalk",
        agent_id="default",
        tool_name="reme:save",
        created_at=0,
        future=asyncio.get_running_loop().create_future(),
        identity_policy=ApprovalIdentityPolicy.EXACT_REQUESTER,
    )
    assert not ApprovalService.actor_can_resolve(pending, actor)
    pending.identity_policy = ApprovalIdentityPolicy.AGENT
    assert ApprovalService.actor_can_resolve(pending, actor)
    assert request.user_id == "real-operator-1234"
    assert request.channel_meta == META
    assert payload["meta"] == META


@pytest.mark.parametrize("operator", [None, "", "   "])
def test_typed_approval_missing_authenticated_identity_fails_closed(
    runtime,
    operator,
):
    channel = runtime.channel(share_session_in_group=True)
    payload = {
        "sender_id": "Alice#1234",
        "content_parts": [TextContent(type="text", text="/approve pending")],
        "meta": dict(META),
    }
    if operator is not None:
        payload["acl_sender_id"] = operator
    with pytest.raises(ValueError):
        channel.build_agent_request_from_native(payload)


@pytest.mark.parametrize("shared", [False, True])
@pytest.mark.parametrize(
    "text",
    ["hello", "please /approve pending", "/approval-help", "/stop"],
)
def test_nonapproval_messages_keep_legacy_conversation_identity(
    runtime,
    shared,
    text,
):
    channel = runtime.channel(share_session_in_group=shared)
    request = channel.build_agent_request_from_native(
        {
            "sender_id": "Alice#1234",
            "acl_sender_id": "real-operator-1234",
            "content_parts": [TextContent(type="text", text=text)],
            "meta": dict(META),
        },
    )
    assert request.user_id == ("group" if shared else "Alice#1234")


@pytest.mark.asyncio
async def test_connecting_is_unhealthy_until_actual_socket_exists(runtime):
    runtime.connect_gate = threading.Event()
    channel = runtime.channel()
    await channel.start()
    await eventually(runtime.connect_entered.is_set)
    assert (await channel.health_check())["status"] == "unhealthy"
    runtime.connect_gate.set()
    await eventually(
        lambda: runtime.sockets and runtime.sockets[0].receiving.is_set(),
    )
    async with asyncio.timeout(2):
        while (await channel.health_check())["status"] != "healthy":
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_partial_initialization_closes_real_http_before_retry(
    runtime,
    monkeypatch,
):
    from alibabacloud_dingtalk.robot_1_0.client import Client

    channel = runtime.channel()
    original_init, original_close = (
        Client.__init__,
        aiohttp.ClientSession.close,
    )
    closed = []

    def fail_init(self, *_args, **_kwargs):
        raise RuntimeError("client construction failed")

    async def track_close(session):
        await original_close(session)
        closed.append(session)

    monkeypatch.setattr(Client, "__init__", fail_init)
    monkeypatch.setattr(aiohttp.ClientSession, "close", track_close)
    with pytest.raises(RuntimeError, match="client construction failed"):
        await channel.start()
    assert len(closed) == 1 and closed[0].closed
    assert channel._client is None and channel._http is None
    assert not runtime.sockets
    assert (await channel.health_check())["status"] == "unhealthy"
    monkeypatch.setattr(Client, "__init__", original_init)
    await channel.start()
    await eventually(lambda: runtime.sockets)


@pytest.mark.asyncio
async def test_http_close_failure_keeps_session_for_explicit_cleanup_retry(
    runtime,
    monkeypatch,
):
    channel = await started(runtime)
    original_close = aiohttp.ClientSession.close
    http = channel._http

    async def fail_close(session):
        if session is http:
            raise RuntimeError("HTTP close failed")
        await original_close(session)

    monkeypatch.setattr(aiohttp.ClientSession, "close", fail_close)
    with pytest.raises(RuntimeError, match="HTTP close failed"):
        await channel.stop()
    assert channel._http is http and not http.closed
    with pytest.raises(RuntimeError):
        await channel.start()
    monkeypatch.setattr(aiohttp.ClientSession, "close", original_close)
    await channel.stop()
    assert http.closed


@pytest.mark.asyncio
async def test_gateway_failure_is_unhealthy_then_recovers_without_new_channel(
    runtime,
):
    runtime.sync_failures = 1
    channel = runtime.channel()
    await channel.start()
    await eventually(lambda: len(runtime.sync_http) == 1)
    async with asyncio.timeout(2):
        while True:
            health = await channel.health_check()
            if health.get("error"):
                break
            await asyncio.sleep(0.005)
    assert health["status"] == "unhealthy"
    assert health["state"] == "reconnecting"
    assert "ConnectionError" in health["error"]
    await eventually(
        lambda: runtime.sockets and runtime.sockets[0].receiving.is_set(),
        timeout=12,
    )
    assert (await channel.health_check())["status"] == "healthy"
    assert len(runtime.sync_http) == 2


@pytest.mark.asyncio
async def test_inbound_picture_downloads_into_workspace_and_preserves_text(
    runtime,
):
    from pathlib import Path

    channel = runtime.channel()
    received = []
    channel.set_enqueue(received.append)
    await channel.start()
    await eventually(
        lambda: runtime.sockets and runtime.sockets[0].receiving.is_set(),
    )
    wire = wire_message("picture-one")
    content = json.loads(wire["data"])
    content.update(
        {
            "msgtype": "richText",
            "content": {
                "richText": [
                    {"text": "hello"},
                    {"type": "picture", "pictureDownloadCode": "download-one"},
                ],
            },
        },
    )
    wire["data"] = json.dumps(content)
    runtime.sockets[0].receive(wire)
    await eventually(lambda: received)
    parts = received[0]["content_parts"]
    assert parts[0].text == "hello"
    images = [part for part in parts if part.type == "image"]
    assert len(images) == 1
    path = Path(images[0].image_url.removeprefix("file://"))
    assert path.parent == channel._workspace_dir / "media"
    assert path.suffix == ".png"
    assert path.read_bytes() == b"\x89PNG\r\n\x1a\noriginal image"
    assert calls(runtime, "download")[0][1].download_code == "download-one"
    await channel.stop()
    assert path.exists()


@pytest.mark.asyncio
async def test_stale_handler_cannot_reserve_current_generation_message_id(
    runtime,
):
    channel = await started(runtime)
    received = []
    channel.set_enqueue(received.append)
    old_handler = channel._client.callback_handler_map[
        dingtalk_stream.ChatbotMessage.TOPIC
    ]
    await channel.stop()
    await channel.start()
    await eventually(lambda: len(runtime.sockets) == 2)
    message = dingtalk_stream.CallbackMessage.from_dict(
        wire_message("reused-id"),
    )
    await old_handler.process(message)
    await asyncio.sleep(0)
    assert not received
    socket = runtime.sockets[-1]
    socket.receive(wire_message("reused-id"))
    await eventually(lambda: socket.sent)
    assert len(received) == 1
    assert received[0]["message_id"] == "reused-id"
