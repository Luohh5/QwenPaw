# -*- coding: utf-8 -*-
"""Exact historical Voice method excerpts for AST-only compatibility oracles.

Provenance and original full-file digest: legacy_channel_baselines.json.
Never import this fixture as a runtime implementation.
"""

from typing import Any

from qwenpaw.schemas import ContentType, MessageType


class ConversationRelayBaseline:
    def _build_agent_request(self, text: str) -> Any:
        """Build an AgentRequest from user speech text."""
        from qwenpaw.schemas import (
            AgentRequest,
            Message,
            Role,
            TextContent,
        )

        msg = Message(
            type=MessageType.MESSAGE,
            role=Role.USER,
            content=[TextContent(type=ContentType.TEXT, text=text)],
        )
        return AgentRequest(
            session_id=f"voice:{self.call_sid}",
            user_id=self.caller_info.get("from", ""),
            input=[msg],
            channel=self._channel_type,
        )


    @staticmethod
    def _extract_text_from_event(event: Any) -> str:
        """Extract plain text from a completed message event."""
        content = getattr(event, "content", None)
        if not content:
            return ""

        parts: list[str] = []
        for c in content:
            ct = getattr(c, "type", None)
            if ct == ContentType.TEXT:
                text = getattr(c, "text", None)
                if text:
                    parts.append(text.strip())
            elif ct == ContentType.REFUSAL:
                refusal = getattr(c, "refusal", None)
                if refusal:
                    parts.append(refusal.strip())
        return " ".join(parts)
