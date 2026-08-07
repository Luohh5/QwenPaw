# -*- coding: utf-8 -*-
"""Mail access control store for per-agent sender
whitelist/blacklist management.

Persists per-agent mail ACL (whitelist, blacklist, pending approval) entries
to a JSON file under the working directory.  Supports domain-wildcard entries
(e.g. ``*@example.com``) for bulk allow/deny by domain.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from ...constant import WORKING_DIR

logger = logging.getLogger(__name__)

MAIL_ACCESS_CONTROL_FILE = "mail_access_control.json"

# Regex for validating domain part after *@
_DOMAIN_RE = re.compile(
    r"^[a-zA-Z0-9]([a-zA-Z0-9\-]*[a-zA-Z0-9])?"
    r"(\.[a-zA-Z0-9]([a-zA-Z0-9\-]*[a-zA-Z0-9])?)+$"
)


class MailPendingEntry:
    """A sender who emailed the agent but is not yet on any list."""

    __slots__ = (
        "sender_address",
        "agent_id",
        "display_name",
        "subject",
        "body_preview",
        "timestamp",
        "remark",
        "uid",
        "date",
    )

    def __init__(
        self,
        sender_address: str,
        agent_id: str,
        display_name: str = "",
        subject: str = "",
        body_preview: str = "",
        timestamp: float = 0.0,
        remark: str = "",
        uid: int = 0,
        date: str = "",
    ):
        self.sender_address = sender_address
        self.agent_id = agent_id
        self.display_name = display_name
        self.subject = subject
        self.body_preview = body_preview
        self.timestamp = timestamp
        self.remark = remark
        self.uid = uid
        self.date = date

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sender_address": self.sender_address,
            "agent_id": self.agent_id,
            "display_name": self.display_name,
            "subject": self.subject,
            "body_preview": self.body_preview,
            "timestamp": self.timestamp,
            "remark": self.remark,
            "uid": self.uid,
            "date": self.date,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> MailPendingEntry:
        return cls(
            sender_address=data.get("sender_address", ""),
            agent_id=data.get("agent_id", ""),
            display_name=data.get("display_name", ""),
            subject=data.get("subject", ""),
            body_preview=data.get("body_preview", ""),
            timestamp=data.get("timestamp", 0.0),
            remark=data.get("remark", ""),
            uid=data.get("uid", 0),
            date=data.get("date", ""),
        )


class MailUserInfo:
    """Per-address metadata stored in whitelist/blacklist."""

    __slots__ = ("remark", "display_name")

    def __init__(self, remark: str = "", display_name: str = ""):
        self.remark = remark
        self.display_name = display_name

    def to_dict(self) -> Dict[str, str]:
        return {"remark": self.remark, "display_name": self.display_name}

    @classmethod
    def from_dict(cls, data: Any) -> MailUserInfo:
        if isinstance(data, dict):
            return cls(
                remark=str(data.get("remark", "")),
                display_name=str(data.get("display_name", "")),
            )
        return cls(remark=str(data) if data else "")


class AgentMailACL:
    """Access control data for a single agent's mail."""

    def __init__(
        self,
        whitelist: Optional[Dict[str, MailUserInfo]] = None,
        blacklist: Optional[Dict[str, MailUserInfo]] = None,
        pending: Optional[List[MailPendingEntry]] = None,
    ):
        self.whitelist: Dict[str, MailUserInfo] = whitelist or {}
        self.blacklist: Dict[str, MailUserInfo] = blacklist or {}
        self.pending: List[MailPendingEntry] = pending or []

    def to_dict(self) -> Dict[str, Any]:
        return {
            "whitelist": {k: v.to_dict() for k, v in self.whitelist.items()},
            "blacklist": {k: v.to_dict() for k, v in self.blacklist.items()},
            "pending": [p.to_dict() for p in self.pending],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> AgentMailACL:
        whitelist: Dict[str, MailUserInfo] = {}
        for k, v in data.get("whitelist", {}).items():
            whitelist[k] = MailUserInfo.from_dict(v)
        blacklist: Dict[str, MailUserInfo] = {}
        for k, v in data.get("blacklist", {}).items():
            blacklist[k] = MailUserInfo.from_dict(v)
        pending = [
            MailPendingEntry.from_dict(p) for p in data.get("pending", [])
        ]
        return cls(whitelist=whitelist, blacklist=blacklist, pending=pending)


class MailAccessControlStore:
    """Thread-safe persistent store for per-agent mail access control lists."""

    _MAX_PENDING = 500

    def __init__(self, path: Optional[Path] = None):
        self._path = path or WORKING_DIR / MAIL_ACCESS_CONTROL_FILE
        self._lock = threading.Lock()
        self._data: Dict[str, AgentMailACL] = {}
        self._last_mtime: float = 0.0
        # Domain wildcard caches
        self._domain_whitelist: Dict[str, Set[str]] = {}
        self._domain_blacklist: Dict[str, Set[str]] = {}
        self._load()

    # ── Persistence ─────────────────────────────────────────────────────

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            self._last_mtime = self._path.stat().st_mtime
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            self._data = {k: AgentMailACL.from_dict(v) for k, v in raw.items()}
            self._rebuild_domain_sets()
        except Exception:
            logger.exception(
                "Failed to load mail access control data from %s",
                self._path,
            )

    def _reload_if_stale(self) -> None:
        """Reload from disk if the file was updated since last load."""
        try:
            if not self._path.exists():
                return
            current_mtime = self._path.stat().st_mtime
            if current_mtime > self._last_mtime:
                self._load()
        except OSError:
            pass

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = {k: v.to_dict() for k, v in self._data.items()}
            self._path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            self._last_mtime = self._path.stat().st_mtime
            self._rebuild_domain_sets()
        except Exception:
            logger.exception(
                "Failed to save mail access control data to %s",
                self._path,
            )

    def _acl(self, agent_id: str) -> AgentMailACL:
        if agent_id not in self._data:
            self._data[agent_id] = AgentMailACL()
        return self._data[agent_id]

    def _rebuild_domain_sets(self) -> None:
        """Rebuild domain wildcard caches from current data."""
        dw: Dict[str, Set[str]] = {}
        db: Dict[str, Set[str]] = {}
        for agent_id, acl in self._data.items():
            wset: Set[str] = set()
            for addr in acl.whitelist:
                if addr.startswith("*@"):
                    domain = addr[2:].lower()
                    wset.add(domain)
            if wset:
                dw[agent_id] = wset

            bset: Set[str] = set()
            for addr in acl.blacklist:
                if addr.startswith("*@"):
                    domain = addr[2:].lower()
                    bset.add(domain)
            if bset:
                db[agent_id] = bset
        self._domain_whitelist = dw
        self._domain_blacklist = db

    # ── Query ───────────────────────────────────────────────────────────

    def check_sender(self, agent_id: str, sender_email: str) -> str:
        """Check sender status.

        Returns "allow", "deny", "pending", or "unknown".
        """
        with self._lock:
            self._reload_if_stale()
            acl = self._data.get(agent_id)
            if acl is None:
                return "unknown"

            sender_lower = sender_email.lower().strip()

            # 1. Check pending
            for entry in acl.pending:
                if entry.sender_address == sender_lower:
                    return "pending"

            # 2. Exact whitelist match
            if sender_lower in acl.whitelist:
                return "allow"

            # 3. Exact blacklist match
            if sender_lower in acl.blacklist:
                return "deny"

            # 4. Domain whitelist
            domain = self._extract_domain(sender_lower)
            if domain:
                wset = self._domain_whitelist.get(agent_id)
                if wset and domain in wset:
                    return "allow"

                # 5. Domain blacklist
                bset = self._domain_blacklist.get(agent_id)
                if bset and domain in bset:
                    return "deny"

            # 6. Unknown
            return "unknown"

    @staticmethod
    def _extract_domain(email: str) -> str:
        """Extract domain from an email address."""
        at_idx = email.rfind("@")
        if at_idx < 0:
            return ""
        return email[at_idx + 1 :]

    def get_acl(self, agent_id: str) -> Dict[str, Any]:
        with self._lock:
            self._reload_if_stale()
            return self._acl(agent_id).to_dict()

    def get_all_acls(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            self._reload_if_stale()
            return {k: v.to_dict() for k, v in self._data.items()}

    # ── Whitelist ───────────────────────────────────────────────────────

    def add_to_whitelist(
        self,
        agent_id: str,
        address: str,
        remark: str = "",
        display_name: str = "",
    ) -> None:
        address = address.lower().strip()
        self._validate_wildcard(address)
        with self._lock:
            acl = self._acl(agent_id)
            existing = acl.whitelist.get(address)
            acl.whitelist[address] = MailUserInfo(
                remark=remark or (existing.remark if existing else ""),
                display_name=display_name
                or (existing.display_name if existing else ""),
            )
            acl.blacklist.pop(address, None)
            acl.pending = [
                p for p in acl.pending if p.sender_address != address
            ]
            self._save()

    def remove_from_whitelist(self, agent_id: str, address: str) -> None:
        address = address.lower().strip()
        with self._lock:
            self._acl(agent_id).whitelist.pop(address, None)
            self._save()

    # ── Blacklist ───────────────────────────────────────────────────────

    def add_to_blacklist(
        self,
        agent_id: str,
        address: str,
        remark: str = "",
        display_name: str = "",
    ) -> None:
        address = address.lower().strip()
        self._validate_wildcard(address)
        with self._lock:
            acl = self._acl(agent_id)
            existing = acl.blacklist.get(address)
            acl.blacklist[address] = MailUserInfo(
                remark=remark or (existing.remark if existing else ""),
                display_name=display_name
                or (existing.display_name if existing else ""),
            )
            acl.whitelist.pop(address, None)
            acl.pending = [
                p for p in acl.pending if p.sender_address != address
            ]
            self._save()

    def remove_from_blacklist(self, agent_id: str, address: str) -> None:
        address = address.lower().strip()
        with self._lock:
            self._acl(agent_id).blacklist.pop(address, None)
            self._save()

    # ── Pending ─────────────────────────────────────────────────────────

    def add_pending(
        self,
        agent_id: str,
        sender_address: str,
        display_name: str = "",
        subject: str = "",
        body_preview: str = "",
        uid: int = 0,
        date: str = "",
    ) -> None:
        sender_address = sender_address.lower().strip()
        with self._lock:
            acl = self._acl(agent_id)
            # Deduplicate
            for existing in acl.pending:
                if existing.sender_address == sender_address:
                    return
            # Enforce max pending limit
            if len(acl.pending) >= self._MAX_PENDING:
                acl.pending.sort(key=lambda p: p.timestamp)
                acl.pending.pop(0)
            acl.pending.append(
                MailPendingEntry(
                    sender_address=sender_address,
                    agent_id=agent_id,
                    display_name=display_name[:200],
                    subject=subject[:200],
                    body_preview=body_preview[:500],
                    timestamp=time.time(),
                    uid=uid,
                    date=date,
                ),
            )
            self._save()

    def get_pending_entry(
        self,
        agent_id: str,
        sender_address: str,
    ) -> Optional[Dict[str, Any]]:
        """Return the pending entry dict for a sender, or None."""
        sender_address = sender_address.lower().strip()
        with self._lock:
            self._reload_if_stale()
            for entry in self._acl(agent_id).pending:
                if entry.sender_address == sender_address:
                    return entry.to_dict()
            return None

    def get_all_pending(self) -> List[Dict[str, Any]]:
        with self._lock:
            self._reload_if_stale()
            result: List[Dict[str, Any]] = []
            for acl in self._data.values():
                result.extend(p.to_dict() for p in acl.pending)
            result.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
            return result

    def get_pending_count(self) -> int:
        with self._lock:
            self._reload_if_stale()
            return sum(len(acl.pending) for acl in self._data.values())

    def approve_pending(
        self,
        agent_id: str,
        sender_address: str,
        remark: str = "",
    ) -> bool:
        """Move a pending sender to the whitelist."""
        sender_address = sender_address.lower().strip()
        with self._lock:
            acl = self._acl(agent_id)
            effective_remark = remark
            display_name = ""
            for entry in acl.pending:
                if entry.sender_address == sender_address:
                    if not effective_remark:
                        effective_remark = entry.remark
                    display_name = entry.display_name
                    break
            acl.pending = [
                p for p in acl.pending if p.sender_address != sender_address
            ]
            acl.whitelist[sender_address] = MailUserInfo(
                remark=effective_remark,
                display_name=display_name,
            )
            acl.blacklist.pop(sender_address, None)
            self._save()
            return True

    def deny_pending(
        self,
        agent_id: str,
        sender_address: str,
        remark: str = "",
    ) -> bool:
        """Move a pending sender to the blacklist."""
        sender_address = sender_address.lower().strip()
        with self._lock:
            acl = self._acl(agent_id)
            effective_remark = remark
            display_name = ""
            for entry in acl.pending:
                if entry.sender_address == sender_address:
                    if not effective_remark:
                        effective_remark = entry.remark
                    display_name = entry.display_name
                    break
            acl.pending = [
                p for p in acl.pending if p.sender_address != sender_address
            ]
            acl.blacklist[sender_address] = MailUserInfo(
                remark=effective_remark,
                display_name=display_name,
            )
            acl.whitelist.pop(sender_address, None)
            self._save()
            return True

    def dismiss_pending(self, agent_id: str, sender_address: str) -> bool:
        """Remove from pending without adding to any list."""
        sender_address = sender_address.lower().strip()
        with self._lock:
            acl = self._acl(agent_id)
            before = len(acl.pending)
            acl.pending = [
                p for p in acl.pending if p.sender_address != sender_address
            ]
            if len(acl.pending) < before:
                self._save()
                return True
            return False

    def update_pending_remark(
        self,
        agent_id: str,
        sender_address: str,
        remark: str,
    ) -> bool:
        """Update the remark on a pending entry."""
        sender_address = sender_address.lower().strip()
        with self._lock:
            acl = self._acl(agent_id)
            for entry in acl.pending:
                if entry.sender_address == sender_address:
                    entry.remark = remark
                    self._save()
                    return True
            return False

    def update_remark(
        self,
        agent_id: str,
        address: str,
        remark: str,
    ) -> bool:
        """Update the remark for an address in whitelist or blacklist."""
        address = address.lower().strip()
        with self._lock:
            acl = self._acl(agent_id)
            if address in acl.whitelist:
                acl.whitelist[address].remark = remark
                self._save()
                return True
            if address in acl.blacklist:
                acl.blacklist[address].remark = remark
                self._save()
                return True
            return False

    # ── Validation helpers ──────────────────────────────────────────────

    @staticmethod
    def _validate_wildcard(address: str) -> None:
        """Validate wildcard address format."""
        if not address.startswith("*@"):
            return
        domain = address[2:]
        if not domain or domain == "*":
            raise ValueError(
                f"Invalid wildcard address {address!r}: "
                "domain must be a valid domain name, '*@*' is not allowed."
            )
        if not _DOMAIN_RE.match(domain):
            raise ValueError(
                f"Invalid wildcard address {address!r}: "
                f"{domain!r} is not a valid domain format."
            )


# Per-workspace store registry keyed by resolved workspace directory path.
_stores: Dict[str, MailAccessControlStore] = {}
_stores_lock = threading.Lock()


def get_mail_access_control_store(
    workspace_dir: Optional[Path] = None,
) -> MailAccessControlStore:
    """Get (or create) the MailAccessControlStore for a workspace.

    Args:
        workspace_dir: Workspace directory. If None, uses WORKING_DIR fallback.
    """
    with _stores_lock:
        if workspace_dir:
            key = str(Path(workspace_dir).resolve())
        else:
            key = str(Path(WORKING_DIR).resolve())
        if key not in _stores:
            path = Path(key) / MAIL_ACCESS_CONTROL_FILE
            _stores[key] = MailAccessControlStore(path)
        return _stores[key]
