# -*- coding: utf-8 -*-
# pylint: disable=protected-access
"""Unit tests for the mail F1 exploration mode (step-by-step approval).

F1 semantics: once activated for a session, EVERY tool call — built-in
(PolicyGuardedTool path) and MCP/Driver (DriverHandler path) — is gated
to STRICT and requires user approval. When inactive, nothing changes.

F1 state lives in a module-level session registry (NOT a ContextVar):
the tool coordinator runs each tool call in its own asyncio task
(``asyncio.create_task`` copies the context), so a ContextVar written
inside the activation tool would stay isolated in that child task and
never be visible to subsequent tool calls.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from qwenpaw.config.context import (
    _f1_active_sessions,
    activate_f1_for_session,
    current_session_id,
    deactivate_f1_for_session,
    is_f1_active_for_session,
)
from qwenpaw.drivers.handler import _resolve_driver_execution_level
from qwenpaw.governance import tool_adapter as gov_tool_adapter
from qwenpaw.governance.policy import (
    GovernanceAction,
    GovernanceDecision,
    ToolCallSpec,
)
from qwenpaw.security.tool_guard.execution_level import ToolExecutionLevel

_SESSION = "test-session"


@pytest.fixture(autouse=True)
def _reset_f1_registry():
    """Clear the F1 session registry and session_id ContextVar."""
    _f1_active_sessions.clear()
    token = current_session_id.set(None)
    yield
    _f1_active_sessions.clear()
    current_session_id.reset(token)


# ---------- 1. Session registry semantics ----------


def test_registry_activation_roundtrip():
    """activate/is_active/deactivate round-trip per session."""
    assert is_f1_active_for_session(_SESSION) is False

    activate_f1_for_session(_SESSION)
    assert is_f1_active_for_session(_SESSION) is True
    # Other sessions are unaffected.
    assert is_f1_active_for_session("other-session") is False

    deactivate_f1_for_session(_SESSION)
    assert is_f1_active_for_session(_SESSION) is False


def test_registry_tolerates_empty_session():
    """Falsy session ids never match and never raise."""
    assert is_f1_active_for_session(None) is False
    assert is_f1_active_for_session("") is False
    deactivate_f1_for_session(None)  # no-op
    deactivate_f1_for_session("")  # no-op


def test_activation_tool_registers_session():
    """The tool must register the session from get_current_session_id."""
    from qwenpaw.agents.tools.mail_f1_tool import (
        activate_f1_exploration_mode,
    )

    async def _run() -> Any:
        current_session_id.set(_SESSION)
        return await activate_f1_exploration_mode()

    chunk = asyncio.run(_run())
    assert chunk.is_last is True
    assert is_f1_active_for_session(_SESSION) is True


def test_activation_tool_without_session_id_is_safe():
    """No session_id in context → warning path, nothing registered."""
    from qwenpaw.agents.tools.mail_f1_tool import (
        activate_f1_exploration_mode,
    )

    async def _run() -> Any:
        current_session_id.set(None)
        return await activate_f1_exploration_mode()

    chunk = asyncio.run(_run())
    assert chunk.is_last is True
    assert "激活失败" in chunk.content[0].text
    assert not _f1_active_sessions


# ---------- Helpers for the PolicyGuardedTool path ----------


class _FakePolicy:
    def __init__(self, execution_level: str = "smart") -> None:
        self.execution_level = execution_level


class _FakeGovernor:
    """Mimics ResourceGovernor: STRICT → ASK for every tool, else ALLOW."""

    def __init__(self, execution_level: str = "smart") -> None:
        self.policy = _FakePolicy(execution_level)
        self.seen_levels: list[str] = []

    def assert_policy(self, _tc_spec: ToolCallSpec) -> GovernanceDecision:
        self.seen_levels.append(self.policy.execution_level)
        if self.policy.execution_level == "strict":
            return GovernanceDecision(
                action=GovernanceAction.ASK,
                reason="STRICT mode: all tool calls require approval",
            )
        return GovernanceDecision(
            action=GovernanceAction.ALLOW,
            reason="allowed",
        )

    def audit(self, tc_spec: ToolCallSpec, decision: Any) -> None:
        pass


def _make_tool(governor, request_context=None, name="edit_file"):
    """Build a minimal stand-in for a PolicyGuardedTool instance."""
    tc_spec = ToolCallSpec(
        tool_name=name,
        target="",
        agent_id="",
        session_id="",
        raw_params={},
    )
    return SimpleNamespace(
        name=name,
        _qp_governor=governor,
        _qp_request_context=dict(request_context or {}),
        _build_tc_spec=lambda: tc_spec,
    )


@pytest.fixture()
def _stub_ask_approval(monkeypatch):
    """Replace the blocking approval flow with a recognisable sentinel."""
    from agentscope.permission import PermissionBehavior, PermissionDecision

    calls = []

    async def _fake_ask(**kwargs):
        calls.append(kwargs)
        return PermissionDecision(
            behavior=PermissionBehavior.ASK,
            message="sentinel: approval requested",
        )

    monkeypatch.setattr(gov_tool_adapter, "_ask_user_approval", _fake_ask)
    return calls


# ---------- 2. PolicyGuardedTool: F1 gates ALL tools ----------


@pytest.mark.parametrize(
    "tool_name",
    ["edit_file", "read_file", "qwenpawmail__reply_message", "browser_use"],
)
def test_policy_tool_gates_all_tools_when_f1_active(
    _stub_ask_approval,
    tool_name,
):
    """With F1 active, every tool must be evaluated under STRICT → ASK."""
    activate_f1_for_session(_SESSION)

    governor = _FakeGovernor(execution_level="smart")
    tool = _make_tool(
        governor,
        request_context={"session_id": _SESSION},
        name=tool_name,
    )

    decision = asyncio.run(
        gov_tool_adapter._policy_tool_check_permissions(tool, {}),
    )

    # Evaluation ran under STRICT and routed to the approval flow.
    assert governor.seen_levels == ["strict"]
    assert len(_stub_ask_approval) == 1
    assert "approval requested" in decision.message
    # The pre-F1 level must be restored (no leak into later requests).
    assert governor.policy.execution_level == "smart"


def test_policy_tool_session_id_fallback_to_contextvar(_stub_ask_approval):
    """Without session_id in request_context, the ContextVar is used."""
    activate_f1_for_session(_SESSION)

    governor = _FakeGovernor(execution_level="smart")
    tool = _make_tool(governor, name="edit_file")

    async def _run() -> Any:
        current_session_id.set(_SESSION)
        return await gov_tool_adapter._policy_tool_check_permissions(
            tool,
            {},
        )

    decision = asyncio.run(_run())
    assert governor.seen_levels == ["strict"]
    assert "approval requested" in decision.message


def test_policy_tool_f1_overrides_off_level(_stub_ask_approval):
    """F1 must override approval_level=off (no silent allow-all)."""
    activate_f1_for_session(_SESSION)

    governor = _FakeGovernor(execution_level="smart")
    tool = _make_tool(
        governor,
        request_context={"approval_level": "off", "session_id": _SESSION},
        name="qwenpawmail__send_message",
    )

    decision = asyncio.run(
        gov_tool_adapter._policy_tool_check_permissions(tool, {}),
    )

    assert governor.seen_levels == ["strict"]
    assert "approval requested" in decision.message


# ---------- 3. PolicyGuardedTool: unaffected when F1 inactive ----------


def test_policy_tool_normal_when_f1_inactive(_stub_ask_approval):
    """Without F1, evaluation uses the governor's own level (ALLOW here)."""
    from agentscope.permission import PermissionBehavior

    governor = _FakeGovernor(execution_level="smart")
    tool = _make_tool(
        governor,
        request_context={"session_id": _SESSION},
        name="qwenpawmail__reply_message",
    )

    decision = asyncio.run(
        gov_tool_adapter._policy_tool_check_permissions(tool, {}),
    )

    assert decision.behavior == PermissionBehavior.ALLOW
    assert governor.seen_levels == ["smart"]
    assert not _stub_ask_approval


def test_policy_tool_normal_after_deactivate(_stub_ask_approval):
    """deactivate_f1_for_session restores the normal approval flow."""
    from agentscope.permission import PermissionBehavior

    activate_f1_for_session(_SESSION)
    deactivate_f1_for_session(_SESSION)

    governor = _FakeGovernor(execution_level="smart")
    tool = _make_tool(
        governor,
        request_context={"session_id": _SESSION},
        name="edit_file",
    )

    decision = asyncio.run(
        gov_tool_adapter._policy_tool_check_permissions(tool, {}),
    )

    assert decision.behavior == PermissionBehavior.ALLOW
    assert governor.seen_levels == ["smart"]
    assert not _stub_ask_approval


# ---------- 4. Driver/MCP path: F1 forces STRICT ----------


def test_driver_level_strict_when_f1_active():
    """MCP tools resolve to STRICT while F1 is active for the session."""
    activate_f1_for_session(_SESSION)

    level = _resolve_driver_execution_level({"session_id": _SESSION})
    assert level is ToolExecutionLevel.STRICT
    assert level.requires_approval_for_all_tools() is True


def test_driver_level_session_id_fallback_to_contextvar():
    """Without session_id in request_context, the ContextVar is used."""
    activate_f1_for_session(_SESSION)

    async def _run() -> ToolExecutionLevel:
        current_session_id.set(_SESSION)
        return _resolve_driver_execution_level({})

    level = asyncio.run(_run())
    assert level is ToolExecutionLevel.STRICT


def test_driver_level_f1_overrides_off():
    """F1 overrides an explicit approval_level=off in request_context."""
    activate_f1_for_session(_SESSION)

    level = _resolve_driver_execution_level(
        {"approval_level": "off", "session_id": _SESSION},
    )
    assert level is ToolExecutionLevel.STRICT


def test_driver_level_normal_when_f1_inactive():
    """Without F1, request_context approval_level is honoured."""
    level = _resolve_driver_execution_level(
        {"approval_level": "auto", "session_id": _SESSION},
    )
    assert level is ToolExecutionLevel.AUTO

    level = _resolve_driver_execution_level(
        {"approval_level": "off", "session_id": _SESSION},
    )
    assert level is ToolExecutionLevel.OFF


def test_driver_level_normal_after_deactivate():
    """deactivate restores request_context approval_level resolution."""
    activate_f1_for_session(_SESSION)
    deactivate_f1_for_session(_SESSION)

    level = _resolve_driver_execution_level(
        {"approval_level": "auto", "session_id": _SESSION},
    )
    assert level is ToolExecutionLevel.AUTO


# ---------- 5. create_task isolation (the real production scenario) ----------


def test_activation_survives_create_task_isolation(_stub_ask_approval):
    """Activation inside a per-tool child task must be visible outside.

    Mirrors production: ToolCoordinator.execute() wraps every tool call
    in ``asyncio.create_task``, which copies the contextvars context.
    The old ContextVar mechanism passed same-coroutine tests but failed
    here — the flag never propagated back to the parent task. The
    session registry must survive this isolation.
    """
    from qwenpaw.agents.tools.mail_f1_tool import (
        activate_f1_exploration_mode,
    )

    async def _run() -> tuple[Any, ToolExecutionLevel]:
        # PRE_DISPATCH sets session_id before any tool task is spawned,
        # so child tasks inherit it via the copied context.
        current_session_id.set(_SESSION)

        # Tool call #1: activation runs in its own child task.
        await asyncio.create_task(activate_f1_exploration_mode())

        # Parent task must see the activation.
        assert is_f1_active_for_session(_SESSION) is True

        # Tool call #2 (another child task): PolicyGuardedTool path.
        governor = _FakeGovernor(execution_level="smart")
        tool = _make_tool(
            governor,
            request_context={"session_id": _SESSION},
            name="qwenpawmail__reply_message",
        )
        policy_decision = await asyncio.create_task(
            gov_tool_adapter._policy_tool_check_permissions(tool, {}),
        )
        assert governor.seen_levels == ["strict"]

        # Tool call #3 (another child task): Driver/MCP path.
        driver_level = await asyncio.create_task(_driver_level_task())
        return policy_decision, driver_level

    async def _driver_level_task() -> ToolExecutionLevel:
        return _resolve_driver_execution_level({"session_id": _SESSION})

    decision, level = asyncio.run(_run())
    assert "approval requested" in decision.message
    assert level is ToolExecutionLevel.STRICT
