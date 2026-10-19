# -*- coding: utf-8 -*-
"""ApprovalService display severities cross the real shared worker boundary."""

import asyncio
from contextvars import ContextVar
from copy import deepcopy
import json
import os
from types import SimpleNamespace

import pytest

from feishu_process_suite import (
    inbound as feishu_inbound,
    message as feishu_message,
    proxy_api,
    runtime as feishu_runtime,
)
from dingtalk_process_suite import (
    inbound as dingtalk_inbound,
    message as dingtalk_message,
    runtime as dingtalk_runtime,
)


@pytest.fixture
def capture_proxy(proxy_api):
    """Capture card construction; worker tests below prove real delivery."""
    from qwenpaw.channel_protocol.models import ApprovalCardParams

    class CaptureProxy(proxy_api):
        def __new__(cls):
            return object.__new__(cls)

        def __init__(self):
            self._response_context = ContextVar("approval-test-response", default=None)
            self.operations = []

        async def _send_operation(self, to_handle, meta, operation, **fields):
            self.operations.append(deepcopy(fields))
            ApprovalCardParams.from_mapping(fields["approval"])

    return CaptureProxy()


async def send_approval_event(channel, severity_fields):
    from qwenpaw.schemas import Message, TextContent

    event = Message(
        content=[TextContent(text="Diagnostic approval body")],
        metadata={
            "metadata": {
                "message_type": "tool_guard_approval",
                "approval_request_id": "approval-display-1",
                "tool_name": "diagnostic-tool",
                **severity_fields,
            }
        },
    )
    original = deepcopy(event.model_dump())
    meta = {"session_id": "diagnostic-session", "payload": {"severity": "HIGH"}}
    original_meta = deepcopy(meta)
    try:
        await channel.on_event_message_completed(
            SimpleNamespace(),
            "diagnostic-handle",
            event,
            meta,
        )
    finally:
        assert event.model_dump() == original
        assert meta == original_meta


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value,expected",
    [
        ("LOW", "low"),
        ("low", "low"),
        ("MEDIUM", "medium"),
        ("medium", "medium"),
        ("HIGH", "high"),
        ("high", "high"),
        ("CRITICAL", "critical"),
        ("critical", "critical"),
        ("HiGh", "high"),
        ("INFO", "medium"),
        ("info", "medium"),
        ("SAFE", "medium"),
        ("safe", "medium"),
    ],
)
async def test_public_approval_event_maps_display_severity(
    capture_proxy, value, expected
):
    await send_approval_event(capture_proxy, {"severity": value})
    assert capture_proxy.operations == [
        {
            "approval": {
                "request_id": "approval-display-1",
                "tool_name": "diagnostic-tool",
                "severity": expected,
            },
            "content_parts": [{"type": "text", "text": "Diagnostic approval body"}],
        }
    ]


@pytest.mark.asyncio
async def test_public_approval_event_retains_absent_severity_default(capture_proxy):
    await send_approval_event(capture_proxy, {})
    assert capture_proxy.operations[0]["approval"]["severity"] == "medium"


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["UNKNOWN", "", "   ", None, 7])
async def test_public_approval_event_rejects_invalid_severity(capture_proxy, value):
    from qwenpaw.channel_protocol.errors import ProtocolValidationError

    with pytest.raises(ProtocolValidationError):
        await send_approval_event(capture_proxy, {"severity": value})
    assert capture_proxy.operations == []


@pytest.mark.parametrize(
    "value", ["LOW", "MEDIUM", "HIGH", "CRITICAL", "INFO", "SAFE", "info", "safe"]
)
def test_wire_approval_severity_remains_closed_and_canonical(value):
    from qwenpaw.channel_protocol.errors import ProtocolValidationError
    from qwenpaw.channel_protocol.models import ApprovalCardParams

    with pytest.raises(ProtocolValidationError, match="unsupported approval severity"):
        ApprovalCardParams.from_mapping(
            {
                "request_id": "approval-wire-1",
                "tool_name": "diagnostic-tool",
                "severity": value,
            }
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["feishu", "dingtalk"])
@pytest.mark.parametrize("severity,action", [("HIGH", "approve"), ("INFO", "deny")])
async def test_service_notification_reaches_card_and_resolves_wait(
    feishu_runtime,
    dingtalk_runtime,
    monkeypatch,
    platform,
    severity,
    action,
):
    from qwenpaw.app.approvals import service as approval_module
    from qwenpaw.app.approvals.service import ApprovalService
    from qwenpaw.runtime.commands.control.approval_handler import (
        ApprovalCommandHandler,
    )
    from qwenpaw.runtime.commands.control.base import ControlContext
    from qwenpaw.security.tool_guard.approval import ApprovalDecision
    from qwenpaw.security.tool_guard.models import (
        GuardFinding,
        GuardSeverity,
        GuardThreatCategory,
        ToolGuardResult,
    )

    if platform == "feishu":
        rt = await feishu_runtime()
        native = await feishu_inbound(rt, feishu_message("approval-source"))
    else:
        rt = await dingtalk_runtime(
            message_type="card",
            cron_message_type="card",
            card_template_id="template",
        )
        native = await dingtalk_inbound(rt, dingtalk_message("approval-source"))
    request = rt.channel.build_agent_request_from_native(native)
    channel_meta = deepcopy(request.channel_meta)
    original_meta = deepcopy(channel_meta)
    result = ToolGuardResult(
        tool_name="diagnostic-tool",
        params={"probe": "notification-only"},
        findings=[
            GuardFinding(
                id="diagnostic-finding",
                rule_id="diagnostic-rule",
                category=GuardThreatCategory.RESOURCE_ABUSE,
                severity=GuardSeverity(severity),
                title="Diagnostic notification",
                description="No tool is executed by this regression test.",
                tool_name="diagnostic-tool",
            )
        ],
    )
    raw_result = deepcopy(result.to_dict())
    service = ApprovalService()
    monkeypatch.setattr(approval_module, "_approval_service", service)
    pending = await service.create_pending(
        session_id=request.session_id,
        root_session_id=request.session_id,
        owner_agent_id="default",
        user_id=request.user_id,
        channel=platform,
        agent_id="default",
        tool_name="diagnostic-tool",
        result=result,
        extra={"_channel_instance": rt.channel, "channel_meta": channel_meta},
    )
    notify = next(
        task
        for task in asyncio.all_tasks()
        if task.get_name() == f"approval-notify-{pending.request_id[:8]}"
    )
    waiting = asyncio.create_task(service.wait_for_approval(pending.request_id, 300))
    try:
        await asyncio.sleep(0)
        await notify
        assert pending.severity == severity
        assert (await service.get_request(pending.request_id)).severity == severity
        assert result.to_dict() == raw_result
        assert channel_meta == original_meta
        assert not waiting.done()
        assert len(rt.rows("create")) == 1
        assert rt.rows("create")[0]["pid"] != os.getpid()
        if platform == "feishu":
            row = rt.rows("create")[0]
            assert row["msg_type"] == "interactive"
            card = json.loads(row["content"])
            assert "diagnostic-tool" in row["content"]
            buttons = card["elements"][-1]["actions"]
            value = next(
                button["value"]
                for button in buttons
                if button["value"]["action"] == action
            )
            assert value["request_id"] == pending.request_id
            assert value["tool_name"] == "diagnostic-tool"
            assert value["severity"] == ("high" if severity == "HIGH" else "medium")
            assert severity in value["body"]
            callback = await feishu_inbound(
                rt,
                {
                    "header": {"event_id": "approval-decision"},
                    "event": {
                        "operator": {"open_id": "ou_alice"},
                        "action": {"value": value},
                    },
                },
                "card",
            )
        else:
            assert len(rt.rows("deliver")) == 1
            update = rt.rows("update")[-1]["request"]
            assert update["isFinalize"]
            card_text = json.dumps(update)
            assert "diagnostic-tool" in card_text
            assert f"/approval approve {pending.request_id}" in card_text
            assert f"/approval deny {pending.request_id}" in card_text
            assert severity in card_text
            callback = await dingtalk_inbound(
                rt,
                dingtalk_message(
                    "approval-decision",
                    text={"content": f"/approval {action} {pending.request_id}"},
                ),
            )
        command = rt.channel.build_agent_request_from_native(callback)
        assert command.input[0].content[0].text == (
            f"/approval {action} {pending.request_id}"
        )
        context = ControlContext(
            workspace=None,
            payload=command,
            channel=rt.channel,
            session_id=command.session_id,
            user_id=command.user_id,
            agent_id="default",
            args={"action": action, "request_id": pending.request_id},
        )
        await ApprovalCommandHandler().handle(context)
        expected = (
            ApprovalDecision.APPROVED
            if action == "approve"
            else ApprovalDecision.DENIED
        )
        assert await asyncio.wait_for(waiting, 1) is expected
        assert pending.status == expected.value
        assert pending.severity == severity
        assert pending.extra["channel_meta"] == original_meta
        assert await service.get_request(pending.request_id) is None
    finally:
        if not waiting.done():
            waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
