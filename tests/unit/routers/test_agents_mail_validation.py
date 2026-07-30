# -*- coding: utf-8 -*-
"""Unit tests for _validate_mail_config push-rule validation."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from qwenpaw.app.routers.agents import (
    CreateAgentRequest,
    _build_copied_agent_config,
    _validate_mail_config,
    create_agent,
    update_agent,
)
from qwenpaw.config.config import (
    AgentMailConfig,
    AgentMailCredential,
    AgentMailPushConfig,
    AgentMailPushRule,
    AgentProfileConfig,
)


def _valid_mail(push: AgentMailPushConfig | None = None) -> AgentMailConfig:
    return AgentMailConfig(
        is_new_account=False,
        credential=AgentMailCredential(
            name="tester",
            domain="163.com",
            auth_code="a" * 16,
            password="pw",
            phone_number="13800000000",
        ),
        push=push,
    )


def test_valid_config_without_push_passes():
    _validate_mail_config(_valid_mail())


def test_valid_push_config_passes():
    push = AgentMailPushConfig(
        mode="rules_then_agent",
        rules=[
            AgentMailPushRule(
                field="subject",
                contains="invoice",
                action="move",
                param="Archive",
            ),
            AgentMailPushRule(field="from", contains="mom",
                              action="wake_agent"),
        ],
    )
    _validate_mail_config(_valid_mail(push))


def test_move_rule_without_param_rejected():
    push = AgentMailPushConfig(
        mode="rules_only",
        rules=[
            AgentMailPushRule(field="subject", contains="x",
                              action="move", param="  "),
        ],
    )
    with pytest.raises(HTTPException) as exc_info:
        _validate_mail_config(_valid_mail(push))
    assert exc_info.value.status_code == 400
    assert "move" in exc_info.value.detail


def test_too_many_rules_rejected():
    push = AgentMailPushConfig(
        mode="rules_only",
        rules=[
            AgentMailPushRule(field="from", contains=f"user{i}")
            for i in range(51)
        ],
    )
    with pytest.raises(HTTPException) as exc_info:
        _validate_mail_config(_valid_mail(push))
    assert exc_info.value.status_code == 400
    assert "50" in exc_info.value.detail


def test_unsupported_domain_still_rejected():
    mail = _valid_mail()
    mail.credential.domain = "gmail.com"
    with pytest.raises(HTTPException) as exc_info:
        _validate_mail_config(mail)
    assert exc_info.value.status_code == 400


def test_create_agent_rejects_mail_for_third_party_backend():
    request = CreateAgentRequest(
        name="mailbot",
        backend="claude_code",
        mail=_valid_mail(),
    )
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(create_agent(request=request, http_request=None))
    assert exc_info.value.status_code == 400
    assert "qwenpaw backend" in exc_info.value.detail


def _fake_global_config(agent_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        agents=SimpleNamespace(
            profiles={agent_id: SimpleNamespace(workspace_dir="/tmp/ws")},
        ),
    )


def test_update_agent_rejects_mail_when_existing_backend_third_party():
    # Request does not set backend explicitly: the effective backend
    # must fall back to the existing third-party config.
    body = AgentProfileConfig(id="a1", name="bot", mail=_valid_mail())
    with patch(
        "qwenpaw.app.routers.agents.load_config",
        return_value=_fake_global_config("a1"),
    ), patch(
        "qwenpaw.app.routers.agents.load_agent_config",
        return_value=SimpleNamespace(backend="claude_code"),
    ):
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(
                update_agent(agentId="a1", agent_config=body, request=None),
            )
    assert exc_info.value.status_code == 400
    assert "qwenpaw backend" in exc_info.value.detail


def test_update_agent_rejects_mail_with_explicit_third_party_backend():
    body = AgentProfileConfig(
        id="a1",
        name="bot",
        backend="claude_code",
        mail=_valid_mail(),
    )
    with patch(
        "qwenpaw.app.routers.agents.load_config",
        return_value=_fake_global_config("a1"),
    ), patch(
        "qwenpaw.app.routers.agents.load_agent_config",
        return_value=SimpleNamespace(backend="qwenpaw"),
    ):
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(
                update_agent(agentId="a1", agent_config=body, request=None),
            )
    assert exc_info.value.status_code == 400
    assert "qwenpaw backend" in exc_info.value.detail


def test_copied_agent_drops_mail_for_third_party_backend(tmp_path):
    source = AgentProfileConfig(
        id="src",
        name="src",
        backend="claude_code",
        mail=_valid_mail(),
    )
    copied = _build_copied_agent_config(
        source_config=source,
        new_id="new",
        new_name="src Copy",
        workspace_dir=tmp_path,
    )
    assert copied.mail is None


def test_copied_agent_keeps_mail_for_qwenpaw_backend(tmp_path):
    source = AgentProfileConfig(
        id="src",
        name="src",
        backend="qwenpaw",
        mail=_valid_mail(),
    )
    copied = _build_copied_agent_config(
        source_config=source,
        new_id="new",
        new_name="src Copy",
        workspace_dir=tmp_path,
    )
    assert copied.mail is not None
