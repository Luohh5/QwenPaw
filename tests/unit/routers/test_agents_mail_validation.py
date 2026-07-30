# -*- coding: utf-8 -*-
"""Unit tests for _validate_mail_config push-rule validation."""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from qwenpaw.app.routers.agents import (
    CreateAgentRequest,
    _validate_mail_config,
    create_agent,
)
from qwenpaw.config.config import (
    AgentMailConfig,
    AgentMailCredential,
    AgentMailPushConfig,
    AgentMailPushRule,
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
