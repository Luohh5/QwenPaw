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
    _build_qwenpawmail_env,
    _ensure_mail_triage_file,
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
            password="",
            phone_number="",
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
            AgentMailPushRule(
                field="from",
                contains="mom",
                action="wake_agent",
            ),
        ],
    )
    _validate_mail_config(_valid_mail(push))


def test_move_rule_without_param_rejected():
    push = AgentMailPushConfig(
        mode="rules_only",
        rules=[
            AgentMailPushRule(
                field="subject",
                contains="x",
                action="move",
                param="  ",
            ),
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
    mail.credential.domain = "unknown.example"
    with pytest.raises(HTTPException) as exc_info:
        _validate_mail_config(mail)
    assert exc_info.value.status_code == 400


def test_new_whitelisted_domains_pass():
    for domain in (
        "sina.com",
        "sina.cn",
        "aliyun.com",
        "gmail.com",
        "exmail.qq.com",
        "qiye.aliyun.com",
        "qiye.163.com",
    ):
        mail = _valid_mail()
        mail.credential.domain = domain
        _validate_mail_config(mail)


def test_enterprise_provider_allows_custom_domain():
    mail = _valid_mail()
    mail.credential.provider = "tencent_exmail"
    mail.credential.domain = "mycompany.com"
    _validate_mail_config(mail)


def test_enterprise_provider_rejects_malformed_domain():
    for bad_domain in (
        "",
        "nodot",
        "bad domain.com",
        "foo..com",
        "-bad.com",
        "evil.com;rm",
    ):
        mail = _valid_mail()
        mail.credential.provider = "aliyun_qiye"
        mail.credential.domain = bad_domain
        with pytest.raises(HTTPException) as exc_info:
            _validate_mail_config(mail)
        assert exc_info.value.status_code == 400


def test_enterprise_provider_rejects_whitelisted_domain():
    """Well-known domains must not carry an enterprise provider."""
    for domain in ("163.com", "gmail.com", "exmail.qq.com"):
        mail = _valid_mail()
        mail.credential.provider = "tencent_exmail"
        mail.credential.domain = domain
        with pytest.raises(HTTPException) as exc_info:
            _validate_mail_config(mail)
        assert exc_info.value.status_code == 400
        assert "well-known domain" in exc_info.value.detail


def test_invalid_provider_rejected():
    mail = _valid_mail()
    mail.credential.provider = "unknown_provider"
    with pytest.raises(HTTPException) as exc_info:
        _validate_mail_config(mail)
    assert exc_info.value.status_code == 400
    assert "provider" in exc_info.value.detail


def test_microsoft_domains_rejected_with_oauth2_reason():
    for domain in (
        "outlook.com",
        "hotmail.com",
        "live.com",
        "msn.com",
        "office365.com",
    ):
        mail = _valid_mail()
        mail.credential.domain = domain
        with pytest.raises(HTTPException) as exc_info:
            _validate_mail_config(mail)
        assert exc_info.value.status_code == 400
        assert "OAuth2" in exc_info.value.detail


def test_env_injects_hosts_for_enterprise_provider(tmp_path):
    mail = _valid_mail()
    mail.credential.provider = "netease_qiye"
    mail.credential.domain = "mycompany.com"
    env = _build_qwenpawmail_env(mail, tmp_path)
    assert env["QWENPAWMAIL_EMAIL"] == "tester@mycompany.com"
    assert env["QWENPAWMAIL_IMAP_HOST"] == "imap.qiye.163.com"
    assert env["QWENPAWMAIL_IMAP_PORT"] == "993"
    assert env["QWENPAWMAIL_SMTP_HOST"] == "smtp.qiye.163.com"
    # NetEase enterprise SMTP SSL port is 994, not 465.
    assert env["QWENPAWMAIL_SMTP_PORT"] == "994"


def test_env_injects_tencent_exmail_hosts(tmp_path):
    mail = _valid_mail()
    mail.credential.provider = "tencent_exmail"
    mail.credential.domain = "mycompany.com"
    env = _build_qwenpawmail_env(mail, tmp_path)
    assert env["QWENPAWMAIL_IMAP_HOST"] == "imap.exmail.qq.com"
    assert env["QWENPAWMAIL_IMAP_PORT"] == "993"
    assert env["QWENPAWMAIL_SMTP_HOST"] == "smtp.exmail.qq.com"
    assert env["QWENPAWMAIL_SMTP_PORT"] == "465"


def test_env_without_provider_has_no_host_overrides(tmp_path):
    env = _build_qwenpawmail_env(_valid_mail(), tmp_path)
    assert env["QWENPAWMAIL_EMAIL"] == "tester@163.com"
    assert "QWENPAWMAIL_IMAP_HOST" not in env
    assert "QWENPAWMAIL_IMAP_PORT" not in env
    assert "QWENPAWMAIL_SMTP_HOST" not in env
    assert "QWENPAWMAIL_SMTP_PORT" not in env


def test_env_injects_workspace_and_state_dirs(tmp_path):
    env = _build_qwenpawmail_env(_valid_mail(), tmp_path)
    assert env["QWENPAWMAIL_STATE_DIR"] == str(tmp_path / "mail_state")
    assert env["QWENPAWMAIL_WORKSPACE_DIR"] == str(tmp_path)


def test_env_without_workspace_dir_has_no_dir_vars():
    env = _build_qwenpawmail_env(_valid_mail())
    assert "QWENPAWMAIL_STATE_DIR" not in env
    assert "QWENPAWMAIL_WORKSPACE_DIR" not in env


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


def test_aliyun_domain_accepts_non_16_char_auth_code():
    """aliyun.com uses login password which is not 16 chars."""
    mail = AgentMailConfig(
        is_new_account=False,
        credential=AgentMailCredential(
            name="tester",
            domain="aliyun.com",
            auth_code="my_login_password_123",
            password="",
            phone_number="",
        ),
    )
    _validate_mail_config(mail)


def test_enterprise_provider_accepts_non_16_char_auth_code():
    """Enterprise mail providers use login/client passwords (non-16 chars)."""
    for provider in ("tencent_exmail", "aliyun_qiye", "netease_qiye"):
        mail = AgentMailConfig(
            is_new_account=False,
            credential=AgentMailCredential(
                name="tester",
                domain="mycompany.com",
                auth_code="enterprise_pwd_8",
                password="",
                phone_number="",
                provider=provider,
            ),
        )
        _validate_mail_config(mail)


def test_aliyun_domain_rejects_empty_auth_code():
    """aliyun.com still requires a non-empty auth_code."""
    mail = AgentMailConfig(
        is_new_account=False,
        credential=AgentMailCredential(
            name="tester",
            domain="aliyun.com",
            auth_code="",
            password="",
            phone_number="",
        ),
    )
    with pytest.raises(HTTPException) as exc_info:
        _validate_mail_config(mail)
    assert exc_info.value.status_code == 400
    assert "auth_code" in exc_info.value.detail


def test_personal_mail_without_password_phone_passes():
    """Personal mailbox only needs name + auth_code, not password/phone."""
    mail = AgentMailConfig(
        is_new_account=False,
        credential=AgentMailCredential(
            name="tester",
            domain="163.com",
            auth_code="a" * 16,
            password="",
            phone_number="",
        ),
    )
    # Should not raise
    _validate_mail_config(mail)


def test_personal_mail_without_name_rejected():
    """Personal mailbox still requires credential name."""
    mail = AgentMailConfig(
        is_new_account=False,
        credential=AgentMailCredential(
            name="",
            domain="163.com",
            auth_code="a" * 16,
            password="",
            phone_number="",
        ),
    )
    with pytest.raises(HTTPException) as exc_info:
        _validate_mail_config(mail)
    assert exc_info.value.status_code == 400
    assert "credential name" in exc_info.value.detail


# ── MAIL_TRIAGE.md seed distribution ──────────────────────────────


def test_ensure_mail_triage_file_copies_seed(tmp_path):
    _ensure_mail_triage_file(tmp_path, "zh")
    target = tmp_path / "MAIL_TRIAGE.md"
    assert target.is_file()
    text = target.read_text("utf-8")
    assert "邮件分诊树" in text
    assert "F1 探索处理" in text


def test_ensure_mail_triage_file_skips_existing(tmp_path):
    target = tmp_path / "MAIL_TRIAGE.md"
    target.write_text("user grown tree", "utf-8")
    _ensure_mail_triage_file(tmp_path, "zh")
    assert target.read_text("utf-8") == "user grown tree"


def test_ensure_mail_triage_file_falls_back_to_en(tmp_path):
    # Unsupported language normalizes to en; en also carries the seed.
    _ensure_mail_triage_file(tmp_path, "fr")
    assert (tmp_path / "MAIL_TRIAGE.md").is_file()
