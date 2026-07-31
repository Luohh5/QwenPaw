# -*- coding: utf-8 -*-
# pylint: disable=redefined-outer-name,protected-access
"""Unit tests for the mail push monitor (rule matching, mode branches)."""
from __future__ import annotations

import asyncio
import email as email_lib
import imaplib
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from qwenpaw.config.config import (
    AgentMailConfig,
    AgentMailCredential,
    AgentMailPushConfig,
    AgentMailPushRule,
)
from qwenpaw.app.mail.monitor import (
    MailMonitorService,
    build_wake_prompt,
    build_wake_trace,
    encode_folder,
    extract_body_preview,
    match_rules,
    resolve_idle_timeout,
    resolve_imap_host,
    rule_matches,
    should_wake_agent,
)


# ── test doubles ─────────────────────────────────────────────────────


class EventRecorder:
    """Async stand-in for inbox_store.append_event."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    async def __call__(self, **kwargs):
        self.events.append(kwargs)
        return kwargs

    def types(self) -> list[str]:
        return [event["event_type"] for event in self.events]


class FakeImapConn:
    """Records IMAP commands issued by the monitor."""

    def __init__(self, uids: bytes = b"") -> None:
        self.calls: list[tuple] = []
        self.search_result = uids
        self.search_typ = "OK"
        self.fetch_typ = "OK"
        self.create_typ = "OK"
        self.create_detail = b"CREATE completed"
        self.created: list[str] = []
        self.body_bytes: bytes | None = None
        self.header_bytes = (
            b"From: alice@example.com\r\n"
            b"Subject: hello\r\n"
            b"Date: Tue, 28 Jul 2026 10:00:00 +0800\r\n\r\n"
        )

    def uid(self, command, *args):
        self.calls.append((command, *args))
        if command == "SEARCH":
            return (self.search_typ, [self.search_result])
        if command == "FETCH":
            if self.fetch_typ != "OK":
                return (self.fetch_typ, [b"FETCH failed"])
            spec = args[1] if len(args) > 1 else ""
            if "HEADER.FIELDS" not in spec and self.body_bytes is not None:
                return ("OK", [(b"1 (BODY[])", self.body_bytes), b")"])
            return ("OK", [(b"1 (BODY[])", self.header_bytes), b")"])
        return ("OK", [b""])

    def create(self, folder):
        self.created.append(folder)
        return (self.create_typ, [self.create_detail])

    def expunge(self):
        self.calls.append(("EXPUNGE",))
        return ("OK", [b""])

    def commands(self) -> list[str]:
        return [call[0] for call in self.calls]


class FakeWorkspace:
    """Workspace stub exposing workspace_dir + stream_query."""

    def __init__(self, workspace_dir: Path) -> None:
        self.workspace_dir = workspace_dir
        self.queries: list[dict] = []

    async def stream_query(self, req):
        self.queries.append(req)
        yield {"type": "done"}


class FakeSession:
    """Session stub backing read_session_messages."""

    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def get_session_state_dict(self, *_args, **_kwargs):
        return {"agent": {"state": {"context": list(self.messages)}}}


class FakeWorkspaceWithSession(FakeWorkspace):
    """Workspace whose stream_query appends session messages."""

    def __init__(self, workspace_dir: Path) -> None:
        super().__init__(workspace_dir)
        self.session = FakeSession()
        self.run_messages: list[dict] = []

    async def stream_query(self, req):
        self.queries.append(req)
        self.session.messages.extend(self.run_messages)
        yield {"type": "done"}


def _mail_config(
    mode: str = "rules_only",
    rules: list[AgentMailPushRule] | None = None,
) -> AgentMailConfig:
    return AgentMailConfig(
        is_new_account=False,
        credential=AgentMailCredential(
            name="tester",
            domain="163.com",
            auth_code="a" * 16,
            password="pw",
            phone_number="13800000000",
        ),
        push=AgentMailPushConfig(mode=mode, rules=rules or []),
    )


@pytest.fixture
def recorder():
    rec = EventRecorder()
    with patch(
        "qwenpaw.app.mail.monitor.append_inbox_event",
        new=rec,
    ):
        yield rec


def _service(
    tmp_path: Path,
    mode: str = "rules_only",
    rules: list[AgentMailPushRule] | None = None,
) -> tuple[MailMonitorService, FakeWorkspace]:
    workspace = FakeWorkspace(tmp_path)
    service = MailMonitorService(
        agent_id="test-agent",
        workspace=workspace,
        mail_config=_mail_config(mode, rules),
    )
    return service, workspace


async def _run_pipeline(
    service: MailMonitorService,
    conn: FakeImapConn,
    uid: int = 5,
    sender: str = "alice@example.com",
    subject: str = "hello",
) -> None:
    """Run the sync pipeline off-loop like the worker thread does."""
    service._loop = asyncio.get_running_loop()
    envelope = {"sender": sender, "subject": subject, "date": "now"}
    await asyncio.to_thread(
        service._process_new_email,
        conn,
        uid,
        envelope,
    )


# ── rule matching ─────────────────────────────────────────────────────


def test_rule_matches_from_case_insensitive():
    rule = AgentMailPushRule(field="from", contains="ALICE")
    assert rule_matches(rule, "alice@example.com", "whatever")
    assert not rule_matches(rule, "bob@example.com", "alice in subject")


def test_rule_matches_subject_alias_matches_subject_and_body():
    # "subject" is a legacy alias of "content": subject + body.
    rule = AgentMailPushRule(field="subject", contains="invoice")
    assert rule_matches(rule, "x@y.z", "Your INVOICE #42")
    assert rule_matches(rule, "x@y.z", "hello", "see the Invoice here")
    assert not rule_matches(rule, "invoice@y.z", "hello")


def test_rule_matches_content_hits_body_not_subject():
    rule = AgentMailPushRule(field="content", contains="refund")
    assert rule_matches(rule, "x@y.z", "hello", "please REFUND me")
    assert rule_matches(rule, "x@y.z", "Refund request", "")
    assert not rule_matches(rule, "refund@y.z", "hello", "nothing")


def test_rule_matches_content_empty_body_degrades_to_subject():
    # Failed body fetch yields "": subject-only matching, no error.
    rule = AgentMailPushRule(field="content", contains="invoice")
    assert rule_matches(rule, "x@y.z", "Your invoice", "")
    assert not rule_matches(rule, "x@y.z", "hello", "")


def test_rule_field_subject_deserializes_from_legacy_config():
    # Existing agent.json entries with field="subject" stay valid.
    rule = AgentMailPushRule.model_validate(
        {"field": "subject", "contains": "x", "action": "notify"},
    )
    assert rule.field == "subject"


def test_rule_matches_keyword_matches_all_fields():
    rule = AgentMailPushRule(field="keyword", contains="bank")
    assert rule_matches(rule, "no-reply@bank.com", "hello")
    assert rule_matches(rule, "x@y.z", "Bank statement")
    assert rule_matches(rule, "x@y.z", "hello", "from your BANK")
    assert not rule_matches(rule, "x@y.z", "hello", "nothing")


def test_rule_empty_contains_never_matches():
    rule = AgentMailPushRule(field="keyword", contains="  ")
    assert not rule_matches(rule, "a@b.c", "anything")


def test_match_rules_preserves_order():
    rules = [
        AgentMailPushRule(field="from", contains="alice"),
        AgentMailPushRule(field="subject", contains="none"),
        AgentMailPushRule(field="keyword", contains="hello"),
        AgentMailPushRule(field="content", contains="body text"),
    ]
    matched = match_rules(
        rules,
        "alice@example.com",
        "hello",
        "some body text",
    )
    assert matched == [rules[0], rules[2], rules[3]]


# ── wake decision ─────────────────────────────────────────────────────


def test_should_wake_rules_only_never():
    rule = AgentMailPushRule(action="wake_agent", contains="x")
    assert not should_wake_agent("rules_only", [rule])
    assert not should_wake_agent("rules_only", [])


def test_should_wake_agent_all_always():
    assert should_wake_agent("agent_all", [])
    assert should_wake_agent(
        "agent_all",
        [AgentMailPushRule(action="mark_read", contains="x")],
    )


def test_should_wake_rules_then_agent_branches():
    wake = AgentMailPushRule(action="wake_agent", contains="x")
    mark = AgentMailPushRule(action="mark_read", contains="x")
    # No rule matched -> wake.
    assert should_wake_agent("rules_then_agent", [])
    # Matched wake_agent -> wake.
    assert should_wake_agent("rules_then_agent", [mark, wake])
    # Matched non-wake rules only -> no wake.
    assert not should_wake_agent("rules_then_agent", [mark])


def test_should_wake_off_never():
    assert not should_wake_agent("off", [])


# ── host routing ──────────────────────────────────────────────────────


def test_resolve_imap_host_table():
    assert resolve_imap_host("163.com") == "imap.163.com"
    assert resolve_imap_host("foxmail.com") == "imap.qq.com"
    assert resolve_imap_host("unknown.example") is None
    # New personal / enterprise domains.
    assert resolve_imap_host("sina.com") == "imap.sina.com"
    assert resolve_imap_host("sina.cn") == "imap.sina.cn"
    assert resolve_imap_host("aliyun.com") == "imap.aliyun.com"
    assert resolve_imap_host("gmail.com") == "imap.gmail.com"
    assert resolve_imap_host("exmail.qq.com") == "imap.exmail.qq.com"
    assert resolve_imap_host("qiye.aliyun.com") == "imap.qiye.aliyun.com"
    assert resolve_imap_host("qiye.163.com") == "imap.qiye.163.com"


def test_resolve_imap_host_by_provider():
    # A non-empty provider takes precedence over the domain table,
    # enabling custom enterprise domains.
    assert (
        resolve_imap_host("mycompany.com", "tencent_exmail")
        == "imap.exmail.qq.com"
    )
    assert (
        resolve_imap_host("mycompany.com", "aliyun_qiye")
        == "imap.qiye.aliyun.com"
    )
    assert (
        resolve_imap_host("mycompany.com", "netease_qiye")
        == "imap.qiye.163.com"
    )
    # Unknown provider -> None (skip monitoring).
    assert resolve_imap_host("163.com", "bogus_provider") is None
    # Empty provider falls back to the domain table.
    assert resolve_imap_host("163.com", "") == "imap.163.com"


def test_resolve_idle_timeout_by_domain():
    # QQ family servers do not reliably push EXISTS while idling, so
    # the IDLE timeout doubles as the polling cadence -> 2 minutes.
    assert resolve_idle_timeout("qq.com") == 2 * 60
    assert resolve_idle_timeout("foxmail.com") == 2 * 60
    assert resolve_idle_timeout(" QQ.COM ") == 2 * 60
    # Tencent enterprise mail shares the QQ family push behaviour.
    assert resolve_idle_timeout("exmail.qq.com") == 2 * 60
    # NetEase family and unknown domains keep the 25 minute default.
    assert resolve_idle_timeout("163.com") == 25 * 60
    assert resolve_idle_timeout("unknown.example") == 25 * 60
    assert resolve_idle_timeout("") == 25 * 60
    # New domains use the 25 minute default too.
    assert resolve_idle_timeout("gmail.com") == 25 * 60
    assert resolve_idle_timeout("qiye.aliyun.com") == 25 * 60
    assert resolve_idle_timeout("qiye.163.com") == 25 * 60


def test_resolve_idle_timeout_by_provider():
    # tencent_exmail with a custom domain keeps the 2 minute cadence.
    assert resolve_idle_timeout("mycompany.com", "tencent_exmail") == 2 * 60
    # Other providers keep the 25 minute default.
    assert resolve_idle_timeout("mycompany.com", "aliyun_qiye") == 25 * 60
    assert resolve_idle_timeout("mycompany.com", "netease_qiye") == 25 * 60
    # Empty provider falls back to the domain lookup.
    assert resolve_idle_timeout("qq.com", "") == 2 * 60


# ── pipeline: deterministic actions ──────────────────────────────────


async def test_pipeline_always_emits_new_email_event(tmp_path, recorder):
    service, workspace = _service(tmp_path, mode="rules_only")
    conn = FakeImapConn()
    await _run_pipeline(service, conn)
    assert recorder.types() == ["new_email"]
    event = recorder.events[0]
    assert event["source_type"] == "mail"
    assert event["payload"]["uid"] == 5
    assert workspace.queries == []


async def test_pipeline_mark_read_action(tmp_path, recorder):
    rules = [AgentMailPushRule(field="from", contains="alice",
                               action="mark_read")]
    service, _ = _service(tmp_path, mode="rules_only", rules=rules)
    conn = FakeImapConn()
    await _run_pipeline(service, conn)
    assert ("STORE", "5", "+FLAGS", r"(\Seen)") in conn.calls
    assert recorder.types() == ["new_email"]


async def test_pipeline_move_action(tmp_path, recorder):
    rules = [AgentMailPushRule(field="subject", contains="hello",
                               action="move", param="Archive")]
    service, _ = _service(tmp_path, mode="rules_only", rules=rules)
    conn = FakeImapConn()
    await _run_pipeline(service, conn)
    assert conn.created == ['"Archive"']
    assert ("COPY", "5", encode_folder("Archive")) in conn.calls
    assert ("STORE", "5", "+FLAGS", r"(\Deleted)") in conn.calls
    assert ("EXPUNGE",) in conn.calls


async def test_pipeline_move_creates_chinese_folder(tmp_path, recorder):
    rules = [AgentMailPushRule(field="subject", contains="hello",
                               action="move", param="归档")]
    service, _ = _service(tmp_path, mode="rules_only", rules=rules)
    conn = FakeImapConn()
    await _run_pipeline(service, conn)
    # Chinese folder names are CREATEd in IMAP modified UTF-7.
    assert conn.created == ['"&X1JoYw-"']
    assert ("COPY", "5", encode_folder("归档")) in conn.calls


async def test_pipeline_move_ignores_already_exists(tmp_path, recorder):
    rules = [AgentMailPushRule(field="subject", contains="hello",
                               action="move", param="Archive")]
    service, _ = _service(tmp_path, mode="rules_only", rules=rules)
    conn = FakeImapConn()
    conn.create_typ = "NO"
    conn.create_detail = b"[ALREADYEXISTS] Mailbox already exists"
    await _run_pipeline(service, conn)
    # "already exists" is not an error: the move still runs.
    assert ("COPY", "5", encode_folder("Archive")) in conn.calls
    assert ("EXPUNGE",) in conn.calls


async def test_pipeline_move_skipped_on_create_failure(
    tmp_path,
    recorder,
):
    rules = [AgentMailPushRule(field="subject", contains="hello",
                               action="move", param="Archive")]
    service, _ = _service(tmp_path, mode="rules_only", rules=rules)
    conn = FakeImapConn()
    conn.create_typ = "NO"
    conn.create_detail = b"[NOPERM] Permission denied"
    await _run_pipeline(service, conn)
    # Move skipped, pipeline not interrupted: new_email still emitted.
    assert ("COPY", "5", "Archive") not in conn.calls
    assert ("EXPUNGE",) not in conn.calls
    assert recorder.types() == ["new_email"]


async def test_pipeline_notify_action_appends_extra_event(
    tmp_path,
    recorder,
):
    rules = [AgentMailPushRule(field="keyword", contains="hello",
                               action="notify")]
    service, _ = _service(tmp_path, mode="rules_only", rules=rules)
    await _run_pipeline(service, FakeImapConn())
    # One rule-notify event + one unconditional new_email event.
    assert recorder.types() == ["new_email", "new_email"]
    assert recorder.events[0]["payload"]["rule_action"] == "notify"


# ── pipeline: mode branches ───────────────────────────────────────────


async def test_rules_then_agent_wakes_when_no_rule_matches(
    tmp_path,
    recorder,
):
    rules = [AgentMailPushRule(field="from", contains="nobody",
                               action="mark_read")]
    service, workspace = _service(
        tmp_path,
        mode="rules_then_agent",
        rules=rules,
    )
    await _run_pipeline(service, FakeImapConn())
    assert len(workspace.queries) == 1
    assert recorder.types() == ["new_email", "auto_handled"]
    assert recorder.events[1]["status"] == "success"


async def test_rules_then_agent_no_wake_when_rule_handles(
    tmp_path,
    recorder,
):
    rules = [AgentMailPushRule(field="from", contains="alice",
                               action="mark_read")]
    service, workspace = _service(
        tmp_path,
        mode="rules_then_agent",
        rules=rules,
    )
    await _run_pipeline(service, FakeImapConn())
    assert workspace.queries == []
    assert recorder.types() == ["new_email"]


async def test_rules_then_agent_wake_agent_action_param(
    tmp_path,
    recorder,
):
    rules = [AgentMailPushRule(field="from", contains="alice",
                               action="wake_agent",
                               param="转发给我微信")]
    service, workspace = _service(
        tmp_path,
        mode="rules_then_agent",
        rules=rules,
    )
    await _run_pipeline(service, FakeImapConn())
    assert len(workspace.queries) == 1
    prompt = workspace.queries[0]["input"][0]["content"][0]["text"]
    assert "转发给我微信" in prompt
    assert "CONTACTS.md" in prompt
    assert recorder.types() == ["new_email", "auto_handled"]


async def test_agent_all_always_wakes(tmp_path, recorder):
    rules = [AgentMailPushRule(field="from", contains="alice",
                               action="mark_read")]
    service, workspace = _service(tmp_path, mode="agent_all", rules=rules)
    await _run_pipeline(service, FakeImapConn())
    assert len(workspace.queries) == 1
    assert recorder.types() == ["new_email", "auto_handled"]


# ── new-mail detection & state persistence ───────────────────────────


async def test_check_new_messages_baseline_first_run(tmp_path, recorder):
    service, _ = _service(tmp_path, mode="rules_only")
    conn = FakeImapConn(uids=b"1 2 3")
    service._loop = asyncio.get_running_loop()
    await asyncio.to_thread(service._check_new_messages, conn)
    # First run only records the baseline; no events are emitted.
    assert recorder.events == []
    state = json.loads(
        (tmp_path / "mail_state" / "monitor.json").read_text("utf-8"),
    )
    assert state["last_uid"] == 3


async def test_check_new_messages_processes_new_uids(tmp_path, recorder):
    service, _ = _service(tmp_path, mode="rules_only")
    service._last_uid = 3
    conn = FakeImapConn(uids=b"1 2 3 4 5")
    service._loop = asyncio.get_running_loop()
    await asyncio.to_thread(service._check_new_messages, conn)
    assert recorder.types() == ["new_email", "new_email"]
    uids = [event["payload"]["uid"] for event in recorder.events]
    assert uids == [4, 5]
    state = json.loads(
        (tmp_path / "mail_state" / "monitor.json").read_text("utf-8"),
    )
    assert state["last_uid"] == 5


def test_state_round_trip(tmp_path):
    service, _ = _service(tmp_path)
    service._last_uid = 42
    service._save_state()
    fresh, _ = _service(tmp_path)
    fresh._load_state()
    assert fresh._last_uid == 42


def test_state_round_trip_with_uidvalidity(tmp_path):
    service, _ = _service(tmp_path)
    service._last_uid = 42
    service._current_uidvalidity = 1234
    service._save_state()
    state = json.loads(
        (tmp_path / "mail_state" / "monitor.json").read_text("utf-8"),
    )
    assert state == {"last_uid": 42, "uidvalidity": 1234}
    fresh, _ = _service(tmp_path)
    fresh._load_state()
    assert fresh._last_uid == 42
    assert fresh._stored_uidvalidity == 1234


# ── UIDVALIDITY reconciliation ───────────────────────────────────


def test_reconcile_keeps_baseline_when_uidvalidity_matches(tmp_path):
    service, _ = _service(tmp_path)
    service._last_uid = 42
    service._stored_uidvalidity = 1234
    service._current_uidvalidity = 1234
    service._reconcile_uidvalidity()
    assert service._last_uid == 42
    assert service._stored_uidvalidity == 1234


def test_reconcile_resets_baseline_on_uidvalidity_change(tmp_path):
    service, _ = _service(tmp_path)
    service._last_uid = 42
    service._stored_uidvalidity = 1234
    service._current_uidvalidity = 5678
    service._reconcile_uidvalidity()
    assert service._last_uid is None
    assert service._stored_uidvalidity == 5678


@pytest.mark.parametrize("stored,current", [
    (None, 5678),
    (1234, None),
    (None, None),
])
def test_reconcile_resets_baseline_when_not_comparable(
    tmp_path,
    stored,
    current,
):
    service, _ = _service(tmp_path)
    service._last_uid = 42
    service._stored_uidvalidity = stored
    service._current_uidvalidity = current
    service._reconcile_uidvalidity()
    assert service._last_uid is None
    assert service._stored_uidvalidity == current


async def test_uidvalidity_reset_rebaselines_without_processing(
    tmp_path,
    recorder,
):
    """After a reset the next check only re-baselines, no history."""
    state_dir = tmp_path / "mail_state"
    state_dir.mkdir(parents=True)
    (state_dir / "monitor.json").write_text(
        json.dumps({"last_uid": 900, "uidvalidity": 1234}),
        "utf-8",
    )
    service, _ = _service(tmp_path, mode="rules_only")
    service._load_state()
    assert service._last_uid == 900
    # Simulate _connect observing a different UIDVALIDITY.
    service._current_uidvalidity = 5678
    service._reconcile_uidvalidity()
    conn = FakeImapConn(uids=b"1 2 3")
    service._loop = asyncio.get_running_loop()
    await asyncio.to_thread(service._check_new_messages, conn)
    # New UIDs 1..3 (all below the stale 900) were NOT filtered out:
    # the baseline was reset, so this behaves like a first run.
    assert recorder.events == []
    state = json.loads(
        (state_dir / "monitor.json").read_text("utf-8"),
    )
    assert state == {"last_uid": 3, "uidvalidity": 5678}


# ── IMAP response typ defence ────────────────────────────────────


def test_search_uids_raises_on_bad_typ(tmp_path):
    service, _ = _service(tmp_path)
    conn = FakeImapConn(uids=b"1 2")
    conn.search_typ = "NO"
    with pytest.raises(imaplib.IMAP4.error, match="UID SEARCH failed"):
        service._search_uids(conn)


def test_search_uids_raises_on_unparsable_uids(tmp_path):
    service, _ = _service(tmp_path)
    conn = FakeImapConn(uids=b"1 garbage 3")
    with pytest.raises(imaplib.IMAP4.error, match="unparsable"):
        service._search_uids(conn)


def test_fetch_envelope_raises_on_bad_typ(tmp_path):
    service, _ = _service(tmp_path)
    conn = FakeImapConn()
    conn.fetch_typ = "NO"
    with pytest.raises(imaplib.IMAP4.error, match="UID FETCH"):
        service._fetch_envelope(conn, 5)


# ── wake prompt ───────────────────────────────────────────────────────


def test_build_wake_prompt_contains_envelope_fields():
    prompt = build_wake_prompt(
        sender="a@b.c",
        subject="hi",
        date="today",
        uid=7,
        param="",
    )
    assert "a@b.c" in prompt
    assert "hi" in prompt
    assert "uid：7" in prompt
    assert "INBOX" in prompt
    assert "reply_message" in prompt


# ── folder name encoding ──────────────────────────────────────────────────


def test_encode_folder_ascii_passthrough():
    assert encode_folder("Archive") == '"Archive"'


def test_encode_folder_chinese_modified_utf7():
    assert encode_folder("归档") == '"&X1JoYw-"'


def test_encode_folder_ampersand_escape():
    assert encode_folder("A&B") == '"A&-B"'


# ── body preview extraction ────────────────────────────────────────────


def _message(raw: bytes):
    return email_lib.message_from_bytes(raw)


def test_extract_body_preview_prefers_text_plain():
    raw = (
        b"Content-Type: multipart/alternative; boundary=XX\r\n\r\n"
        b"--XX\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
        b"plain body\r\n"
        b"--XX\r\n"
        b"Content-Type: text/html; charset=utf-8\r\n\r\n"
        b"<p>html body</p>\r\n"
        b"--XX--\r\n"
    )
    assert extract_body_preview(_message(raw)) == "plain body"


def test_extract_body_preview_strips_html_fallback():
    raw = (
        b"Content-Type: text/html; charset=utf-8\r\n\r\n"
        b"<html><style>p {color: red}</style>"
        b"<p>Hello&nbsp;<b>World</b></p></html>\r\n"
    )
    assert extract_body_preview(_message(raw)) == "Hello World"


def test_extract_body_preview_bad_charset_defensive():
    raw = (
        b"Content-Type: text/plain; charset=x-no-such-charset\r\n\r\n"
        b"caf\xe9 body\r\n"
    )
    # Unknown charset falls back to utf-8 with replacement chars.
    preview = extract_body_preview(_message(raw))
    assert preview.startswith("caf")
    assert "body" in preview


def test_extract_body_preview_decode_failure_empty():
    class BrokenPart:
        def is_multipart(self):
            return False

        def get(self, _name):
            return None

        def get_content_type(self):
            return "text/plain"

        def get_payload(self, decode=False):
            raise RuntimeError("boom")

    assert extract_body_preview(BrokenPart()) == ""


def test_extract_body_preview_truncates_2000():
    raw = (
        b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
        + b"x" * 3000
    )
    preview = extract_body_preview(_message(raw))
    assert len(preview) == 2000
    assert preview == "x" * 2000


def test_extract_body_preview_skips_attachments():
    raw = (
        b"Content-Type: multipart/mixed; boundary=XX\r\n\r\n"
        b"--XX\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"Content-Disposition: attachment; filename=a.txt\r\n\r\n"
        b"attachment text\r\n"
        b"--XX\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
        b"real body\r\n"
        b"--XX--\r\n"
    )
    assert extract_body_preview(_message(raw)) == "real body"


# ── body preview in the pipeline ────────────────────────────────────


async def test_new_email_event_includes_body_preview(tmp_path, recorder):
    service, _ = _service(tmp_path, mode="rules_only")
    conn = FakeImapConn()
    conn.body_bytes = (
        b"From: alice@example.com\r\n"
        b"Subject: hello\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
        b"the mail body\r\n"
    )
    await _run_pipeline(service, conn)
    assert recorder.types() == ["new_email"]
    payload = recorder.events[0]["payload"]
    assert payload["body_preview"] == "the mail body"
    # Preview came from a single bounded BODY.PEEK fetch.
    fetches = [c for c in conn.calls if c[0] == "FETCH"]
    assert len(fetches) == 1
    assert "BODY.PEEK[]<0." in fetches[0][2]


async def test_body_preview_empty_on_fetch_failure(tmp_path, recorder):
    service, _ = _service(tmp_path, mode="rules_only")
    conn = FakeImapConn()
    conn.fetch_typ = "NO"
    await _run_pipeline(service, conn)
    # Event delivery is unaffected; preview degrades to "".
    assert recorder.types() == ["new_email"]
    assert recorder.events[0]["payload"]["body_preview"] == ""


# ── auto_handled body + payload.trace ────────────────────────────────────


def _text_msg(role: str, text: str) -> dict:
    return {"role": role, "content": [{"type": "text", "text": text}]}


def _service_with_session(
    tmp_path: Path,
) -> tuple[MailMonitorService, FakeWorkspaceWithSession]:
    workspace = FakeWorkspaceWithSession(tmp_path)
    service = MailMonitorService(
        agent_id="test-agent",
        workspace=workspace,
        mail_config=_mail_config("agent_all", []),
    )
    return service, workspace


async def test_auto_handled_body_from_delta_last_text(
    tmp_path,
    recorder,
):
    service, workspace = _service_with_session(tmp_path)
    workspace.session.messages = [_text_msg("assistant", "old baseline")]
    workspace.run_messages = [
        _text_msg("user", "wake prompt (must not leak into trace)"),
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "name": "reply_message",
                    "input": {"to": "alice@example.com"},
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "output": [{"type": "text", "text": "sent ok"}],
                },
            ],
        },
        _text_msg("assistant", "已回复 Alice 的邮件。"),
    ]
    await _run_pipeline(service, FakeImapConn())
    assert recorder.types() == ["new_email", "auto_handled"]
    event = recorder.events[1]
    assert event["status"] == "success"
    # body = last effective text from the delta, not the old
    # hard-coded sentence.
    assert event["body"] == "已回复 Alice 的邮件。"
    trace = event["payload"]["trace"]
    assert trace == [
        {
            "type": "tool_call",
            "name": "reply_message",
            "summary": '{"to": "alice@example.com"} => sent ok',
        },
        {"type": "text", "summary": "已回复 Alice 的邮件。"},
    ]
    # Pre-existing payload fields are preserved.
    payload = event["payload"]
    assert payload["uid"] == 5
    assert payload["from"] == "alice@example.com"
    assert payload["subject"] == "hello"
    assert payload["folder"] == "INBOX"
    assert payload["mode"] == "agent_all"


async def test_auto_handled_body_falls_back_without_delta(
    tmp_path,
    recorder,
):
    # Plain FakeWorkspace has no .session: delta extraction yields
    # nothing and the body falls back to the hard-coded sentence.
    service, _ = _service(tmp_path, mode="agent_all")
    await _run_pipeline(service, FakeImapConn())
    assert recorder.types() == ["new_email", "auto_handled"]
    event = recorder.events[1]
    assert event["body"] == (
        "Agent processed new email from alice@example.com."
    )
    assert event["payload"]["trace"] == []


async def test_auto_handled_body_truncated_to_500(tmp_path, recorder):
    service, workspace = _service_with_session(tmp_path)
    workspace.run_messages = [_text_msg("assistant", "x" * 800)]
    await _run_pipeline(service, FakeImapConn())
    event = recorder.events[1]
    assert event["event_type"] == "auto_handled"
    assert event["body"] == "x" * 500


def test_build_wake_trace_skips_user_text():
    delta = [
        _text_msg("user", "the wake prompt"),
        _text_msg("assistant", "the answer"),
    ]
    assert build_wake_trace(delta) == [
        {"type": "text", "summary": "the answer"},
    ]


def test_build_wake_trace_truncates_summaries_to_200():
    delta = [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "name": "t",
                    "input": {"arg": "a" * 300},
                },
                {"type": "text", "text": "b" * 300},
            ],
        },
    ]
    trace = build_wake_trace(delta)
    assert len(trace) == 2
    assert len(trace[0]["summary"]) == 200
    assert trace[1]["summary"] == "b" * 200


def test_build_wake_trace_caps_entry_count():
    delta = [_text_msg("assistant", f"step {i}") for i in range(80)]
    assert len(build_wake_trace(delta)) == 50
    assert len(build_wake_trace(delta, max_entries=7)) == 7


def test_build_wake_trace_orphan_tool_result_becomes_text():
    delta = [
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "output": [{"type": "text", "text": "orphan"}],
                },
            ],
        },
    ]
    assert build_wake_trace(delta) == [
        {"type": "text", "summary": "orphan"},
    ]


def test_build_wake_trace_ignores_malformed_entries():
    delta = [
        "not a dict",
        {"role": "assistant", "content": "not a list"},
        {"role": "assistant", "content": ["not a dict", {"type": "?"}]},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "input": None}],
        },
    ]
    # Unknown/malformed blocks are skipped; a nameless tool_use still
    # yields a tool_call entry with an empty summary.
    assert build_wake_trace(delta) == [
        {"type": "tool_call", "summary": ""},
    ]


# ── body extraction: thinking excluded + fallback chain ──────────────


def _thinking_block(text: str) -> dict:
    return {"type": "thinking", "thinking": text}


def _tool_use_block(name: str, tool_id: str, arg: str) -> dict:
    return {
        "type": "tool_use",
        "id": tool_id,
        "name": name,
        "input": {"arg": arg},
    }


def _tool_result_msg(tool_id: str, text: str) -> dict:
    return {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "id": tool_id,
                "output": [{"type": "text", "text": text}],
            },
        ],
    }


async def test_auto_handled_body_skips_thinking(tmp_path, recorder):
    # The delta ends with an assistant message whose content mixes a
    # long thinking block with the final text: body must contain only
    # the text, never the internal reasoning.
    service, workspace = _service_with_session(tmp_path)
    workspace.run_messages = [
        _text_msg("user", "wake prompt"),
        {
            "role": "assistant",
            "content": [
                _thinking_block("The user is notifying me " * 40),
                _tool_use_block("get_message", "t1", "uid 5"),
            ],
        },
        _tool_result_msg("t1", "mail content"),
        {
            "role": "assistant",
            "content": [
                _thinking_block("internal reasoning again"),
                {"type": "text", "text": "✅ 处理完成摘要"},
            ],
        },
    ]
    await _run_pipeline(service, FakeImapConn())
    event = recorder.events[1]
    assert event["event_type"] == "auto_handled"
    assert event["body"] == "✅ 处理完成摘要"
    assert "notifying" not in event["body"]
    assert "reasoning" not in event["body"]


async def test_auto_handled_body_joins_last_message_text_blocks(
    tmp_path,
    recorder,
):
    service, workspace = _service_with_session(tmp_path)
    workspace.run_messages = [
        _text_msg("assistant", "earlier text"),
        {
            "role": "assistant",
            "content": [
                _thinking_block("skip me"),
                {"type": "text", "text": "part one"},
                {"type": "text", "text": "part two"},
            ],
        },
    ]
    await _run_pipeline(service, FakeImapConn())
    # Only the LAST assistant message with text is used; its text
    # blocks are joined.
    assert recorder.events[1]["body"] == "part one\npart two"


async def test_auto_handled_body_falls_back_to_tool_result(
    tmp_path,
    recorder,
):
    # No assistant text block at all: body falls back to the last
    # tool_result text.
    service, workspace = _service_with_session(tmp_path)
    workspace.run_messages = [
        {
            "role": "assistant",
            "content": [
                _thinking_block("only thinking"),
                _tool_use_block("get_message", "t1", "uid 5"),
            ],
        },
        _tool_result_msg("t1", "first result"),
        _tool_result_msg("t1", "last result"),
    ]
    await _run_pipeline(service, FakeImapConn())
    assert recorder.events[1]["body"] == "last result"


async def test_auto_handled_body_falls_back_hardcoded(
    tmp_path,
    recorder,
):
    # Neither text nor tool_result text in the delta: hard-coded
    # sentence remains the final fallback.
    service, workspace = _service_with_session(tmp_path)
    workspace.run_messages = [
        {
            "role": "assistant",
            "content": [
                _thinking_block("only thinking"),
                _tool_use_block("get_message", "t1", "uid 5"),
            ],
        },
    ]
    await _run_pipeline(service, FakeImapConn())
    assert recorder.events[1]["body"] == (
        "Agent processed new email from alice@example.com."
    )


# ── trace: tool_result pairing by id ────────────────────────────


def test_build_wake_trace_pairs_out_of_order_results_by_id():
    # Real-world async wake: two tool_use blocks are emitted before
    # either result arrives; results come back in call order but AFTER
    # the second call, so index-based pairing would mismatch them.
    delta = [
        {
            "role": "assistant",
            "content": [
                _thinking_block("long internal reasoning"),
                _tool_use_block("get_message", "a", "uid 5"),
            ],
        },
        {
            "role": "assistant",
            "content": [_tool_use_block("read_file", "b", "c.md")],
        },
        _tool_result_msg("a", "mail body"),
        _tool_result_msg("b", "contacts file"),
        _text_msg("assistant", "邮件摘要"),
        {
            "role": "assistant",
            "content": [_tool_use_block("edit_file", "c", "c.md")],
        },
        _tool_result_msg("c", "edited"),
        _text_msg("assistant", "✅ 处理完成摘要"),
    ]
    assert build_wake_trace(delta) == [
        {
            "type": "tool_call",
            "name": "get_message",
            "summary": '{"arg": "uid 5"} => mail body',
        },
        {
            "type": "tool_call",
            "name": "read_file",
            "summary": '{"arg": "c.md"} => contacts file',
        },
        {"type": "text", "summary": "邮件摘要"},
        {
            "type": "tool_call",
            "name": "edit_file",
            "summary": '{"arg": "c.md"} => edited',
        },
        {"type": "text", "summary": "✅ 处理完成摘要"},
    ]


def test_build_wake_trace_result_via_tool_use_id_field():
    # Anthropic-style blocks reference the call via ``tool_use_id``.
    delta = [
        {
            "role": "assistant",
            "content": [_tool_use_block("t", "x1", "v")],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "x1",
                    "output": [{"type": "text", "text": "ok"}],
                },
            ],
        },
    ]
    assert build_wake_trace(delta) == [
        {
            "type": "tool_call",
            "name": "t",
            "summary": '{"arg": "v"} => ok',
        },
    ]


def test_build_wake_trace_orphan_result_with_unknown_id():
    # A result whose id matches no pending call stays a standalone
    # entry; it must NOT be merged into an unrelated call.
    delta = [
        {
            "role": "assistant",
            "content": [_tool_use_block("t", "known", "v")],
        },
        _tool_result_msg("unknown", "orphan result"),
        _tool_result_msg("known", "real result"),
    ]
    assert build_wake_trace(delta) == [
        {
            "type": "tool_call",
            "name": "t",
            "summary": '{"arg": "v"} => real result',
        },
        {"type": "text", "summary": "orphan result"},
    ]


def test_build_wake_trace_duplicate_result_id_second_is_orphan():
    delta = [
        {
            "role": "assistant",
            "content": [_tool_use_block("t", "a", "v")],
        },
        _tool_result_msg("a", "first"),
        _tool_result_msg("a", "second"),
    ]
    # The first result consumes the pending call; the duplicate
    # becomes a standalone orphan entry.
    assert build_wake_trace(delta) == [
        {
            "type": "tool_call",
            "name": "t",
            "summary": '{"arg": "v"} => first',
        },
        {"type": "text", "summary": "second"},
    ]
