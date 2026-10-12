# -*- coding: utf-8 -*-
# pylint: disable=protected-access,redefined-outer-name
"""Independent lifecycle contracts for the standalone Feishu plugin."""

import asyncio
import threading

import pytest

import feishu_suite
import lifecycle_boundary
from lifecycle_boundary import eventually, wait_healthy

runtime = feishu_suite.runtime
network_runtime = lifecycle_boundary.network_runtime


@pytest.mark.asyncio
async def test_stop_failure_keeps_live_handle(runtime, monkeypatch):
    """A live worker cannot be forgotten or replaced after a failed stop."""
    rt = await runtime()
    release = threading.Event()
    thread = threading.Thread(target=release.wait, daemon=True)
    thread.start()
    rt.platform._ws_thread = thread
    monkeypatch.setattr(rt.module, "_WS_THREAD_JOIN_TIMEOUT", 0.02)
    try:
        with pytest.raises(RuntimeError):
            await rt.channel.stop()
        assert rt.channel._platform is rt.platform
        assert rt.platform._ws_thread is thread
        assert thread.is_alive()
        assert (await rt.channel.health_check())["status"] == "unhealthy"
        with pytest.raises(RuntimeError):
            await rt.channel.start()
        assert rt.channel._platform is rt.platform
    finally:
        release.set()
        await asyncio.to_thread(thread.join, 1)
        await rt.platform.close()
    await rt.channel.stop()
    assert rt.channel._platform is None


@pytest.mark.asyncio
async def test_old_callback_cannot_enqueue_after_restart(runtime):
    """An earlier connection's callback cannot enter the restarted channel."""
    rt = await runtime()
    old_callback = rt.on_message
    await rt.channel.stop()
    await rt.channel.start()
    await old_callback(
        {
            "message_id": "old-generation",
            "sender_open_id": "ou_alice",
            "chat_id": "oc_room",
            "content_parts": [{"type": "text", "text": "late"}],
        },
    )
    assert rt.queued == []
    rt.sdk.reaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_stop_before_start_and_repeated_lifecycle(network_runtime):
    rt = network_runtime
    channel = rt.channel()
    assert await channel.stop() is None
    assert (await channel.health_check())["status"] == "unhealthy"
    assert rt.http_clients == []
    for _ in range(2):
        await channel.start()
        platform = channel._platform
        thread = platform._ws_thread
        await channel.start()
        assert channel._platform is platform
        assert thread.is_alive()
        assert (await channel.health_check())["status"] == "healthy"
        assert await channel.stop() is None
        assert await channel.stop() is None
        assert not thread.is_alive()
        assert rt.http_clients[-1].is_closed
        assert rt.sockets[-1].closed
    assert rt.connection_attempts == 2


@pytest.mark.asyncio
async def test_authentication_failure_closes_acquired_http(network_runtime):
    rt = network_runtime
    rt.token = lambda _config: ""
    channel = rt.channel()
    with pytest.raises(RuntimeError, match="credential|auth"):
        await channel.start()
    assert rt.connection_attempts == 0
    assert all(client.is_closed for client in rt.http_clients)
    assert channel._platform is None
    assert (await channel.health_check())["status"] == "unhealthy"
    rt.token = lambda _config: "valid"
    await channel.start()
    assert (await channel.health_check())["status"] == "healthy"


@pytest.mark.asyncio
async def test_partial_connection_failure_cleans_socket_thread_and_http(
    network_runtime,
):
    rt = network_runtime
    channel = rt.channel()
    rt.connect_error = ConnectionError("handshake rejected")
    with pytest.raises(RuntimeError, match="connection|WebSocket"):
        await channel.start()
    assert rt.connection_attempts == 1
    assert all(client.is_closed for client in rt.http_clients)
    assert all(
        platform._ws_thread is None or not platform._ws_thread.is_alive()
        for platform in rt.platforms
    )
    assert channel._platform is None
    rt.connect_error = None
    await channel.start()
    assert (await channel.health_check())["status"] == "healthy"


@pytest.mark.asyncio
async def test_two_workspaces_keep_sdk_loop_and_connections_isolated(
    network_runtime,
):
    rt = network_runtime
    sdk_loop = rt.module.lark_ws.loop
    one, two = rt.channel("one"), rt.channel("two")
    await asyncio.gather(one.start(), two.start())
    await eventually(
        lambda: len(rt.sockets) == 2
        and all(s.receiving.is_set() for s in rt.sockets),
    )
    assert rt.module.lark_ws.loop is sdk_loop
    assert all(s.receive_loops == [s.loop] for s in rt.sockets)
    first, second = one._platform, two._platform
    assert first._media_work_dir != second._media_work_dir
    first._processed_ids["one-only"] = None
    first._nickname_cache["ou_alice"] = "One"
    assert "one-only" not in second._processed_ids
    assert "ou_alice" not in second._nickname_cache
    await one.stop()
    assert (await two.health_check())["status"] == "healthy"
    assert second._ws_thread.is_alive()
    assert not second._http_client.is_closed


@pytest.mark.asyncio
async def test_stop_does_not_block_other_coroutines(runtime, monkeypatch):
    rt = await runtime()
    release = threading.Event()
    thread = threading.Thread(target=release.wait, daemon=True)
    thread.start()
    rt.platform._ws_thread = thread
    monkeypatch.setattr(rt.module, "_WS_THREAD_JOIN_TIMEOUT", 0.5)
    progressed = asyncio.Event()

    async def release_from_host_loop():
        await asyncio.sleep(0.01)
        progressed.set()
        release.set()

    helper = asyncio.create_task(release_from_host_loop())
    try:
        await asyncio.wait_for(rt.channel.stop(), 1)
        assert progressed.is_set()
        assert not thread.is_alive()
    finally:
        release.set()
        await helper
        await asyncio.to_thread(thread.join, 1)


@pytest.mark.asyncio
async def test_overlapping_starts_do_not_create_duplicate_resources(
    network_runtime,
):
    rt = network_runtime
    channel = rt.channel()
    await asyncio.gather(channel.start(), channel.start())
    assert rt.connection_attempts == 1
    assert len(rt.http_clients) == 1
    assert (await channel.health_check())["status"] == "healthy"


@pytest.mark.asyncio
async def test_reconnect_closes_previous_connection_before_replacement(
    network_runtime,
):
    rt = network_runtime
    channel = rt.channel()
    await channel.start()
    old_socket = rt.sockets[0]
    old_thread = channel._platform._ws_thread
    old_callback = rt.callbacks[0][0]
    queued = []
    channel.set_enqueue(queued.append)
    old_socket.drop()
    await eventually(lambda: len(rt.sockets) == 2)
    await wait_healthy(channel)
    assert old_socket.closed
    assert not old_thread.is_alive()
    assert (await channel.health_check())["status"] == "healthy"
    await old_callback(
        {
            "message_id": "stale",
            "sender_open_id": "ou_alice",
            "chat_id": "oc_room",
            "content_parts": [{"type": "text", "text": "old"}],
        },
    )
    assert not queued
    await channel.stop()
    assert rt.sockets[-1].closed
    attempts = rt.connection_attempts
    await asyncio.sleep(0.05)
    assert rt.connection_attempts == attempts


@pytest.mark.asyncio
@pytest.mark.parametrize("worker_outlives_cancel", [False, True])
async def test_cancelled_stop_keeps_live_worker_until_retry(
    runtime,
    monkeypatch,
    worker_outlives_cancel,
):
    rt = await runtime()
    release = threading.Event()
    joined = threading.Event()
    join_finished = threading.Event()
    thread = threading.Thread(target=release.wait, daemon=True)
    thread.start()
    real_join = thread.join

    def observed_join(timeout=None):
        joined.set()
        real_join(timeout)
        join_finished.set()

    monkeypatch.setattr(thread, "join", observed_join)
    monkeypatch.setattr(rt.module, "_WS_THREAD_JOIN_TIMEOUT", 0.1)
    rt.platform._ws_thread = thread
    stopping = asyncio.create_task(rt.channel.stop())
    try:
        await eventually(joined.is_set)
        stopping.cancel()
        await asyncio.sleep(0.01)
        assert thread.is_alive()
        assert rt.channel._platform is rt.platform
        assert rt.platform._ws_thread is thread
        with pytest.raises(asyncio.CancelledError):
            await stopping
        if worker_outlives_cancel:
            await eventually(join_finished.is_set)
            assert thread.is_alive()
            assert rt.channel._platform is rt.platform
            assert (await rt.channel.health_check())["status"] == "unhealthy"
            with pytest.raises(RuntimeError):
                await rt.channel.start()
        release.set()
        await asyncio.to_thread(real_join, 1)
        await rt.channel.stop()
        assert not thread.is_alive()
        assert rt.channel._platform is None
    finally:
        release.set()
        await asyncio.to_thread(real_join, 1)
        if not stopping.done():
            await asyncio.gather(stopping, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelled_auth_wait_does_not_orphan_token_worker(
    network_runtime,
):
    rt = network_runtime
    entered, release, finished = (threading.Event() for _ in range(3))

    def token(_config):
        entered.set()
        release.wait(2)
        finished.set()
        return "valid"

    rt.token = token
    channel = rt.channel()
    starting = asyncio.create_task(channel.start())
    try:
        await eventually(entered.is_set)
        platform = channel._platform
        starting.cancel()
        await asyncio.sleep(0.02)
        assert not finished.is_set()
        assert channel._platform is platform
        assert not rt.http_clients[0].is_closed
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await starting
        await channel.stop()
        assert finished.is_set()
        assert all(client.is_closed for client in rt.http_clients)
        assert rt.connection_attempts == 0
    finally:
        release.set()
        await asyncio.gather(starting, return_exceptions=True)


@pytest.mark.asyncio
async def test_stop_waits_for_real_callback_finally_before_closing_http(
    network_runtime,
    monkeypatch,
):
    from feishu_suite import sdk_event

    rt = network_runtime
    channel = rt.channel()
    await channel.start()
    platform = channel._platform
    http = platform._http_client
    entered, work_release, cleanup_entered, cleanup_release, finished = (
        asyncio.Event() for _ in range(5)
    )
    real_request = http.__class__.request

    async def delayed_request(client, method, url, **kwargs):
        if "reactions" not in str(url):
            return await real_request(client, method, url, **kwargs)
        entered.set()
        try:
            await work_release.wait()
        finally:
            cleanup_entered.set()
            await cleanup_release.wait()
            assert not http.is_closed
            finished.set()
        return await real_request(client, method, url, **kwargs)

    monkeypatch.setattr(http.__class__, "request", delayed_request)
    queued = []
    channel.set_enqueue(queued.append)
    unrelated = asyncio.create_task(
        asyncio.Event().wait(),
        name="unrelated-agent-work",
    )
    stopping = None
    try:
        platform._on_message_sync(
            sdk_event("pending-callback"),
            rt.callbacks[-1][0],
        )
        await asyncio.wait_for(entered.wait(), 1)
        stopping = asyncio.create_task(channel.stop())
        await asyncio.sleep(0.01)
        work_release.set()
        await asyncio.wait_for(cleanup_entered.wait(), 1)
        await asyncio.sleep(0.01)
        assert not stopping.done()
        assert not finished.is_set()
        assert not http.is_closed
        assert not unrelated.done()
        # Closed-generation ingress must be rejected while cleanup is pending.
        platform._on_message_sync(
            sdk_event("late-ingress"),
            rt.callbacks[-1][0],
        )
        cleanup_release.set()
        await asyncio.wait_for(stopping, 2)
        assert finished.is_set()
        assert http.is_closed
        assert [item["meta"]["feishu_message_id"] for item in queued] == [
            "pending-callback",
        ]
        assert not unrelated.done()
    finally:
        work_release.set()
        cleanup_release.set()
        unrelated.cancel()
        await asyncio.gather(unrelated, return_exceptions=True)
        if stopping is not None:
            await asyncio.gather(stopping, return_exceptions=True)


@pytest.mark.asyncio
async def test_missing_dependency_names_package_without_install(
    load_plugin,
    tmp_path,
    monkeypatch,
):
    import builtins
    import subprocess
    from feishu_suite import make_channel

    channel = make_channel(load_plugin, tmp_path)
    original_import = builtins.__import__

    def import_without_sdk(name, *args, **kwargs):
        if name == "lark_oapi" or name.startswith("lark_oapi."):
            raise ModuleNotFoundError(
                "No module named 'lark_oapi'",
                name="lark_oapi",
            )
        return original_import(name, *args, **kwargs)

    def forbidden_install(*_args, **_kwargs):
        raise AssertionError("A channel must not install its own dependencies")

    monkeypatch.setattr(builtins, "__import__", import_without_sdk)
    monkeypatch.setattr(subprocess, "Popen", forbidden_install)
    with pytest.raises((ImportError, RuntimeError), match="lark[-_]oapi"):
        await channel.start()
    assert (await channel.health_check())["status"] == "unhealthy"
    await channel.stop()


@pytest.mark.asyncio
async def test_connection_timeout_closes_all_acquired_resources(
    network_runtime,
    monkeypatch,
):
    rt = network_runtime
    channel = rt.channel()
    rt.connect_gate = threading.Event()
    monkeypatch.setattr(rt.module, "_WS_CONNECT_TIMEOUT", 0.04)
    with pytest.raises(TimeoutError, match="timed out|timeout"):
        await channel.start()
    assert rt.connection_attempts == 1
    assert channel._platform is None
    assert all(client.is_closed for client in rt.http_clients)
    assert all(
        platform._ws_thread is None or not platform._ws_thread.is_alive()
        for platform in rt.platforms
    )


@pytest.mark.asyncio
async def test_startup_cleanup_failure_retains_http_and_blocks_replacement(
    network_runtime,
    monkeypatch,
):
    rt = network_runtime
    channel = rt.channel()
    rt.token = lambda _config: ""
    # Patch the real client's close boundary after construction, before auth.
    close_allowed = False
    http_class = rt.http_class
    real_close = http_class.aclose

    async def fail_close(client):
        if not close_allowed:
            raise RuntimeError("HTTP close failed")
        await real_close(client)

    monkeypatch.setattr(http_class, "aclose", fail_close)
    with pytest.raises(RuntimeError, match="close"):
        await channel.start()
    platform = channel._platform
    assert platform is not None
    assert platform._http_client is not None
    assert not platform._http_client.is_closed
    assert (await channel.health_check())["status"] == "unhealthy"
    with pytest.raises(RuntimeError):
        await channel.start()
    assert channel._platform is platform
    assert rt.connection_attempts == 0
    close_allowed = True
    await channel.stop()
    assert channel._platform is None
    assert all(client.is_closed for client in rt.http_clients)


@pytest.mark.asyncio
async def test_stop_finishes_inflight_send_before_http_close(
    network_runtime,
    monkeypatch,
):
    from qwenpaw.schemas import TextContent

    rt = network_runtime
    channel = rt.channel()
    await channel.start()
    platform = channel._platform
    http = platform._http_client
    entered, release, finished = (asyncio.Event() for _ in range(3))
    real_request = http.__class__.request

    async def delayed_request(client, method, url, **kwargs):
        if not str(url).endswith("/im/v1/messages"):
            return await real_request(client, method, url, **kwargs)
        entered.set()
        await release.wait()
        assert not http.is_closed
        response = await real_request(client, method, url, **kwargs)
        finished.set()
        return response

    monkeypatch.setattr(http.__class__, "request", delayed_request)
    sending = asyncio.create_task(
        channel.send_content_parts(
            "ou_alice",
            [TextContent(type="text", text="finish this send")],
            {
                "feishu_receive_id": "ou_alice",
                "feishu_receive_id_type": "open_id",
            },
        ),
    )
    stopping = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        stopping = asyncio.create_task(channel.stop())
        await asyncio.sleep(0.02)
        assert not stopping.done()
        assert not http.is_closed
        release.set()
        await asyncio.wait_for(stopping, 2)
        assert await sending == "sent-1"
        assert finished.is_set()
        assert http.is_closed
    finally:
        release.set()
        await asyncio.gather(sending, return_exceptions=True)
        if stopping is not None:
            await asyncio.gather(stopping, return_exceptions=True)


@pytest.mark.asyncio
async def test_overlapping_start_stop_leave_no_resources(network_runtime):
    rt = network_runtime
    channel = rt.channel()
    entered, release = threading.Event(), threading.Event()

    def token(_config):
        entered.set()
        release.wait(2)
        return "valid"

    rt.token = token
    starting = asyncio.create_task(channel.start())
    await eventually(entered.is_set)
    stopping = asyncio.create_task(channel.stop())
    release.set()
    await asyncio.gather(starting, stopping)
    assert (await channel.health_check())["status"] == "unhealthy"
    assert channel._platform is None
    assert rt.connection_attempts == 1
    assert all(client.is_closed for client in rt.http_clients)
    assert all(socket.closed for socket in rt.sockets)


@pytest.mark.asyncio
async def test_reconnect_failure_uses_bounded_backoff_and_stop_cancels_retry(
    network_runtime,
    monkeypatch,
):
    import importlib
    import time

    rt = network_runtime
    channel_module = importlib.import_module("plugin_feishu.channel")
    monkeypatch.setattr(channel_module, "_RECONNECT_INITIAL_DELAY", 0.02)
    monkeypatch.setattr(channel_module, "_RECONNECT_MAX_DELAY", 0.04)
    channel = rt.channel()
    await channel.start()
    attempts = []

    async def rejected_socket(_url, **_kwargs):
        attempts.append(time.monotonic())
        raise ConnectionError("service unavailable")

    monkeypatch.setattr(
        rt.module.lark_ws.websockets,
        "connect",
        rejected_socket,
    )
    rt.sockets[0].drop()
    await eventually(lambda: len(attempts) >= 4)
    intervals = [
        later - earlier for earlier, later in zip(attempts, attempts[1:])
    ]
    assert all(0.03 <= interval < 0.3 for interval in intervals)
    assert (await channel.health_check())["status"] == "unhealthy"
    await channel.stop()
    stopped_at = len(attempts)
    await asyncio.sleep(0.08)
    assert len(attempts) == stopped_at
    assert rt.sockets[0].closed
    assert all(client.is_closed for client in rt.http_clients)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["exception", "timeout"])
async def test_sdk_socket_close_failure_retains_retryable_socket(
    network_runtime,
    failure,
):
    rt = network_runtime
    channel = rt.channel()
    await channel.start()
    platform = channel._platform
    socket = rt.sockets[0]
    http = platform._http_client
    thread = platform._ws_thread
    if failure == "exception":
        socket.close_error = RuntimeError("socket close failed")
    else:
        socket.close_gate = threading.Event()
    try:
        with pytest.raises(RuntimeError):
            await channel.stop()
        assert channel._platform is platform
        assert platform._ws_client._conn is socket
        assert not socket.closed
        assert not http.is_closed
        assert (await channel.health_check())["status"] == "unhealthy"
        with pytest.raises(RuntimeError):
            await channel.start()
        assert rt.connection_attempts == 1
        socket.close_error = None
        if socket.close_gate is not None:
            socket.close_gate.set()
        await channel.stop()
        assert socket.closed
        assert http.is_closed
        assert not thread.is_alive()
        assert channel._platform is None
    finally:
        socket.close_error = None
        if socket.close_gate is not None:
            socket.close_gate.set()


@pytest.mark.asyncio
async def test_old_sdk_dispatcher_cannot_mutate_reconnected_state(
    network_runtime,
):
    from feishu_suite import sdk_event

    rt = network_runtime
    channel = rt.channel()
    await channel.start()
    old_handler = channel._platform._ws_client._event_handler
    rt.sockets[0].drop()
    await eventually(lambda: len(rt.sockets) == 2)
    await wait_healthy(channel)
    platform = channel._platform
    queued = []
    channel.set_enqueue(queued.append)
    old_handler._processorMap["p2.im.message.receive_v1"].do(
        sdk_event("old-sdk-message"),
    )
    await asyncio.sleep(0.02)
    assert "old-sdk-message" not in platform._processed_ids
    assert not queued
    assert not any(
        "reactions" in request.url.path for request in rt.http_requests
    )
    new_handler = platform._ws_client._event_handler
    new_handler._processorMap["p2.im.message.receive_v1"].do(
        sdk_event("current-sdk-message"),
    )
    await eventually(lambda: len(queued) == 1)
    assert queued[0]["meta"]["feishu_message_id"] == "current-sdk-message"


@pytest.mark.asyncio
async def test_stop_preserves_workspace_data_and_joins_before_owned_http_close(
    network_runtime,
    monkeypatch,
):
    rt = network_runtime
    channel = rt.channel()
    await channel.start()
    platform = channel._platform
    http, thread, socket = (
        platform._http_client,
        platform._ws_thread,
        rt.sockets[0],
    )
    config_file = channel._workspace_dir / "config.json"
    media_file = platform._media_work_dir / "saved-media.bin"
    config_file.write_text('{"enabled": true}')
    media_file.write_bytes(b"keep-media")
    await channel._save_receive_id("saved-session", "oc_saved", "chat_id")
    receive_file = channel._workspace_dir / "feishu_receive_ids.json"
    receive_data = receive_file.read_bytes()
    real_close = rt.http_class.aclose
    close_checked = False

    async def observed_close(client):
        nonlocal close_checked
        if client is http:
            assert socket.closed
            assert not thread.is_alive()
            close_checked = True
        await real_close(client)

    monkeypatch.setattr(rt.http_class, "aclose", observed_close)
    await channel.stop()
    assert close_checked
    assert config_file.read_text() == '{"enabled": true}'
    assert media_file.read_bytes() == b"keep-media"
    assert receive_file.read_bytes() == receive_data
    await channel.start()
    assert await channel._load_receive_id("saved-session") == (
        "chat_id",
        "oc_saved",
    )


@pytest.mark.asyncio
async def test_first_reconnect_frame_survives_pending_connect_ack(
    network_runtime,
    monkeypatch,
):
    """A live receiver must not deduplicate then discard its first message."""
    import json

    rt = network_runtime
    channel, queued = rt.channel(), []
    channel.set_enqueue(queued.append)
    await channel.start()
    platform = channel._platform
    original_wait = platform._ws_started.wait
    ack_waiting, release_ack = threading.Event(), threading.Event()
    callback_entered = asyncio.Event()
    original_connect = platform.connect

    async def observed_connect(on_message, on_card):
        async def observed_message(value):
            callback_entered.set()
            await on_message(value)

        await original_connect(observed_message, on_card)

    def delayed_ack(timeout=None):
        connected = original_wait(timeout)
        ack_waiting.set()
        if not release_ack.wait(2):
            raise TimeoutError("test connection acknowledgement not released")
        return connected

    monkeypatch.setattr(platform._ws_started, "wait", delayed_ack)
    monkeypatch.setattr(platform, "connect", observed_connect)
    rt.sockets[0].drop()
    try:
        await eventually(ack_waiting.is_set)
        assert len(rt.sockets) == 2
        socket = rt.sockets[-1]
        await eventually(socket.receiving.is_set)
        payload = {
            "schema": "2.0",
            "header": {
                "event_id": "reconnect-first",
                "event_type": "im.message.receive_v1",
            },
            "event": {
                "sender": {
                    "sender_id": {"open_id": "ou_alice"},
                    "sender_type": "user",
                },
                "message": {
                    "message_id": "reconnect-first",
                    "chat_id": "oc_room",
                    "chat_type": "p2p",
                    "message_type": "text",
                    "content": json.dumps({"text": "first after reconnect"}),
                },
            },
        }
        frame = rt.module.lark_ws.Frame(
            SeqID=1,
            LogID=1,
            service=1,
            method=1,
            payload=json.dumps(payload).encode(),
        )
        for key, value in (
            ("message_id", "reconnect-first"),
            ("trace_id", "trace-test"),
            ("sum", "1"),
            ("seq", "0"),
            ("type", "event"),
        ):
            frame.headers.add(key=key, value=value)
        wire_message = frame.SerializeToString()
        socket.loop.call_soon_threadsafe(
            socket.inbox.put_nowait,
            wire_message,
        )
        await asyncio.wait_for(callback_entered.wait(), 1)
        assert "reconnect-first" not in platform._processed_ids
        release_ack.set()
        await wait_healthy(channel)
        # Only successful callback admission may enter the dedup ledger.
        await eventually(lambda: not platform._callbacks)
        assert [item["meta"]["feishu_message_id"] for item in queued] == [
            "reconnect-first",
        ]
        assert "reconnect-first" in platform._processed_ids
        socket.loop.call_soon_threadsafe(
            socket.inbox.put_nowait,
            wire_message,
        )
        await eventually(lambda: len(socket.receive_loops) >= 3)
        await eventually(lambda: not platform._callbacks)
        assert len(queued) == 1
        handler = platform._ws_client._event_handler
        await channel.stop()
        payload["event"]["message"]["message_id"] = "after-stop"
        handler._do_without_validation(json.dumps(payload).encode())
        await asyncio.sleep(0)
        assert len(queued) == 1
        assert "after-stop" not in platform._processed_ids
    finally:
        release_ack.set()
