"""Existing empty-caller facts; no invented unknown-identity protocol."""

import ast
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytest_plugins = ["extension_process_suite"]


def test_frozen_voice_empty_caller_builds_default_open_request_without_fabricated_sender():
    from qwenpaw.schemas import MessageType, ContentType

    path = (
        Path(__file__).resolve().parents[3]
        / "tests/fixtures/channel_plugins/legacy_voice_conversation_relay.py"
    )
    tree = ast.parse(path.read_text())
    (fn,) = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_build_agent_request"
    ]
    ns = {"Any": Any, "MessageType": MessageType, "ContentType": ContentType}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), str(path), "exec"), ns)
    # Only data consumed by the original pure builder, not a fake Base/Agent.
    data = SimpleNamespace(
        call_sid="CA-original", caller_info={}, _channel_type="voice"
    )
    request = ns[fn.name](data, "anonymous speech")
    assert request.user_id == "" and request.session_id == "voice:CA-original"
    assert (
        request.channel == "voice"
        and request.input[0].content[0].text == "anonymous speech"
    )


@pytest.mark.parametrize("field", ["sender_id", "acl_sender_id"])
def test_existing_inbound_dto_rejects_empty_identity(field):
    from qwenpaw.channel_protocol.models import InboundEvent
    from qwenpaw.channel_protocol.errors import ProtocolValidationError

    value = {
        "channel_key": "voice",
        "instance_id": "instance",
        "generation": 1,
        "event_id": "event",
        "event_kind": "message.query",
        "conversation": {"id": "voice:CA-original", "type": "dm", "thread_id": None},
        "sender_id": "+15550001",
        "acl_sender_id": "+15550001",
        "sender_name": "",
        "content_parts": [{"type": "text", "text": "hello"}],
        "metadata": {},
        "response_handle": "voice:reply:original",
    }
    value[field] = ""
    with pytest.raises(ProtocolValidationError):
        InboundEvent.from_mapping(value)


@pytest.mark.asyncio
async def test_real_proxy_default_open_allowlist_and_approval_identity_boundaries(
    runtime,
):
    rt = await runtime()
    channel = rt.channel
    payload = {
        "sender_id": "",
        "acl_sender_id": "",
        "session_id": "voice:CA-original",
        "content_parts": [{"type": "text", "text": "hello"}],
        "meta": {"is_group": False},
    }
    request = channel.build_agent_request_from_native(payload)
    assert request.user_id == "" and request.session_id == "voice:CA-original"
    assert not await channel._access_control_gate(payload)
    channel.access_control_dm = True
    store = channel._get_acl_store()
    store.set_whitelist(channel.channel, ["known-caller"])
    known = {**payload, "sender_id": "known-caller", "acl_sender_id": "known-caller"}
    denied = {
        **payload,
        "sender_id": "unlisted-caller",
        "acl_sender_id": "unlisted-caller",
    }
    assert not await channel._access_control_gate(known)
    assert await channel._access_control_gate(denied)
    assert not store.is_whitelisted(channel.channel, "unlisted-caller")
    assert not store.is_whitelisted(channel.channel, "")
    # Observed limitation, not an assertion that unknown is authorized:
    # Base returns early for empty sender before consulting the store.
    assert not await channel._access_control_gate(payload)
    print(
        "BASE_EMPTY_IDENTITY_BYPASSES_ACL_GATE: current DTO blocks ingress; future unknown contract must close this gap"
    )
    for command in [
        "/approve pending-one",
        "/deny pending-one",
        "/approval approve pending-one",
    ]:
        for operator in ["", "   ", None]:
            with pytest.raises(ValueError, match="APPROVAL_OPERATOR_UNAVAILABLE"):
                channel.build_agent_request_from_native(
                    {
                        **payload,
                        "sender_id": "display-or-session",
                        "acl_sender_id": operator,
                        "content_parts": [{"type": "text", "text": command}],
                    }
                )
        actual = channel.build_agent_request_from_native(
            {
                **payload,
                "sender_id": "display-or-session",
                "acl_sender_id": "known-caller",
                "content_parts": [{"type": "text", "text": command}],
            }
        )
        assert actual.user_id == "known-caller"
    assert not rt.ws.requests  # No fake Agent response used for these checks.
