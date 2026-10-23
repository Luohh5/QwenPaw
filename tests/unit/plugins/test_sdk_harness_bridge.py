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
from .harness_bridge_fixture import collect, harness_fixture, request, until


async def test_turn_envelopes_attachments_commands_clear_and_history(
    tmp_path,
    harness,
):
    from qwenpaw.app.chats.session import SafeJSONSession
    from qwenpaw.schemas import AgentRequest, MessageType

    session = SafeJSONSession(str(tmp_path / "transcript"))
    runtime = harness.runtime(session=session)
    incoming = request()
    image = tmp_path / "picture.png"
    image.write_bytes(b"fake-image")
    incoming = AgentRequest.model_validate(
        {
            **incoming.model_dump(),
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "hello"},
                        {"type": "image", "image_url": str(image)},
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
    assert backend.last_request.attachments[0].path == image
    persisted = await session.get_session_state_dict(
        "same-session",
        "user",
        "console",
    )
    assert persisted["agent"]["state"]["context"]
    assert (await collect(runtime, "/compact details"))[-1].output[-1].content[
        0
    ].text == "compact:details"
    await collect(runtime, "/clear")
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
    from tests.unit.harnesses.test_capability_resolver import _write_skill

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
    from tests.unit.harnesses.test_capability_resolver import _write_skill

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


@pytest.mark.parametrize("outcome", ["approved", "rejected", "cancelled"])
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
                    ApprovalDecision.APPROVED
                    if outcome == "approved"
                    else ApprovalDecision.DENIED
                ),
                actor=actor,
            )
        assert (await running)[-1].status == "completed"
        assert harness.plugin.created[0].approval_decision == outcome
    finally:
        if not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
