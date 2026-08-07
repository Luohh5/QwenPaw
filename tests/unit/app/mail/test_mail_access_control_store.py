# -*- coding: utf-8 -*-
"""Unit tests for MailAccessControlStore persistence and ACL semantics."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from qwenpaw.app.mail.mail_access_control import (
    MailAccessControlStore,
    validate_acl_address,
)

AGENT = "agent-1"


def _store(tmp_path: Path) -> MailAccessControlStore:
    return MailAccessControlStore(tmp_path / "mail_access_control.json")


# ── Whitelist / blacklist CRUD ──────────────────────────────────────


def test_whitelist_add_and_remove(tmp_path):
    store = _store(tmp_path)
    store.add_to_whitelist(AGENT, "Alice@Example.com", remark="friend")
    acl = store.get_acl(AGENT)
    assert "alice@example.com" in acl["whitelist"]
    assert acl["whitelist"]["alice@example.com"]["remark"] == "friend"

    store.remove_from_whitelist(AGENT, "alice@example.com")
    assert "alice@example.com" not in store.get_acl(AGENT)["whitelist"]


def test_blacklist_add_and_remove(tmp_path):
    store = _store(tmp_path)
    store.add_to_blacklist(AGENT, "spam@example.com")
    assert "spam@example.com" in store.get_acl(AGENT)["blacklist"]

    store.remove_from_blacklist(AGENT, "spam@example.com")
    assert "spam@example.com" not in store.get_acl(AGENT)["blacklist"]


def test_whitelist_add_removes_from_blacklist_and_pending(tmp_path):
    store = _store(tmp_path)
    store.add_to_blacklist(AGENT, "bob@example.com")
    store.add_pending(AGENT, "bob@example.com")
    store.add_to_whitelist(AGENT, "bob@example.com")
    acl = store.get_acl(AGENT)
    assert "bob@example.com" in acl["whitelist"]
    assert "bob@example.com" not in acl["blacklist"]
    assert acl["pending"] == []


# ── check_sender precedence ─────────────────────────────────────────


def test_check_sender_levels(tmp_path):
    store = _store(tmp_path)
    store.add_to_whitelist(AGENT, "alice@example.com")
    store.add_to_blacklist(AGENT, "spam@example.com")
    store.add_to_whitelist(AGENT, "*@good.com")
    store.add_to_blacklist(AGENT, "*@bad.com")

    assert store.check_sender(AGENT, "alice@example.com") == "allow"
    assert store.check_sender(AGENT, "spam@example.com") == "deny"
    assert store.check_sender(AGENT, "anyone@good.com") == "allow"
    assert store.check_sender(AGENT, "anyone@bad.com") == "deny"
    assert store.check_sender(AGENT, "stranger@other.com") == "unknown"
    assert store.check_sender("no-such-agent", "x@y.com") == "unknown"


def test_exact_blacklist_beats_domain_whitelist(tmp_path):
    store = _store(tmp_path)
    store.add_to_whitelist(AGENT, "*@corp.com")
    store.add_to_blacklist(AGENT, "bad@corp.com")
    assert store.check_sender(AGENT, "bad@corp.com") == "deny"
    assert store.check_sender(AGENT, "good@corp.com") == "allow"


def test_pending_sender_reported_as_pending(tmp_path):
    store = _store(tmp_path)
    store.add_pending(AGENT, "new@example.com", subject="hi")
    assert store.check_sender(AGENT, "new@example.com") == "pending"


# ── Wildcard validation ─────────────────────────────────────────────


def test_invalid_wildcard_rejected(tmp_path):
    store = _store(tmp_path)
    for bad in ("*@*", "*@", "*@bad domain", "*@-bad.com", "*@nodot"):
        with pytest.raises(ValueError):
            store.add_to_whitelist(AGENT, bad)
        with pytest.raises(ValueError):
            store.add_to_blacklist(AGENT, bad)
    assert store.get_acl(AGENT)["whitelist"] == {}


def test_validate_acl_address():
    validate_acl_address("user@example.com")
    validate_acl_address("*@example.com")
    for bad in ("", "nodot", "user@nodot", "two@@example.com ", "*@*"):
        with pytest.raises(ValueError):
            validate_acl_address(bad)


# ── Persistence ─────────────────────────────────────────────────────


def test_persistence_round_trip(tmp_path):
    path = tmp_path / "mail_access_control.json"
    store = MailAccessControlStore(path)
    store.add_to_whitelist(AGENT, "alice@example.com", remark="friend")
    store.add_to_blacklist(AGENT, "*@bad.com")
    store.add_pending(AGENT, "new@example.com", subject="hello", uid=7)

    reloaded = MailAccessControlStore(path)
    acl = reloaded.get_acl(AGENT)
    assert acl["whitelist"]["alice@example.com"]["remark"] == "friend"
    assert "*@bad.com" in acl["blacklist"]
    assert acl["pending"][0]["sender_address"] == "new@example.com"
    assert acl["pending"][0]["uid"] == 7
    # Domain wildcard caches must be rebuilt from disk as well.
    assert reloaded.check_sender(AGENT, "x@bad.com") == "deny"


def test_corrupted_file_does_not_crash_and_reloads_after_repair(tmp_path):
    path = tmp_path / "mail_access_control.json"
    path.write_text("{not valid json", encoding="utf-8")

    store = MailAccessControlStore(path)  # must not raise
    assert store.check_sender(AGENT, "alice@example.com") == "unknown"

    # Repair the file on disk; the store must pick it up via
    # _reload_if_stale because a failed parse never updates the mtime.
    path.write_text(
        json.dumps(
            {
                AGENT: {
                    "whitelist": {"alice@example.com": {"remark": ""}},
                    "blacklist": {},
                    "pending": [],
                },
            },
        ),
        encoding="utf-8",
    )
    assert store.check_sender(AGENT, "alice@example.com") == "allow"


# ── Pending queue ───────────────────────────────────────────────────


def test_pending_deduplicates(tmp_path):
    store = _store(tmp_path)
    store.add_pending(AGENT, "new@example.com", subject="first")
    store.add_pending(AGENT, "new@example.com", subject="second")
    pending = store.get_acl(AGENT)["pending"]
    assert len(pending) == 1
    assert pending[0]["subject"] == "first"
    assert store.get_pending_count() == 1


def test_pending_max_limit_evicts_oldest(tmp_path):
    store = _store(tmp_path)
    store._MAX_PENDING = 3  # keep the test fast
    for i in range(4):
        store.add_pending(AGENT, f"user{i}@example.com")
    pending = store.get_acl(AGENT)["pending"]
    assert len(pending) == 3
    addresses = {p["sender_address"] for p in pending}
    assert "user0@example.com" not in addresses
    assert "user3@example.com" in addresses


def test_approve_pending_moves_to_whitelist(tmp_path):
    store = _store(tmp_path)
    store.add_pending(AGENT, "new@example.com", display_name="New Guy")
    assert store.approve_pending(AGENT, "new@example.com", remark="ok")
    acl = store.get_acl(AGENT)
    assert acl["pending"] == []
    assert acl["whitelist"]["new@example.com"]["remark"] == "ok"
    assert acl["whitelist"]["new@example.com"]["display_name"] == "New Guy"


def test_deny_pending_moves_to_blacklist(tmp_path):
    store = _store(tmp_path)
    store.add_pending(AGENT, "new@example.com")
    assert store.deny_pending(AGENT, "new@example.com")
    acl = store.get_acl(AGENT)
    assert acl["pending"] == []
    assert "new@example.com" in acl["blacklist"]


def test_dismiss_pending(tmp_path):
    store = _store(tmp_path)
    store.add_pending(AGENT, "new@example.com")
    assert store.dismiss_pending(AGENT, "new@example.com") is True
    assert store.dismiss_pending(AGENT, "new@example.com") is False
    acl = store.get_acl(AGENT)
    assert acl["pending"] == []
    assert acl["whitelist"] == {}
    assert acl["blacklist"] == {}


def test_update_remark(tmp_path):
    store = _store(tmp_path)
    store.add_to_whitelist(AGENT, "alice@example.com")
    assert store.update_remark(AGENT, "alice@example.com", "bestie") is True
    acl = store.get_acl(AGENT)
    assert acl["whitelist"]["alice@example.com"]["remark"] == "bestie"
    assert store.update_remark(AGENT, "ghost@example.com", "x") is False
