# -*- coding: utf-8 -*-
"""Unit tests for the WeCom tool-guard approval card.

Covers the truncation helper, the button-key payload guard, both card
builders, the ``template_card_event`` parser, the outbound render path
(stream detail then card) and the inbound click handling (resolved card
update plus ``/approval`` command re-injection).
The channel and its SDK client are stubbed, so nothing hits the network.
"""
# pylint: disable=protected-access,redefined-outer-name,use-implicit-booleaness-not-comparison  # noqa: E501
from __future__ import annotations

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "plugins/channel/wecom"))

import asyncio
import json

import pytest

from qwenpaw_channel_wecom.cards import tool_guard as tg


class _StubClient:
    """Stand-in for the aibot SDK client used by ``WecomChannel``."""

    def __init__(self, *, card_error=None, update_error=None):
        self.cards = []
        self.updates = []
        self.streams = []
        self._card_error = card_error
        self._update_error = update_error

    async def reply_template_card(self, frame, card):
        if self._card_error is not None:
            raise self._card_error
        self.cards.append({"frame": frame, "card": card})

    async def update_template_card(self, frame, card):
        if self._update_error is not None:
            raise self._update_error
        self.updates.append({"frame": frame, "card": card})

    async def reply_stream(self, frame, *, stream_id, content, finish):
        self.streams.append(
            {
                "frame": frame,
                "stream_id": stream_id,
                "content": content,
                "finish": finish,
            },
        )


class _StubChannel:
    """Minimal stand-in for ``WecomChannel`` used by the card helpers."""

    channel = "wecom"

    def __init__(self, *, enabled=True, client=True, enqueue=True):
        self.enabled = enabled
        self._client = _StubClient() if client else None
        self._keepalive_tasks = {}
        self.enqueued = []
        self._enqueue = self.enqueued.append if enqueue else None


def _event(content="please approve"):
    class _Msg:
        pass

    msg = _Msg()
    msg.content = content
    return msg


def _send_meta(**overrides):
    meta = {
        "wecom_frame": {"id": "frame-1"},
        "wecom_sender_id": "sender-1",
        "wecom_chatid": "chat-1",
        "wecom_chat_type": "single",
    }
    meta.update(overrides)
    return meta


def _button_key(action="approve", request_id="rid-12345678", **extra):
    payload = {
        "a": action,
        "rid": request_id,
        "tool": "shell",
        "sev": "medium",
    }
    payload.update(extra)
    return json.dumps(payload, separators=(",", ":"))


def _frame(event_key=None, task_id="tg_approval_rid-12345678", **extra):
    body = {
        "event": {
            "template_card_event": {
                "event_key": (
                    _button_key() if event_key is None else event_key
                ),
                "task_id": task_id,
            },
        },
        "from": {"userid": "user-abc"},
    }
    body.update(extra)
    return {"body": body}


# ---------------------------------------------------------------------
# Helpers / builders
# ---------------------------------------------------------------------


class TestTruncate:
    @pytest.mark.parametrize("text", ["", None])
    def test_empty_input(self, text):
        assert tg._truncate(text, 10) == ""

    def test_short_text_is_untouched(self):
        assert tg._truncate("abc", 3) == "abc"

    def test_long_text_is_cut_with_ellipsis(self):
        assert tg._truncate("abcdef", 4) == "abc…"
        assert len(tg._truncate("abcdef", 4)) == 4

    def test_limit_of_one_keeps_only_the_ellipsis(self):
        assert tg._truncate("abcdef", 1) == "…"


class TestBuildButtonKey:
    def test_payload_is_compact_and_keyed(self):
        raw = tg._build_button_key(
            "approve",
            "rid-1",
            "shell",
            "high",
            {"chatid": "c1"},
        )

        assert json.loads(raw) == {
            "a": "approve",
            "rid": "rid-1",
            "tool": "shell",
            "sev": "high",
            "chatid": "c1",
        }
        assert ", " not in raw and '": "' not in raw

    def test_non_ascii_context_is_preserved(self):
        raw = tg._build_button_key("deny", "rid", "tool", "low", {"n": "泰哥"})

        assert "泰哥" in raw

    def test_oversized_payload_is_rejected(self):
        with pytest.raises(ValueError) as excinfo:
            tg._build_button_key(
                "approve",
                "rid-1",
                "x" * 1200,
                "medium",
                {},
            )

        assert "too large" in str(excinfo.value)
        assert "1024" in str(excinfo.value)

    def test_payload_at_the_limit_is_accepted(self):
        """Just under the byte budget must not raise."""
        # 1024 - overhead of the fixed keys, filled with ASCII padding.
        overhead = len(
            tg._build_button_key("approve", "rid", "", "medium", {}),
        )
        raw = tg._build_button_key(
            "approve",
            "rid",
            "t" * (1024 - overhead),
            "medium",
            {},
        )

        assert len(raw.encode("utf-8")) <= 1024

    def test_multibyte_context_counts_bytes_not_chars(self):
        with pytest.raises(ValueError):
            tg._build_button_key(
                "approve",
                "rid",
                "泰" * 400,
                "medium",
                {},
            )


class TestBuildApprovalCard:
    def test_card_shape(self):
        card = tg.build_approval_card(
            request_id="rid-1",
            tool_name="shell",
            severity="HIGH",
        )

        assert card["card_type"] == "button_interaction"
        assert card["task_id"] == "tg_approval_rid-1"
        assert card["main_title"] == {
            "title": "🛡️ Tool Approval Required",
            "desc": "shell | high",
        }

    def test_button_list_lives_at_the_root(self):
        """``card_action`` must not wrap the buttons (WeCom rejects it)."""
        card = tg.build_approval_card(
            request_id="rid-1",
            tool_name="shell",
            severity="medium",
        )

        assert "card_action" not in card
        assert [button["text"] for button in card["button_list"]] == [
            "Approve",
            "Deny",
        ]
        assert [button["style"] for button in card["button_list"]] == [1, 2]

    def test_buttons_carry_the_callback_payload(self):
        card = tg.build_approval_card(
            request_id="rid-1",
            tool_name="shell",
            severity="medium",
            session_ctx={"chatid": "chat-1"},
        )

        for button, expected in zip(card["button_list"], ("approve", "deny")):
            payload = json.loads(button["key"])
            assert payload["a"] == expected
            assert payload["rid"] == "rid-1"
            assert payload["chatid"] == "chat-1"

    @pytest.mark.parametrize("severity", ["", None])
    def test_blank_severity_falls_back_to_medium(self, severity):
        card = tg.build_approval_card(
            request_id="rid-1",
            tool_name="shell",
            severity=severity,
        )

        assert card["main_title"]["desc"] == "shell | medium"

    def test_oversized_tool_name_raises(self):
        with pytest.raises(ValueError):
            tg.build_approval_card(
                request_id="rid-1",
                tool_name="x" * 2000,
                severity="medium",
            )


class TestBuildResolvedCard:
    def test_approved_card(self):
        card = tg.build_resolved_card(
            task_id="task-1",
            tool_name="shell",
            action="approve",
            operator_display="泰哥",
        )

        assert card["card_type"] == "text_notice"
        assert card["task_id"] == "task-1"
        assert card["main_title"]["title"] == "✅ Approved"
        assert card["main_title"]["desc"] == "by 泰哥\nshell"
        assert card["card_action"]["type"] == 1

    def test_denied_card(self):
        card = tg.build_resolved_card(
            task_id="task-1",
            tool_name="shell",
            action="deny",
        )

        assert card["main_title"]["title"] == "🚫 Denied"
        assert card["main_title"]["desc"] == "\nshell"

    def test_unknown_action_reports_expiry(self):
        card = tg.build_resolved_card(
            task_id="task-1",
            tool_name="shell",
            action="whatever",
        )

        assert card["main_title"]["title"] == "⌛ Expired"
        assert card["main_title"]["desc"] == (
            "Approval for shell has expired."
        )

    def test_long_fields_are_truncated(self):
        card = tg.build_resolved_card(
            task_id="task-1",
            tool_name="t" * 200,
            action="approve",
            operator_display="o" * 200,
        )

        assert len(card["main_title"]["title"]) <= 36
        assert len(card["main_title"]["desc"]) <= 44

    def test_card_action_url_is_present(self):
        card = tg.build_resolved_card(
            task_id="task-1",
            tool_name="shell",
            action="approve",
        )

        assert card["card_action"]["url"] == tg._RESOLVED_CARD_URL


# ---------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------


class TestParseCardEvent:
    def test_nested_template_card_event(self):
        parsed = tg.parse_card_event(_frame()["body"])

        assert parsed == {
            "action": "approve",
            "request_id": "rid-12345678",
            "task_id": "tg_approval_rid-12345678",
            "tool_name": "shell",
            "severity": "medium",
            "session_ctx": {},
            "user_id": "user-abc",
        }

    def test_event_block_without_card_wrapper_is_accepted(self):
        """``event.template_card_event`` may be absent; ``event`` is used."""
        body = {
            "event": {
                "event_key": _button_key(action="deny"),
                "task_id": "task-9",
            },
            "from": {"userid": "u-1"},
        }

        parsed = tg.parse_card_event(body)

        assert parsed["action"] == "deny"
        assert parsed["task_id"] == "task-9"
        assert parsed["user_id"] == "u-1"

    def test_session_ctx_drops_guard_keys(self):
        parsed = tg.parse_card_event(
            _frame(event_key=_button_key(chatid="chat-1"))["body"],
        )

        assert parsed["session_ctx"] == {"chatid": "chat-1"}

    def test_missing_severity_defaults_to_medium(self):
        payload = json.loads(_button_key())
        del payload["sev"]
        parsed = tg.parse_card_event(
            _frame(event_key=json.dumps(payload))["body"],
        )

        assert parsed["severity"] == "medium"

    def test_missing_from_block_yields_empty_user(self):
        body = _frame()["body"]
        del body["from"]

        assert tg.parse_card_event(body)["user_id"] == ""

    @pytest.mark.parametrize(
        "body",
        [
            {},
            {"event": {}},
            {"event": {"template_card_event": {}}},
            {"event_key": ""},
            {"event": {"template_card_event": {"event_key": None}}},
        ],
    )
    def test_bodies_without_a_key_are_ignored(self, body):
        assert tg.parse_card_event(body) is None

    @pytest.mark.parametrize("raw", ["not-json", "{", '{"a":'])
    def test_malformed_key_is_ignored(self, raw):
        assert tg.parse_card_event(_frame(event_key=raw)["body"]) is None

    @pytest.mark.parametrize("action", ["", "retry", "APPROVE"])
    def test_unknown_actions_are_ignored(self, action):
        raw = _button_key(action=action)

        assert tg.parse_card_event(_frame(event_key=raw)["body"]) is None

    def test_non_string_ids_are_coerced(self):
        payload = json.loads(_button_key())
        payload["rid"] = 99
        parsed = tg.parse_card_event(
            _frame(event_key=json.dumps(payload))["body"],
        )

        assert parsed["request_id"] == "99"


# ---------------------------------------------------------------------
# render
# ---------------------------------------------------------------------
