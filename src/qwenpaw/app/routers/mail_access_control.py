# -*- coding: utf-8 -*-
"""API router for mail access control (whitelist / blacklist / pending).

Read endpoints aggregate ACL data across all agents that have mail access
control enabled; write endpoints route each entry to the owning agent's
workspace store.  An empty ``agent_id`` on whitelist/blacklist "add" entries
means "broadcast to all mail-enabled agents".
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/mail-access-control", tags=["mail-access-control"])


# ── Store helpers ───────────────────────────────────────────────────────────


def _agent_mail_acl_enabled(agent_id: str) -> bool:
    """Return True if the agent has mailbox management with access control enabled."""
    from ...config.config import load_agent_config

    try:
        agent_config = load_agent_config(agent_id)
    except Exception:
        return False
    mail = getattr(agent_config, "mail", None)
    if mail is None or mail.push is None:
        return False
    return mail.push.mode != "off" and bool(mail.push.access_control_enabled)


def _iter_mail_agent_stores() -> Iterator[Tuple[str, Any]]:
    """Yield (agent_id, store) for all enabled agents with mail ACL enabled."""
    from ...config.utils import load_config
    from ..mail.mail_access_control import get_mail_access_control_store

    config = load_config()
    for agent_id, agent_ref in config.agents.profiles.items():
        if not getattr(agent_ref, "enabled", True):
            continue
        if not _agent_mail_acl_enabled(agent_id):
            continue
        yield agent_id, get_mail_access_control_store(Path(agent_ref.workspace_dir))


def _get_store_for_agent(agent_id: str):
    """Get the MailAccessControlStore for a specific agent, or None if unknown."""
    from ...config.utils import load_config
    from ..mail.mail_access_control import get_mail_access_control_store

    config = load_config()
    agent_ref = config.agents.profiles.get(agent_id)
    if agent_ref is None:
        return None
    return get_mail_access_control_store(Path(agent_ref.workspace_dir))


# ── Request / Response schemas ──────────────────────────────────────────────


class MailACLEntry(BaseModel):
    agent_id: str
    address: str
    remark: Optional[str] = None
    display_name: Optional[str] = None


class MailACLActionBody(BaseModel):
    entries: List[MailACLEntry]


class MailACLRemarkBody(BaseModel):
    agent_id: str
    address: str
    remark: str


# ── Endpoints ───────────────────────────────────────────────────────────────


@router.get(
    "/agents",
    summary="List all agents with mail access control enabled",
)
async def list_mail_agents():
    """Return agent ids that have mailbox access control enabled."""
    return {"agents": [agent_id for agent_id, _ in _iter_mail_agent_stores()]}


@router.get(
    "",
    summary="Get all mail access control lists",
)
async def get_all_acls():
    """Return mail ACLs aggregated across all mail-enabled agents."""
    result: Dict[str, Dict[str, Any]] = {}
    for agent_id, store in _iter_mail_agent_stores():
        result[agent_id] = store.get_acl(agent_id)
    return result


@router.get(
    "/pending/all",
    summary="Get all pending approval entries",
)
async def get_all_pending():
    """Return pending entries aggregated across all mail-enabled agents."""
    result: List[Dict[str, Any]] = []
    for agent_id, store in _iter_mail_agent_stores():
        acl = store.get_acl(agent_id)
        result.extend(acl.get("pending", []))
    result.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
    return result


@router.get(
    "/pending/count",
    summary="Get pending approval count",
)
async def get_pending_count():
    """Return the total pending count across all mail-enabled agents."""
    count = 0
    for agent_id, store in _iter_mail_agent_stores():
        count += len(store.get_acl(agent_id).get("pending", []))
    return {"count": count}


@router.post(
    "/pending/approve",
    summary="Approve one or more pending senders (add to whitelist)",
)
async def approve_pending(body: MailACLActionBody, request: Request):
    count = 0
    for entry in body.entries:
        store = _get_store_for_agent(entry.agent_id)
        if store is None:
            continue
        # Snapshot the pending entry (uid/date/subject) before it is
        # removed by approve_pending, so the blocked email can be
        # auto-handled after approval.
        pending_info = store.get_pending_entry(entry.agent_id, entry.address)
        store.approve_pending(
            entry.agent_id,
            entry.address,
            entry.remark or "",
        )
        count += 1
        try:
            await _trigger_wake_after_approve(request, entry, pending_info)
        except Exception:  # pylint: disable=broad-except
            logger.warning(
                "failed to trigger mail auto-handling after approving "
                "sender %s for agent %s",
                entry.address,
                entry.agent_id,
                exc_info=True,
            )
    return {"status": "ok", "count": count}


async def _trigger_wake_after_approve(
    request: Request,
    entry: MailACLEntry,
    pending_info: Optional[Dict[str, Any]],
) -> None:
    """Wake the agent to handle the blocked email after sender approval.

    Skips silently when the pending entry has no uid (legacy data) or
    the agent workspace is unavailable.
    """
    from ..mail.monitor import wake_agent_for_mail

    if not pending_info or not pending_info.get("uid", 0):
        return
    manager = getattr(request.app.state, "multi_agent_manager", None)
    if manager is None:
        return
    workspace = await manager.get_agent(entry.agent_id)
    if workspace is None:
        return
    asyncio.create_task(
        wake_agent_for_mail(
            workspace,
            entry.agent_id,
            uid=pending_info["uid"],
            sender=pending_info.get("display_name") or entry.address,
            subject=pending_info.get("subject", ""),
            date=pending_info.get("date", ""),
        )
    )


@router.post(
    "/pending/deny",
    summary="Deny one or more pending senders (add to blacklist)",
)
async def deny_pending(body: MailACLActionBody):
    count = 0
    for entry in body.entries:
        store = _get_store_for_agent(entry.agent_id)
        if store is None:
            continue
        store.deny_pending(
            entry.agent_id,
            entry.address,
            entry.remark or "",
        )
        count += 1
    return {"status": "ok", "count": count}


@router.post(
    "/pending/dismiss",
    summary="Dismiss one or more pending senders (remove w/o action)",
)
async def dismiss_pending(body: MailACLActionBody):
    count = 0
    for entry in body.entries:
        store = _get_store_for_agent(entry.agent_id)
        if store is None:
            continue
        store.dismiss_pending(entry.agent_id, entry.address)
        count += 1
    return {"status": "ok", "count": count}


@router.post(
    "/pending/remark",
    summary="Update remark on a pending entry",
)
async def update_pending_remark(body: MailACLRemarkBody):
    store = _get_store_for_agent(body.agent_id)
    found = store is not None and store.update_pending_remark(
        body.agent_id,
        body.address,
        body.remark,
    )
    if not found:
        raise HTTPException(
            status_code=404,
            detail="Pending entry not found",
        )
    return {"status": "ok"}


# ── Whitelist / Blacklist endpoints ─────────────────────────────────────────


@router.post(
    "/whitelist/add",
    summary="Add one or more addresses to whitelist",
)
async def add_to_whitelist(body: MailACLActionBody):
    count = 0
    for entry in body.entries:
        if entry.agent_id == "":
            # Broadcast: apply to all mail-enabled agents.
            for agent_id, store in _iter_mail_agent_stores():
                store.add_to_whitelist(
                    agent_id,
                    entry.address,
                    remark=entry.remark or "",
                    display_name=entry.display_name or "",
                )
                count += 1
            continue
        store = _get_store_for_agent(entry.agent_id)
        if store is None:
            continue
        store.add_to_whitelist(
            entry.agent_id,
            entry.address,
            remark=entry.remark or "",
            display_name=entry.display_name or "",
        )
        count += 1
    return {"status": "ok", "count": count}


@router.post(
    "/whitelist/remove",
    summary="Remove one or more addresses from whitelist",
)
async def remove_from_whitelist(body: MailACLActionBody):
    count = 0
    for entry in body.entries:
        store = _get_store_for_agent(entry.agent_id)
        if store is None:
            continue
        store.remove_from_whitelist(entry.agent_id, entry.address)
        count += 1
    return {"status": "ok", "count": count}


@router.post(
    "/blacklist/add",
    summary="Add one or more addresses to blacklist",
)
async def add_to_blacklist(body: MailACLActionBody):
    count = 0
    for entry in body.entries:
        if entry.agent_id == "":
            # Broadcast: apply to all mail-enabled agents.
            for agent_id, store in _iter_mail_agent_stores():
                store.add_to_blacklist(
                    agent_id,
                    entry.address,
                    remark=entry.remark or "",
                    display_name=entry.display_name or "",
                )
                count += 1
            continue
        store = _get_store_for_agent(entry.agent_id)
        if store is None:
            continue
        store.add_to_blacklist(
            entry.agent_id,
            entry.address,
            remark=entry.remark or "",
            display_name=entry.display_name or "",
        )
        count += 1
    return {"status": "ok", "count": count}


@router.post(
    "/blacklist/remove",
    summary="Remove one or more addresses from blacklist",
)
async def remove_from_blacklist(body: MailACLActionBody):
    count = 0
    for entry in body.entries:
        store = _get_store_for_agent(entry.agent_id)
        if store is None:
            continue
        store.remove_from_blacklist(entry.agent_id, entry.address)
        count += 1
    return {"status": "ok", "count": count}


@router.post(
    "/remark",
    summary="Update remark for an address in whitelist or blacklist",
)
async def update_remark(body: MailACLRemarkBody):
    store = _get_store_for_agent(body.agent_id)
    found = store is not None and store.update_remark(
        body.agent_id,
        body.address,
        body.remark,
    )
    if not found:
        raise HTTPException(
            status_code=404,
            detail="Address not found in any list",
        )
    return {"status": "ok"}
