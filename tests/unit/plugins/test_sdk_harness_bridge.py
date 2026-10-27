# -*- coding: utf-8 -*-
"""Production bridge preserves wire/history semantics and host identity."""

import asyncio
import threading
from pathlib import Path

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


@pytest.mark.parametrize("reset_command", ["/clear", "/new"])
async def test_turn_envelopes_attachments_commands_clear_and_history(
    tmp_path,
    harness,
    reset_command,
):
    from qwenpaw.app.chats.session import SafeJSONSession
    from qwenpaw.schemas import AgentRequest, MessageType

    session = SafeJSONSession(str(tmp_path / "transcript"))
    runtime = harness.runtime(session=session)
    incoming = request()
    image = tmp_path / "picture.png"
    image.write_bytes(b"fake-image")
    document = tmp_path / "notes.txt"
    document.write_text("notes", encoding="utf-8")
    incoming = AgentRequest.model_validate(
        {
            **incoming.model_dump(),
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "hello"},
                        {"type": "image", "image_url": str(image)},
                        {
                            "type": "file",
                            "filename": "notes.txt",
                            "file_url": str(document),
                        },
                    ],
                },
            ],
        },
    )
    output = [item async for item in runtime.stream("fake-backend", incoming)]
    final = output[-1]
    assert final.status == "completed"
    assert [message.type for message in final.output] == [
        MessageType.MESSAGE,
        MessageType.REASONING,
        MessageType.PLUGIN_CALL,
        MessageType.PLUGIN_CALL_OUTPUT,
    ]
    assert (
        final.output[2].content[0].data["arguments"]
        == '{"command": "echo test"}'
    )
    backend = harness.plugin.created[0]
    assert [
        attachment.path for attachment in backend.last_request.attachments
    ] == [
        image,
        document,
    ]
    assert backend.last_request.attachments[1].name == "notes.txt"
    attachment_only = AgentRequest.model_validate(
        {
            **incoming.model_dump(),
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image", "image_url": str(image)},
                    ],
                },
            ],
        },
    )
    assert [
        item
        async for item in runtime.stream(
            "fake-backend",
            attachment_only,
        )
    ][-1].status == "completed"
    assert backend.last_request.prompt == ""
    persisted = await session.get_session_state_dict(
        "same-session",
        "user",
        "console",
    )
    assert persisted["agent"]["state"]["context"]
    assert (await collect(runtime, "/compact details"))[-1].output[-1].content[
        0
    ].text == "compact:details"
    await collect(runtime, reset_command)
    assert backend.resets == 1
    cleared = await session.get_session_state_dict(
        "same-session",
        "user",
        "console",
    )
    assert cleared["agent"]["state"]["context"] == []
    await runtime.hydrate_session(
        "fake-backend",
        session_id="same-session",
        user_id="user",
        channel="console",
    )
    assert backend.history_calls == 1
    hydrated = await session.get_session_state_dict(
        "same-session",
        "user",
        "console",
    )
    assert "old answer" in str(hydrated)
    await runtime.hydrate_session(
        "fake-backend",
        session_id="same-session",
        user_id="user",
        channel="console",
    )
    assert backend.history_calls == 1
    assert (await collect(runtime, "error"))[-1].status == "failed"


async def test_call_context_uses_host_identity_and_fresh_capabilities(
    tmp_path,
    harness,
):
    from qwenpaw.app.approvals import get_approval_service
    from qwenpaw.plugins import SDKError
    from qwenpaw.plugins.harnesses import HarnessApprovalSummary
    from tests.unit.plugins.test_harness_capabilities import _write_skill

    path = tmp_path / "one"
    _write_skill(path, "console-only", channels=["console"])
    _write_skill(path, "mail-only", channels=["mail"])
    from types import SimpleNamespace
    from qwenpaw.app.driver_config_service import DriverConfigService
    from qwenpaw.drivers.contracts import (
        DriverCard,
        DriverPolicy,
        PolicyPrincipal,
        PolicyRule,
        PolicyTarget,
    )

    drivers = DriverConfigService(SimpleNamespace(workspace_dir=path))
    await drivers.save_card(
        DriverCard(
            name="policy-mcp",
            protocol="mcp",
            endpoint={
                "transport": "stdio",
                "command": "never-run",
                "env": {"TOKEN": "resolved-test-secret"},
            },
            config={"tools": ["read", "write"]},
            policy=DriverPolicy(
                default_effect="deny",
                rules=[
                    PolicyRule(
                        subject="*",
                        effect="allow",
                        target=PolicyTarget(kind="tool", name="read"),
                        principal=PolicyPrincipal(
                            source_type="channel",
                            source_value="console",
                            subject_type="all",
                            subject_value="*",
                        ),
                    ),
                ],
            ),
        ),
        reload_driver=False,
    )
    runtime = harness.runtime()
    forged = {
        "_request_context": {
            "user_id": "forged",
            "channel": "mail",
            "session_id": "forged",
        },
    }
    await collect(runtime, user="alice", channel="console", settings=forged)
    await collect(runtime, user="bob", channel="mail", settings=forged)
    assert len(harness.plugin.created) == 1
    backend = harness.plugin.created[0]
    alice, bob = backend.calls[-2:]
    assert (
        alice.request_id,
        alice.user_id,
        alice.channel,
        alice.session_id,
    ) == ("trusted-request", "alice", "console", "same-session")
    assert (bob.user_id, bob.channel, bob.session_id) == (
        "bob",
        "mail",
        "same-session",
    )
    assert [skill.name for skill in alice.runtime_capabilities.skills] == [
        "console-only",
    ]
    assert [skill.name for skill in bob.runtime_capabilities.skills] == [
        "mail-only",
    ]
    alice_mcp, bob_mcp = (
        alice.runtime_capabilities.mcp_servers[0],
        bob.runtime_capabilities.mcp_servers[0],
    )
    assert alice_mcp.tool_policies == {"read": "allow", "write": "deny"}
    assert bob_mcp.tool_policies == {"read": "deny", "write": "deny"}
    assert alice_mcp.revealed_env() == {"TOKEN": "resolved-test-secret"}
    assert "resolved-test-secret" not in repr(alice.runtime_capabilities)
    await runtime.call("fake-backend", "status", forged)
    assert backend.calls[-1].runtime_capabilities is None
    assert backend.calls[-1].session_id == ""
    service = get_approval_service()
    pending_before = await service.list_pending_by_session("same-session")
    with pytest.raises(SDKError):
        await alice.request_approval(
            HarnessApprovalSummary("late-tool", "Late request"),
            timeout=0.05,
        )
    assert (
        await service.list_pending_by_session("same-session") == pending_before
    )


async def test_same_session_id_isolated_by_workspace_and_user(
    tmp_path,
    harness,
):
    from qwenpaw.app.chats.session import SafeJSONSession

    one_store = SafeJSONSession(str(tmp_path / "one-history"))
    two_store = SafeJSONSession(str(tmp_path / "two-history"))
    one, two = harness.runtime("one", one_store), harness.runtime(
        "two",
        two_store,
    )
    await collect(one, user="alice")
    await collect(one, user="bob")
    await collect(two, user="alice")
    assert len(harness.plugin.created) == 2
    contexts = [backend.context for backend in harness.plugin.created]
    assert {context.workspace_id for context in contexts} == {"one", "two"}
    assert contexts[0].state_dir != contexts[1].state_dir
    alice = await one_store.get_session_state_dict(
        "same-session",
        "alice",
        "console",
    )
    bob = await one_store.get_session_state_dict(
        "same-session",
        "bob",
        "console",
    )
    for saved in (alice, bob):
        transcript = saved["agent"]["state"]["context"]
        assert "first" in str(transcript) and "hello" in str(transcript)
    await collect(one, "/clear", user="alice")
    assert (
        await one_store.get_session_state_dict(
            "same-session",
            "alice",
            "console",
        )
    )["agent"]["state"]["context"] == []
    assert (
        await one_store.get_session_state_dict(
            "same-session",
            "bob",
            "console",
        )
    )["agent"]["state"]["context"]
    assert (
        await two_store.get_session_state_dict(
            "same-session",
            "alice",
            "console",
        )
    )["agent"]["state"]["context"]


async def test_concurrent_same_session_approval_and_credentials_stay_scoped(
    tmp_path,
    harness,
):
    from types import SimpleNamespace

    from qwenpaw.app.approvals import get_approval_service
    from qwenpaw.app.approvals.service import (
        ApprovalActor,
        ApprovalIdentityMismatchError,
    )
    from qwenpaw.app.driver_config_service import DriverConfigService
    from qwenpaw.drivers.contracts import DriverCard
    from qwenpaw.security.tool_guard.approval import ApprovalDecision

    for workspace, token in (("one", "one-secret"), ("two", "two-secret")):
        path = tmp_path / workspace
        path.mkdir()
        await DriverConfigService(
            SimpleNamespace(workspace_dir=path),
        ).save_card(
            DriverCard(
                name="private-mcp",
                protocol="mcp",
                endpoint={
                    "transport": "stdio",
                    "command": "never-run",
                    "env": {"TOKEN": token},
                },
                config={"tools": ["read"]},
            ),
            reload_driver=False,
        )
    service = get_approval_service()
    calls = (
        ("one", "alice", "console"),
        ("two", "bob", "mail"),
    )
    tasks = [
        asyncio.create_task(
            collect(
                harness.runtime(workspace),
                "approval",
                user=user,
                channel=channel,
            ),
        )
        for workspace, user, channel in calls
    ]
    try:
        async with asyncio.timeout(5):
            while True:
                pending = await service.list_pending_by_session(
                    "same-session",
                )
                if len(pending) == 2:
                    break
                await asyncio.sleep(0.005)
        by_agent = {row.agent_id: row for row in pending}
        assert set(by_agent) == {"one", "two"}
        assert {
            (row.user_id, row.channel, row.session_id) for row in pending
        } == {
            ("alice", "console", "same-session"),
            ("bob", "mail", "same-session"),
        }
        contexts = {
            backend.context.workspace_id: backend.calls[0]
            for backend in harness.plugin.created
        }
        assert {
            workspace: context.runtime_capabilities.mcp_servers[
                0
            ].revealed_env()
            for workspace, context in contexts.items()
        } == {
            "one": {"TOKEN": "one-secret"},
            "two": {"TOKEN": "two-secret"},
        }
        alice = ApprovalActor(
            "same-session",
            "same-session",
            "alice",
            "console",
            "one",
        )
        bob = ApprovalActor(
            "same-session",
            "same-session",
            "bob",
            "mail",
            "two",
        )
        with pytest.raises(ApprovalIdentityMismatchError):
            await service.resolve_request(
                by_agent["one"].request_id,
                ApprovalDecision.APPROVED,
                actor=bob,
            )
        await service.resolve_request(
            by_agent["one"].request_id,
            ApprovalDecision.APPROVED,
            actor=alice,
        )
        await service.resolve_request(
            by_agent["two"].request_id,
            ApprovalDecision.DENIED,
            actor=bob,
        )
        completed = await asyncio.gather(*tasks)
        assert all(items[-1].status == "completed" for items in completed)
        assert {
            backend.context.workspace_id: backend.approval_decision
            for backend in harness.plugin.created
        } == {"one": "approved", "two": "rejected"}
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_concurrent_turn_settings_reuse_backend(harness):
    runtime = harness.runtime()
    first_settings = {"model": "one", "permissions": "read-only"}
    second_settings = {"model": "two", "permissions": "full-access"}
    first = asyncio.create_task(
        collect(
            runtime,
            "hold",
            session="first",
            user="alice",
            channel="console",
            settings=first_settings,
        ),
    )
    second = None
    try:
        await until(
            lambda: harness.plugin.created
            and harness.plugin.created[0].entered.is_set(),
        )
        backend = harness.plugin.created[0]
        second = asyncio.create_task(
            collect(
                runtime,
                "hold",
                session="second",
                user="bob",
                channel="mail",
                settings=second_settings,
            ),
        )
        await until(lambda: len(backend.calls) == 2 or second.done())
        if second.done():
            await second
        assert len(harness.plugin.created) == 1
        assert [call.settings for call in backend.calls] == [
            first_settings,
            second_settings,
        ]
        assert [
            (call.session_id, call.user_id, call.channel)
            for call in backend.calls
        ] == [("first", "alice", "console"), ("second", "bob", "mail")]
        assert not first.done() and not second.done() and not backend.closed
        backend.release.set()
        completed = await asyncio.gather(first, second)
        assert all(result[-1].status == "completed" for result in completed)
    finally:
        for backend in harness.plugin.created:
            backend.release.set()
        await asyncio.gather(
            first,
            *([second] if second is not None else []),
            return_exceptions=True,
        )


async def test_settings_copies_isolate_calls_factory_and_reuse(harness):
    from copy import deepcopy

    runtime = harness.runtime()
    original = {
        "binary": "one",
        "connection": {"env": {"TOKEN": "original"}},
        "turn": {"args": ["original"]},
        "model": "chosen",
    }
    incoming = deepcopy(original)
    await runtime.call("fake-backend", "status", incoming)
    backend = harness.plugin.created[0]
    first = backend.calls[0]
    assert backend.settings == {
        "binary": "one",
        "connection": {"env": {"TOKEN": "original"}},
    }
    assert first.settings == original
    incoming["connection"]["env"]["TOKEN"] = "caller"
    incoming["turn"]["args"].append("caller")
    assert first.settings == original
    first.settings["connection"]["env"]["TOKEN"] = "first-call"
    first.settings["turn"]["args"].append("first-call")
    assert backend.settings["connection"]["env"]["TOKEN"] == "original"
    backend.settings["connection"]["env"]["TOKEN"] = "factory"
    await runtime.call("fake-backend", "status", original)
    assert len(harness.plugin.created) == 1
    second = backend.calls[-1]
    assert second.settings == original
    second.settings["turn"]["args"].append("second-call")
    assert first.settings["turn"]["args"] == ["original", "first-call"]
    assert original["turn"]["args"] == ["original"]
    assert first.settings["connection"]["env"]["TOKEN"] == "first-call"


async def test_undeclared_settings_are_only_per_call(tmp_path, harness):
    source = SOURCE.replace(
        '                instance_settings_keys=("binary", "connection"),\n',
        "",
    )
    plugin = await load_backend(
        tmp_path,
        harness.loader,
        owner="default-owner",
        backend="default-backend",
        source=source,
    )
    runtime = harness.runtime()
    try:
        await runtime.call("default-backend", "status", {"binary": "one"})
        await runtime.call("default-backend", "status", {"binary": "two"})
        assert len(plugin.created) == 1
        backend = plugin.created[0]
        assert backend.settings == {}
        assert [call.settings for call in backend.calls] == [
            {"binary": "one"},
            {"binary": "two"},
        ]
    finally:
        await harness.loader.unload_plugin("default-owner")


async def test_console_busy_failure_emits_failed_response(harness):
    import json

    from qwenpaw.app.channels.console.channel import ConsoleChannel

    runtime = harness.runtime()
    active = asyncio.create_task(
        collect(runtime, "hold", settings={"binary": "one"}),
    )
    try:
        await until(
            lambda: harness.plugin.created
            and harness.plugin.created[0].entered.is_set(),
        )
        backend = harness.plugin.created[0]

        def process(incoming):
            return runtime.stream("fake-backend", incoming, {"binary": "two"})

        channel = ConsoleChannel(process=process, enabled=True, bot_prefix="")
        events = [
            json.loads(event.removeprefix("data: "))
            async for event in channel.stream_one(
                {"sender_id": "other", "content_parts": [], "meta": {}},
            )
        ]
        assert events, "Busy backend failure must produce an SSE response"
        assert events[-1]["object"] == "response"
        assert events[-1]["status"] == "failed"
        assert events[-1]["error"]["code"] == "plugin_busy"
        assert len(harness.plugin.created) == 1
        assert not active.done() and not backend.closed
        assert not backend.close_entered.is_set()
    finally:
        for backend in harness.plugin.created:
            backend.release.set()
        await active


async def test_materialized_history_does_not_resolve_missing_backend(tmp_path):
    from qwenpaw.app.chats.session import SafeJSONSession
    from qwenpaw.runtime.backends.execution import BackendExecution
    from qwenpaw.runtime.backends.registry import BackendRegistry
    from qwenpaw.runtime.backends.session import HarnessSessionBridge
    from qwenpaw.plugins.harnesses import (
        HarnessHistoryItem,
        HarnessHistoryKind,
    )

    session = SafeJSONSession(str(tmp_path / "history"))
    bridge = HarnessSessionBridge(session)
    await bridge.hydrate(
        session_id="same",
        user_id="user",
        channel="console",
        backend="gone",
        history=(
            HarnessHistoryItem(HarnessHistoryKind.MESSAGE, text="saved"),
        ),
    )
    runtime = BackendExecution(
        tmp_path,
        session=session,
        registry=BackendRegistry(),
    )
    await runtime.hydrate_session(
        "gone",
        session_id="same",
        user_id="user",
        channel="console",
    )
    persisted = await session.get_session_state_dict("same", "user", "console")
    assert "saved" in str(persisted)
    await runtime.close()


async def test_skill_snapshot_keeps_loop_live_and_lock_until_worker_finishes(
    tmp_path,
    monkeypatch,
):
    from qwenpaw.plugins._harness_capabilities import HarnessCapabilityResolver
    from qwenpaw.utils.io_utils import get_path_lock
    from tests.unit.plugins.test_harness_capabilities import _write_skill

    _write_skill(tmp_path, "blocked", channels=["console"])
    target = tmp_path / "skills" / "blocked" / "SKILL.md"
    entered, release = threading.Event(), threading.Event()
    original_open = Path.open

    def blocked_open(path, *args, **kwargs):
        if path == target and args and "b" in args[0]:
            entered.set()
            assert release.wait(5), "test did not release bounded file read"
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", blocked_open)
    lock = get_path_lock(target)
    resolver = HarnessCapabilityResolver(tmp_path)

    async def snapshot():
        async with lock:
            return await resolver.resolve({"channel": "console"})

    task = asyncio.create_task(snapshot())
    contender_entered = asyncio.Event()

    async def contender():
        async with lock:
            contender_entered.set()

    competing = None
    try:
        await until(entered.is_set)
        # Loop progress continues while the actual file read is blocked.
        task.cancel()
        competing = asyncio.create_task(contender())
        await asyncio.sleep(0)
        assert not task.done() and not contender_entered.is_set()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        if competing is not None:
            await competing
    assert contender_entered.is_set()


@pytest.mark.parametrize(
    "outcome",
    ["approved", "rejected", "cancelled", "timeout"],
)
async def test_approval_uses_host_identity_and_original_decisions(
    harness,
    outcome,
):
    from qwenpaw.app.approvals import get_approval_service
    from qwenpaw.app.approvals.service import ApprovalActor
    from qwenpaw.security.tool_guard.approval import ApprovalDecision

    service = get_approval_service()
    runtime = harness.runtime()
    running = asyncio.create_task(
        collect(
            runtime,
            "approval",
            user="trusted-user",
            channel="console",
            session="approval-session",
            settings={
                "_request_context": {
                    "user_id": "forged",
                    "session_id": "forged",
                },
            },
        ),
    )
    pending = None
    try:
        async with asyncio.timeout(5):
            while pending is None:
                rows = await service.list_pending_by_session(
                    "approval-session",
                )
                pending = rows[0] if rows else None
                await asyncio.sleep(0.005)
        assert (
            pending.user_id,
            pending.channel,
            pending.agent_id,
            pending.owner_agent_id,
        ) == ("trusted-user", "console", "one", "one")
        actor = ApprovalActor(
            "approval-session",
            "approval-session",
            "trusted-user",
            "console",
            "one",
        )
        if outcome == "cancelled":
            await service.cancel_request(pending.request_id, actor=actor)
        else:
            await service.resolve_request(
                pending.request_id,
                (
                    {
                        "approved": ApprovalDecision.APPROVED,
                        "rejected": ApprovalDecision.DENIED,
                        "timeout": ApprovalDecision.TIMEOUT,
                    }[outcome]
                ),
                actor=actor,
            )
        assert (await running)[-1].status == "completed"
        assert harness.plugin.created[0].approval_decision == outcome
    finally:
        if not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
