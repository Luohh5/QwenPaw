# -*- coding: utf-8 -*-
# pylint: disable=too-many-statements,too-many-branches
# pylint: disable=too-many-return-statements,unused-argument
"""DingTalk Channel.

The handler ACKs the DingTalk Stream callback immediately. All actual
replies are delivered asynchronously via sessionWebhook, AI Card streaming
updates, or Open API. The base class event loop drives processing through
hooks (on_streaming_start/delta/end, on_event_message_completed, etc.).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    List,
    Optional,
)


import aiohttp
from qwenpaw.config.config import DingTalkConfig as DingTalkChannelConfig

from qwenpaw.app.channels.renderer import ChannelDisplayConfig
from qwenpaw.app.channels.base import (
    BaseChannel,
    ContentType,
    OnReplySent,
    ProcessHandler,
)

from .content_utils import (
    shared_group_session_id_from_conversation_id,
    short_session_id_from_conversation_id,
)

from .transport import DingTalkTransport

if TYPE_CHECKING:
    from qwenpaw.schemas import AgentRequest

logger = logging.getLogger(__name__)


class DingTalkChannel(DingTalkTransport, BaseChannel):
    """DingTalk Channel: DingTalk Stream -> Incoming -> to_agent_request ->
    process -> send_response -> DingTalk reply.

    Proactive send (stored sessionWebhook):
    - We store sessionWebhook from incoming messages in memory; send() uses it.
    - Key uses short suffix of conversation_id so request and cron stay short.
    - to_handle "dingtalk:sw:<session_id>" (session_id = last N of conv id).
    - Note: sessionWebhook has an expiry (sessionWebhookExpiredTime);
      push only works for users who have chatted recently. For cron to
      users who may not
      have spoken, consider Open API (corp_id + batchSend) instead.
    """

    channel = "dingtalk"
    _STREAM_DELTA_MIN_INTERVAL_S = 0.3

    _NON_SERIALIZABLE_META_KEYS = ()

    def __init__(
        self,
        process: ProcessHandler,
        enabled: bool,
        client_id: str,
        client_secret: str,
        bot_prefix: str,
        message_type: str = "markdown",
        cron_message_type: str = "markdown",
        card_template_id: str = "",
        card_template_key: str = "content",
        robot_code: str = "",
        media_dir: str = "",
        workspace_dir: Path | None = None,
        on_reply_sent: OnReplySent = None,
        display_config: ChannelDisplayConfig | None = None,
        no_text_debounce: bool = True,
        dm_policy: str = "open",
        group_policy: str = "open",
        allow_from: Optional[List[str]] = None,
        deny_message: str = "",
        require_mention: bool = False,
        card_auto_layout: bool = False,
        at_sender_on_reply: bool = False,
        streaming_enabled: bool = False,
        access_control_dm: bool = False,
        access_control_group: bool = False,
        endpoint: str = "",
        share_session_in_group: bool = False,
    ):
        # Streaming only makes sense for card mode (AI Card streaming updates).
        # For markdown mode, force streaming_enabled=False so base class
        # does not dispatch to streaming hooks (which would swallow events).
        effective_streaming = (
            streaming_enabled
            and (message_type or "markdown").strip().lower() == "card"
        )
        logger.info(
            "dingtalk init: streaming_enabled=%s message_type=%s "
            "effective_streaming=%s",
            streaming_enabled,
            message_type,
            effective_streaming,
        )
        super().__init__(
            process,
            on_reply_sent=on_reply_sent,
            display_config=display_config,
            no_text_debounce=no_text_debounce,
            dm_policy=dm_policy,
            group_policy=group_policy,
            allow_from=list(allow_from or []),
            deny_message=deny_message,
            require_mention=require_mention,
            streaming_enabled=effective_streaming,
            access_control_dm=access_control_dm,
            access_control_group=access_control_group,
        )
        self.initialize(locals())

    @classmethod
    def from_env(
        cls,
        process: ProcessHandler,
        on_reply_sent: OnReplySent = None,
    ) -> "DingTalkChannel":
        allow_from_env = os.getenv("DINGTALK_ALLOW_FROM", "")
        allow_from = (
            [s.strip() for s in allow_from_env.split(",") if s.strip()]
            if allow_from_env
            else []
        )
        return cls(
            process=process,
            enabled=os.getenv("DINGTALK_CHANNEL_ENABLED", "1") == "1",
            client_id=os.getenv("DINGTALK_CLIENT_ID", ""),
            client_secret=os.getenv("DINGTALK_CLIENT_SECRET", ""),
            bot_prefix=os.getenv("DINGTALK_BOT_PREFIX", ""),
            message_type=os.getenv("DINGTALK_MESSAGE_TYPE", "markdown"),
            cron_message_type=os.getenv(
                "DINGTALK_CRON_MESSAGE_TYPE",
                "markdown",
            ),
            card_template_id=os.getenv("DINGTALK_CARD_TEMPLATE_ID", ""),
            card_template_key=os.getenv(
                "DINGTALK_CARD_TEMPLATE_KEY",
                "content",
            ),
            robot_code=os.getenv("DINGTALK_ROBOT_CODE", "")
            or os.getenv("DINGTALK_CLIENT_ID", ""),
            media_dir=os.getenv("DINGTALK_MEDIA_DIR", ""),
            on_reply_sent=on_reply_sent,
            dm_policy=os.getenv("DINGTALK_DM_POLICY", "open"),
            group_policy=os.getenv("DINGTALK_GROUP_POLICY", "open"),
            allow_from=list(allow_from or []),
            deny_message=os.getenv("DINGTALK_DENY_MESSAGE", ""),
            require_mention=os.getenv("DINGTALK_REQUIRE_MENTION", "0") == "1",
            card_auto_layout=os.getenv("DINGTALK_CARD_AUTO_LAYOUT", "0")
            == "1",
            at_sender_on_reply=os.getenv(
                "DINGTALK_AT_SENDER_ON_REPLY",
                "0",
            )
            == "1",
            streaming_enabled=os.getenv(
                "DINGTALK_STREAMING_ENABLED",
                "0",
            )
            == "1",
            endpoint=os.getenv("DINGTALK_ENDPOINT", ""),
            share_session_in_group=(
                os.getenv("DINGTALK_SHARE_SESSION_IN_GROUP", "0") == "1"
            ),
        )

    @classmethod
    def from_config(
        cls,
        process: ProcessHandler,
        config: DingTalkChannelConfig,
        on_reply_sent: OnReplySent = None,
        display_config: ChannelDisplayConfig | None = None,
        no_text_debounce: bool = True,
        workspace_dir: Path | None = None,
    ) -> "DingTalkChannel":
        return cls(
            process=process,
            enabled=config.enabled,
            client_id=config.client_id or "",
            client_secret=config.client_secret or "",
            bot_prefix=config.bot_prefix or "",
            message_type=getattr(config, "message_type", "markdown"),
            cron_message_type=getattr(
                config,
                "cron_message_type",
                "markdown",
            ),
            card_template_id=getattr(config, "card_template_id", ""),
            card_template_key=getattr(config, "card_template_key", "content"),
            robot_code=(
                getattr(config, "robot_code", "") or config.client_id or ""
            ),
            media_dir=config.media_dir or "",
            workspace_dir=workspace_dir,
            on_reply_sent=on_reply_sent,
            display_config=display_config
            or ChannelDisplayConfig.from_config(config),
            no_text_debounce=no_text_debounce,
            dm_policy=config.dm_policy or "open",
            group_policy=config.group_policy or "open",
            allow_from=config.allow_from or [],
            deny_message=config.deny_message or "",
            require_mention=config.require_mention,
            card_auto_layout=getattr(config, "card_auto_layout", False),
            at_sender_on_reply=getattr(
                config,
                "at_sender_on_reply",
                False,
            ),
            streaming_enabled=bool(
                getattr(config, "streaming_enabled", False),
            ),
            access_control_dm=bool(
                getattr(config, "access_control_dm", False),
            ),
            access_control_group=bool(
                getattr(config, "access_control_group", False),
            ),
            endpoint=getattr(config, "endpoint", ""),
            share_session_in_group=bool(
                getattr(config, "share_session_in_group", False),
            ),
        )

    # ---------------------------
    # Proactive send: webhook store
    # ---------------------------

    def resolve_session_id(
        self,
        sender_id: str,
        channel_meta: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Resolve session_id from conversation metadata."""
        meta = channel_meta or {}
        cid = meta.get("conversation_id")
        if cid:
            # Shared groups need full-ID entropy because user_id is "group".
            if meta.get("is_group") and self.share_session_in_group:
                return shared_group_session_id_from_conversation_id(cid)
            return short_session_id_from_conversation_id(cid)
        return f"{self.channel}:{sender_id}"

    def get_debounce_key(self, payload: Any) -> str:
        """Queue routing key with sender isolation.

        Appends sender_id to the base session key so that messages
        from different users whose conversation_id share the same
        suffix are routed to separate queues and never merged.
        Skipped for shared group sessions so that one chat maps to
        one queue and turns stay serialized.
        """
        base_key = super().get_debounce_key(payload)
        if isinstance(payload, dict):
            meta = payload.get("meta") or {}
            if meta.get("is_group") and self.share_session_in_group:
                return base_key
            sender_id = payload.get("sender_id") or ""
            if sender_id:
                return f"{base_key}:{sender_id}"
        return base_key

    def build_agent_request_from_native(
        self,
        native_payload: Any,
    ) -> "AgentRequest":
        """Build AgentRequest from DingTalk native dict (runtime content)."""
        payload = native_payload if isinstance(native_payload, dict) else {}
        channel_id = payload.get("channel_id") or self.channel
        sender_id = payload.get("sender_id") or ""
        content_parts = payload.get("content_parts") or []
        meta = dict(payload.get("meta") or {})
        if payload.get("session_webhook"):
            meta["session_webhook"] = payload["session_webhook"]
        session_id = self.resolve_session_id(sender_id, meta)
        # Shared id must not contain "_": the webhook fallback key
        # splits "dingtalk:sw:<user_id>_<session_id>" on the last one.
        user_id = (
            "group"
            if (meta.get("is_group") and self.share_session_in_group)
            else sender_id
        )
        query = self._extract_query_from_payload(payload)
        if query.lstrip().startswith("/"):
            from qwenpaw.runtime.commands.control import (
                _COMMAND_REGISTRY,
                _extract_command_token,
                ApprovalCommandHandler,
                ApproveCommandHandler,
                DenyCommandHandler,
            )

            handler = _COMMAND_REGISTRY.get(_extract_command_token(query))
            if isinstance(
                handler,
                (
                    ApprovalCommandHandler,
                    ApproveCommandHandler,
                    DenyCommandHandler,
                ),
            ):
                user_id = str(payload.get("acl_sender_id") or "").strip()
                if not user_id:
                    raise ValueError(
                        "DingTalk approval operator is unavailable",
                    )
        request = self.build_agent_request_from_user_content(
            channel_id=channel_id,
            sender_id=user_id,
            session_id=session_id,
            content_parts=content_parts,
            channel_meta=meta,
        )
        # Set serializable channel_meta (exclude non-JSON-serializable objects)
        serializable_meta = {
            k: v
            for k, v in meta.items()
            if k not in self._NON_SERIALIZABLE_META_KEYS
        }
        setattr(request, "channel_meta", serializable_meta)
        return request

    def to_handle_from_target(self, *, user_id: str, session_id: str) -> str:
        # Key by session_id (short suffix of conversation_id) so cron can
        # use the same session_id to look up stored sessionWebhook.
        # For DM, prefix with user_id to avoid collision when different
        # conversation_ids share the same suffix.
        return (
            f"dingtalk:sw:{user_id}_{session_id}"
            if user_id
            else f"dingtalk:sw:{session_id}"
        )

    async def _before_consume_process(self, request: "AgentRequest") -> None:
        """Save session_webhook, send processing reaction, pre-create card."""
        meta = getattr(request, "channel_meta", None) or {}

        # Store session_webhook for cron/proactive send
        session_webhook = self._get_session_webhook(meta)
        if session_webhook:
            session_id = getattr(request, "session_id", None)
            if session_id:
                conversation_type = meta.get("conversation_type")
                # For DM, use user_id + suffix as key to avoid collision
                # when different conversation_ids share the same suffix.
                # For group, use suffix-only key (shared across users).
                if conversation_type == "dm":
                    webhook_key = self.to_handle_from_target(
                        user_id=getattr(request, "user_id", None) or "",
                        session_id=session_id,
                    )
                else:
                    webhook_key = f"dingtalk:sw:{session_id}"
                logger.info(
                    "dingtalk _before_consume_process: storing webhook "
                    "session_id=%s conversation_id=%s",
                    session_id,
                    meta.get("conversation_id"),
                )
                await self._save_session_webhook(
                    webhook_key,
                    session_webhook,
                    conversation_id=meta.get("conversation_id"),
                    conversation_type=conversation_type,
                    sender_staff_id=meta.get("sender_staff_id"),
                )

        # Add "processing" reaction to user's incoming message
        incoming_msg_id = str(meta.get("message_id") or "")
        conversation_id = str(meta.get("conversation_id") or "")
        if incoming_msg_id and conversation_id:
            await self._send_emotion(
                incoming_msg_id,
                conversation_id,
                "🤔Thinking",
            )

        # Pre-create AI Card before LLM call so user sees it immediately.
        # The card is stored on request._precreated_card for streaming hooks
        # and on_event_message_completed to reuse.
        if self._ai_card_enabled() and conversation_id:
            try:
                card = await self._create_ai_card(
                    conversation_id,
                    meta=meta,
                    inbound=True,
                )
                if card:
                    setattr(request, "_precreated_card", card)
                    logger.info(
                        "dingtalk _before_consume_process: "
                        "AI card pre-created for conversation=%s",
                        conversation_id,
                    )
            except (TimeoutError, aiohttp.ClientConnectionError) as exc:
                setattr(request, "_dingtalk_delivery_uncertain", exc)
                raise
            except Exception:
                logger.exception(
                    "dingtalk _before_consume_process: "
                    "card pre-creation failed, will retry in hooks",
                )

    async def _on_consume_error(
        self,
        request: "AgentRequest",
        to_handle: str,
        err_text: str,
    ) -> None:
        """Send error emoji and error message via webhook."""
        meta = getattr(request, "channel_meta", None) or {}
        incoming_msg_id = str(meta.get("message_id") or "")
        conversation_id = str(meta.get("conversation_id") or "")
        if incoming_msg_id and conversation_id:
            await self._send_emotion(
                incoming_msg_id,
                conversation_id,
                "🤔Thinking",
                recall=True,
            )
            await self._send_emotion(
                incoming_msg_id,
                conversation_id,
                "☹️Error",
            )
        # Send error text via webhook or fallback
        session_webhook = self._get_session_webhook(meta)
        bot_prefix = self.bot_prefix or ""
        full_err = bot_prefix + err_text if err_text else bot_prefix
        if session_webhook and full_err.strip():
            await self._send_via_session_webhook(
                session_webhook,
                full_err.strip(),
                bot_prefix="",
            )
        # Release dedup msg_id so future retries are accepted
        msg_ids = meta.get("_message_ids")
        if msg_ids is None:
            msg_ids = [incoming_msg_id] if incoming_msg_id else []
        self._release_message_ids(msg_ids)

    # ---------------------------
    # Reply via stream thread
    # ---------------------------

    def _try_accept_message(self, msg_id: str) -> bool:
        """Return True if accepted; False if duplicate (msg_id already in
        progress). Thread-safe; handler in stream thread.
        """
        with self._processing_message_ids_lock:
            if msg_id and msg_id in self._processing_message_ids:
                logger.info(
                    "dingtalk dedup reject: msg_id already in progress "
                    "msg_id=%r",
                    msg_id,
                )
                return False
            if msg_id:
                self._processing_message_ids.add(msg_id)
            logger.debug(
                "dingtalk dedup accept: msg_id=%r in_flight_count=%s",
                msg_id or "(empty)",
                len(self._processing_message_ids),
            )
            return True

    def _release_message_ids(self, msg_ids: List[str]) -> None:
        """Release msg ids after reply."""
        if not msg_ids:
            return
        with self._processing_message_ids_lock:
            for mid in msg_ids:
                if mid:
                    self._processing_message_ids.discard(mid)
            logger.debug(
                "dingtalk dedup release: msg_ids=%s in_flight_count=%s",
                msg_ids,
                len(self._processing_message_ids),
            )

    def merge_native_items(self, items: List[Any]) -> Any:
        """Merge payloads (content_parts + meta) for DingTalk."""
        return self._merge_native(items)

    def _on_debounce_buffer_append(
        self,
        key: str,
        payload: Any,
        existing_items: List[Any],
    ) -> None:
        """No-op: handler ACKs immediately, no futures to unblock."""
        del key, payload, existing_items

    def _resolve_to_handle(self, request: Any) -> str:
        """Resolve target handle from request using session-aware logic."""
        user_id = getattr(request, "user_id", "") or ""
        sid = getattr(request, "session_id", "") or ""
        if sid:
            return self.to_handle_from_target(
                user_id=user_id,
                session_id=sid,
            )
        return user_id

    async def _deliver_media_parts(
        self,
        parts: list,
        webhook: Optional[str],
        to_handle: str,
        meta: Dict[str, Any],
    ) -> None:
        """Send media parts separately.

        AI Card only carries text; images, files,
        videos and audio must be delivered via
        webhook upload or Open API.
        """
        _types = (
            ContentType.IMAGE,
            ContentType.FILE,
            ContentType.VIDEO,
            ContentType.AUDIO,
        )
        for part in parts:
            pt = getattr(part, "type", None)
            if pt not in _types:
                continue
            sent = False
            if webhook:
                sent = await self._send_media_part_via_webhook(
                    webhook,
                    part,
                )
            if not sent:
                resolver = getattr(
                    self,
                    "_resolve_open_api_params_from_handle",
                )
                params = await resolver(
                    to_handle,
                    meta,
                )
                cid = params["conversation_id"]
                if cid:
                    await self._send_media_part_via_open_api(
                        part,
                        conversation_id=cid,
                        conversation_type=params["conversation_type"],
                        sender_staff_id=params["sender_staff_id"],
                    )

    # -- Base class hook overrides -----------------------------

    def get_to_handle_from_request(self, request: "AgentRequest") -> str:
        """Use session-aware handle resolution for DingTalk."""
        return self._resolve_to_handle(request)

    # -- Streaming hooks (card mode) ---------------------------

    def _get_dingtalk_stream_state(
        self,
        send_meta: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Get or create per-request streaming state in send_meta."""
        state = send_meta.get("_dt_stream")
        if state is None:
            state = {
                "cards": {},
                "at_sent": False,
            }
            send_meta["_dt_stream"] = state
        return state

    def _build_card_at_prefix(
        self,
        send_meta: Dict[str, Any],
    ) -> str:
        """Build @mention prefix for AI card content in group chats."""
        if (
            self.at_sender_on_reply
            and send_meta.get("is_group", False)
            and send_meta.get("sender_staff_id", "")
        ):
            at_id = send_meta["sender_staff_id"]
            at_nick = send_meta.get("user_name", "") or at_id
            return f"<a atId={at_id}>{at_nick}</a>\n"
        return ""

    async def on_streaming_start(
        self,
        request: "AgentRequest",
        to_handle: str,
        event: Any,
        send_meta: Dict[str, Any],
        stream_type: str,
        accumulated_text: str = "",
    ) -> None:
        """Card mode: reuse pre-created card or create a new AI Card."""
        if getattr(request, "_dingtalk_delivery_uncertain", None):
            raise getattr(request, "_dingtalk_delivery_uncertain")
        conversation_id = str(send_meta.get("conversation_id") or "")
        if not conversation_id or not self._ai_card_enabled():
            return

        state = self._get_dingtalk_stream_state(send_meta)

        # Build @mention prefix for the first card only
        at_prefix = ""
        if not state["at_sent"]:
            at_prefix = self._build_card_at_prefix(send_meta)
            state["at_sent"] = True

        try:
            # Reuse pre-created card from _before_consume_process if available
            card = getattr(request, "_precreated_card", None)
            if card:
                # Consume: first streaming segment uses pre-created card
                setattr(request, "_precreated_card", None)
            else:
                # Later segments (e.g. reasoning→message) need new card
                card = await self._create_ai_card(
                    conversation_id,
                    meta=send_meta,
                    inbound=True,
                    force=True,
                )
            if card:
                prefix = "💭 " if stream_type == "reasoning" else ""
                initial_text = f"{at_prefix}{prefix}..."
                await self._stream_ai_card(card, initial_text, finalize=False)
                state["cards"][stream_type] = {
                    "card": card,
                    "at_prefix": at_prefix,
                    "display_prefix": prefix,
                }
        except (TimeoutError, aiohttp.ClientConnectionError):
            raise
        except Exception:
            logger.exception(
                "dingtalk on_streaming_start: card creation failed "
                "stream_type=%s",
                stream_type,
            )
            state["cards"].pop(stream_type, None)

    async def on_streaming_delta(
        self,
        request: "AgentRequest",
        to_handle: str,
        event: Any,
        send_meta: Dict[str, Any],
        stream_type: str,
        accumulated_text: str = "",
    ) -> None:
        """Card mode: stream incremental text to the AI Card."""
        state = send_meta.get("_dt_stream")
        if not state:
            return
        card_info = state["cards"].get(stream_type)
        if not card_info:
            return

        card = card_info["card"]
        at_prefix = card_info["at_prefix"]
        display_prefix = card_info["display_prefix"]
        display_text = f"{at_prefix}{display_prefix}{accumulated_text}"

        try:
            await self._stream_ai_card(card, display_text, finalize=False)
        except (TimeoutError, aiohttp.ClientConnectionError):
            raise
        except Exception:
            logger.debug(
                "dingtalk on_streaming_delta: card update failed "
                "stream_type=%s",
                stream_type,
            )

    async def on_streaming_end(
        self,
        request: "AgentRequest",
        to_handle: str,
        event: Any,
        send_meta: Dict[str, Any],
        stream_type: str,
        accumulated_text: str = "",
    ) -> None:
        """Card mode: finalize the AI Card for this segment."""
        state = send_meta.get("_dt_stream")
        if not state:
            return
        card_info = state["cards"].pop(stream_type, None)
        if not card_info:
            return

        card = card_info["card"]
        at_prefix = card_info["at_prefix"]
        display_prefix = card_info["display_prefix"]
        final_text = f"{at_prefix}{display_prefix}{accumulated_text}"

        try:
            await self._stream_ai_card(card, final_text, finalize=True)
        except (TimeoutError, aiohttp.ClientConnectionError):
            raise
        except Exception:
            logger.exception(
                "dingtalk on_streaming_end: card finalize failed "
                "stream_type=%s",
                stream_type,
            )
            await self._mark_card_failed(
                str(send_meta.get("conversation_id") or ""),
            )

        # Deliver media parts separately (card only carries text)
        if stream_type == "message":
            parts = self._message_to_content_parts(event)
            session_webhook = self._get_session_webhook(send_meta)
            await self._deliver_media_parts(
                parts,
                session_webhook,
                to_handle,
                send_meta,
            )

    # -- Non-streaming message handler (markdown mode) ---------

    async def on_event_message_completed(
        self,
        request: "AgentRequest",
        to_handle: str,
        event: Any,
        send_meta: Dict[str, Any],
    ) -> None:
        """Send completed message via AI Card or sessionWebhook.

        Card mode (non-streaming): accumulate text into a single pre-created
        card. The card is finalized in _on_process_completed after all messages
        are processed.
        Markdown mode: send via sessionWebhook or fallback to send().
        """
        if getattr(request, "_dingtalk_delivery_uncertain", None):
            raise getattr(request, "_dingtalk_delivery_uncertain")
        parts = self._message_to_content_parts(event)
        if not parts:
            return

        bot_prefix = self.bot_prefix or ""
        body = self._parts_to_single_text(parts, bot_prefix=bot_prefix)
        conversation_id = str(send_meta.get("conversation_id") or "")
        session_webhook = self._get_session_webhook(send_meta)

        # -- Card mode: accumulate text into pre-created card (no finalize) --
        if self._ai_card_enabled() and conversation_id and body.strip():
            state = self._get_dingtalk_stream_state(send_meta)
            at_prefix = ""
            if not state["at_sent"]:
                at_prefix = self._build_card_at_prefix(send_meta)
                state["at_sent"] = True

            # Get or initialize the active card for this request
            card = state.get("nonstream_card")
            if not card:
                card = getattr(request, "_precreated_card", None)
                if card:
                    setattr(request, "_precreated_card", None)
                else:
                    try:
                        card = await self._create_ai_card(
                            conversation_id,
                            meta=send_meta,
                            inbound=True,
                        )
                    except (TimeoutError, aiohttp.ClientConnectionError):
                        raise
                    except Exception:
                        logger.exception(
                            "dingtalk on_event_message_completed: "
                            "card creation failed, fallback to markdown",
                        )
                        await self._mark_card_failed(conversation_id)
                        card = None
                if card:
                    state["nonstream_card"] = card
                    state["card_full_text"] = ""
                    state["card_at_prefix"] = at_prefix

            if card:
                # Accumulate text
                prev_text = state.get("card_full_text") or ""
                new_body = body.strip()
                if prev_text and new_body:
                    merged = f"{prev_text}\n{new_body}"
                else:
                    merged = new_body or prev_text
                state["card_full_text"] = merged

                # Stream update (not finalize) so user sees progress
                card_at = state.get("card_at_prefix") or ""
                try:
                    await self._stream_ai_card(
                        card,
                        card_at + merged,
                        finalize=False,
                    )
                except (TimeoutError, aiohttp.ClientConnectionError):
                    raise
                except Exception:
                    logger.exception(
                        "dingtalk on_event_message_completed: "
                        "card stream failed, fallback to markdown",
                    )
                    await self._mark_card_failed(conversation_id)
                    state.pop("nonstream_card", None)
                    # Fall through to markdown mode below
                else:
                    # Deliver media parts separately (card only carries text)
                    await self._deliver_media_parts(
                        parts,
                        session_webhook,
                        to_handle,
                        send_meta,
                    )
                    return

        # -- Markdown mode: send via webhook --
        if session_webhook and body.strip():
            at_uids = None
            at_dids = None
            is_group = send_meta.get("is_group", False)
            state = self._get_dingtalk_stream_state(send_meta)
            if self.at_sender_on_reply and not state["at_sent"] and is_group:
                staff_id = send_meta.get("sender_staff_id", "")
                dingtalk_id = send_meta.get("sender_dingtalk_id", "")
                if staff_id:
                    at_uids = [staff_id]
                elif dingtalk_id:
                    at_dids = [dingtalk_id]
                state["at_sent"] = True
            await self._send_via_session_webhook(
                session_webhook,
                body.strip(),
                bot_prefix="",
                at_user_ids=at_uids,
                at_dingtalk_ids=at_dids,
            )
            # Deliver media parts via webhook
            media_types = (
                ContentType.IMAGE,
                ContentType.FILE,
                ContentType.VIDEO,
                ContentType.AUDIO,
            )
            for part in parts:
                if getattr(part, "type", None) in media_types:
                    await self._send_media_part_via_webhook(
                        session_webhook,
                        part,
                    )
        elif body.strip() or parts:
            await self.send_content_parts(to_handle, parts, send_meta)

    # -- Native payload merging --------------------------------

    def _merge_native(self, items: list) -> dict:
        """Merge multiple native payloads into one (content_parts + meta)."""
        if not items:
            return {}
        first = items[0] if isinstance(items[0], dict) else {}
        merged_parts: List[Any] = []
        merged_meta: Dict[str, Any] = dict(first.get("meta") or {})

        message_ids_list: List[str] = []
        for it in items:
            payload = it if isinstance(it, dict) else {}
            merged_parts.extend(payload.get("content_parts") or [])
            m = payload.get("meta") or {}
            # Keep first sender identity; refresh only delivery metadata.
            for k in (
                "conversation_id",
                "session_webhook",
                "session_webhook_expired_time",
                "conversation_type",
            ):
                if k in m:
                    merged_meta[k] = m[k]
            mid = m.get("message_id") or payload.get("message_id")
            if mid:
                message_ids_list.append(str(mid))

        merged_meta["batched_count"] = len(items)
        merged_meta["_message_ids"] = message_ids_list
        # Queue is FIFO: batch = [oldest, ..., newest]. Prefer
        # session_webhook (and related metadata) from newest item so send
        # uses current session.
        out_sw: Optional[str] = None
        for it in reversed(items):
            pl = it if isinstance(it, dict) else {}
            sw = pl.get("session_webhook") or (pl.get("meta") or {}).get(
                "session_webhook",
            )
            if sw:
                out_sw = sw
                break
        out = {
            "channel_id": first.get("channel_id") or self.channel,
            "sender_id": first.get("sender_id") or "",
            "acl_sender_id": first.get("acl_sender_id") or "",
            "content_parts": merged_parts,
            "meta": merged_meta,
        }
        if out_sw:
            out["session_webhook"] = out_sw
            merged_meta["session_webhook"] = out_sw
        return out

    async def _on_process_completed(
        self,
        request: Any,
        to_handle: str,
        send_meta: Dict[str, Any],
    ) -> None:
        """Finalize card, recall 'processing' reaction, release dedup."""
        incoming_msg_id = str((send_meta or {}).get("message_id") or "")
        conversation_id = str((send_meta or {}).get("conversation_id") or "")

        # Finalize non-streaming card if accumulated text exists
        state = (send_meta or {}).get("_dt_stream")
        if state:
            card = state.get("nonstream_card")
            if card:
                card_text = (
                    state.get("card_full_text")
                    or self._build_ai_card_initial_text()
                )
                card_at = state.get("card_at_prefix") or ""
                try:
                    await self._stream_ai_card(
                        card,
                        card_at + card_text,
                        finalize=True,
                    )
                except (TimeoutError, aiohttp.ClientConnectionError):
                    raise
                except Exception:
                    logger.exception(
                        "dingtalk _on_process_completed: "
                        "card finalize failed",
                    )
                    await self._mark_card_failed(conversation_id)
                state.pop("nonstream_card", None)

        # Also finalize any unused pre-created card (e.g. no messages produced)
        unused_card = getattr(request, "_precreated_card", None)
        if unused_card:
            setattr(request, "_precreated_card", None)
            try:
                await self._stream_ai_card(
                    unused_card,
                    self._build_ai_card_initial_text(),
                    finalize=True,
                )
            except (TimeoutError, aiohttp.ClientConnectionError):
                raise
            except Exception:
                logger.debug(
                    "dingtalk _on_process_completed: "
                    "unused card finalize failed",
                    exc_info=True,
                )

        if incoming_msg_id and conversation_id:
            await self._send_emotion(
                incoming_msg_id,
                conversation_id,
                "🤔Thinking",
                recall=True,
            )
            await self._send_emotion(
                incoming_msg_id,
                conversation_id,
                "🥳Done",
            )
        # Release dedup msg_id so future messages with same id are accepted
        msg_ids = (send_meta or {}).get("_message_ids")
        if msg_ids is None:
            msg_ids = [incoming_msg_id] if incoming_msg_id else []
        self._release_message_ids(msg_ids)

    # ---- Emotion reaction helpers ----
