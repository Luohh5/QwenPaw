# -*- coding: utf-8 -*-
"""Normalize Driver identity without interpreting platform metadata."""

from .errors import ProtocolValidationError
from .models import InboundEvent


def normalize_inbound_event(event: InboundEvent) -> dict:
    if not isinstance(event, InboundEvent):
        raise ProtocolValidationError("InboundEvent required")
    meta = dict(event.metadata)
    for key in ("session_id", "user_id", "debounce_key"):
        if not isinstance(meta.get(key), str) or not meta[key].strip():
            raise ProtocolValidationError(
                "Standard inbound identity missing: " + key,
            )
    meta.update(
        is_group=event.conversation["type"] == "group",
        response_handle=event.response_handle,
    )
    return {
        "channel_id": event.channel_key,
        "sender_id": event.sender_id,
        "acl_sender_id": event.acl_sender_id,
        "user_id": meta["user_id"],
        "session_id": meta["session_id"],
        "content_parts": list(event.content_parts),
        "meta": meta,
    }
