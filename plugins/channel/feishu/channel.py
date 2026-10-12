# -*- coding: utf-8 -*-
# pylint: disable=unused-argument,too-many-return-statements
"""Feishu transport adapter for the host queue and Agent execution."""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from qwenpaw.app.channels.base import BaseChannel, OnReplySent, ProcessHandler
from qwenpaw.app.channels.renderer import ChannelDisplayConfig
from qwenpaw.config.config import FeishuConfig
from qwenpaw.config.utils import get_config_path
from qwenpaw.constant import DEFAULT_MEDIA_DIR
from qwenpaw.schemas import TextContent

from .constants import FEISHU_STREAM_MIN_INTERVAL_S
from .utils import sender_display_string, short_session_id_from_full_id

logger = logging.getLogger(__name__)

_RECONNECT_INITIAL_DELAY = 1.0
_RECONNECT_MAX_DELAY = 30.0


class FeishuChannel(BaseChannel):
    channel = "feishu"
    _STREAM_DELTA_MIN_INTERVAL_S = FEISHU_STREAM_MIN_INTERVAL_S

    @classmethod
    def from_config(
        cls,
        process: ProcessHandler,
        config: FeishuConfig,
        on_reply_sent: OnReplySent = None,
        display_config: ChannelDisplayConfig | None = None,
        no_text_debounce: bool = True,
        workspace_dir: Path | None = None,
    ) -> FeishuChannel:
        instance = cls(
            process=process,
            on_reply_sent=on_reply_sent,
            display_config=display_config
            or ChannelDisplayConfig.from_config(config),
            no_text_debounce=no_text_debounce,
            dm_policy=config.dm_policy,
            group_policy=config.group_policy,
            allow_from=config.allow_from,
            deny_message=config.deny_message,
            require_mention=config.require_mention,
            streaming_enabled=config.streaming_enabled,
            access_control_dm=config.access_control_dm,
            access_control_group=config.access_control_group,
        )
        instance.config = config.model_copy(deep=True)
        for key in (
            "enabled",
            "app_id",
            "bot_prefix",
            "share_session_in_group",
            "auto_collapse_thinking",
        ):
            setattr(instance, key, getattr(instance.config, key))
        instance._workspace_dir = (
            Path(workspace_dir).expanduser() if workspace_dir else None
        )
        instance._media_dir = (
            Path(config.media_dir).expanduser()
            if config.media_dir
            else instance._workspace_dir / "media"
            if instance._workspace_dir
            else DEFAULT_MEDIA_DIR
        )
        instance._platform = None
        instance._active = False
        instance._lifecycle_lock = asyncio.Lock()
        instance._cleanup_task = None
        instance._supervisor = None
        instance._generation = 0
        instance._state = "stopped"
        instance._last_error = None
        instance._receive_id_store = {}
        instance._receive_id_lock = asyncio.Lock()
        return instance

    @classmethod
    def from_env(
        cls,
        process: ProcessHandler,
        on_reply_sent: OnReplySent = None,
    ):
        import os

        fields = {}
        for name in FeishuConfig.model_fields:
            value = os.getenv(
                "FEISHU_CHANNEL_ENABLED"
                if name == "enabled"
                else f"FEISHU_{name.upper()}",
            )
            if value is not None:
                fields[name] = (
                    [s.strip() for s in value.split(",") if s.strip()]
                    if name == "allow_from"
                    else value
                )
        return cls.from_config(process, FeishuConfig(**fields), on_reply_sent)

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if not self.enabled or self._active:
                return
            if self._platform is not None:
                raise RuntimeError("Feishu cleanup is incomplete; retry stop")
            self._state = "starting"
            self._last_error = None
            try:
                from .platform import LarkOapiPlatform
            except ModuleNotFoundError as exc:
                package = (
                    "lark-oapi"
                    if (exc.name or "").startswith("lark_oapi")
                    else exc.name
                )
                self._state = "start_failed"
                self._last_error = f"Missing package: {package}"
                raise RuntimeError(
                    f"Feishu requires missing package: {package}",
                ) from exc

            platform = LarkOapiPlatform()
            self._platform = platform
            self._cleanup_task = None
            config = self.config.model_dump()
            secret = {
                key: config.pop(key)
                for key in ("app_secret", "encrypt_key", "verification_token")
            }
            config["bot_prefix"] = ""
            self._load_receive_id_store_from_disk()
            try:
                await platform.prepare(config, secret, self._media_dir)
                self._active = True
                await self._connect(platform)
                self._state = "running"
                self._supervisor = asyncio.create_task(
                    self._supervise(platform),
                )
            except BaseException as error:
                self._active = False
                self._generation += 1
                self._begin_cleanup()
                try:
                    await asyncio.shield(self._cleanup_task)
                except Exception:
                    if not isinstance(error, asyncio.CancelledError):
                        raise
                else:
                    self._state = "start_failed"
                    self._last_error = type(error).__name__
                raise

    async def _connect(self, platform) -> None:
        self._generation += 1
        generation = self._generation
        ready = asyncio.Event()

        async def on_message(value):
            await ready.wait()
            if self._generation == generation and self._platform is platform:
                await self._handle_platform_message(value)

        async def on_card(value):
            await ready.wait()
            if self._generation == generation and self._platform is platform:
                await self._handle_card_action(value)

        try:
            await platform.connect(on_message, on_card)
        finally:
            # The caller activates or rejects this generation before yielding.
            ready.set()

    async def _supervise(self, platform) -> None:
        delay = _RECONNECT_INITIAL_DELAY
        try:
            while True:
                await platform.wait_disconnected()
                self._active = False
                self._state = "reconnecting"
                self._generation += 1
                # Failed disconnect retains handles and prevents replacement.
                await platform.disconnect()
                while True:
                    await asyncio.sleep(delay)
                    try:
                        await self._connect(platform)
                    except Exception:
                        logger.warning(
                            "Feishu reconnect failed",
                            exc_info=True,
                        )
                        await platform.disconnect()
                        delay = min(delay * 2, _RECONNECT_MAX_DELAY)
                    else:
                        self._active = True
                        self._state = "running"
                        delay = _RECONNECT_INITIAL_DELAY
                        break
        except Exception as exc:
            self._state = "stop_failed"
            self._last_error = str(exc)
            logger.exception("Feishu connection cleanup failed; stop required")

    def _begin_cleanup(self) -> None:
        self._cleanup_task = asyncio.create_task(self._cleanup())
        self._cleanup_task.add_done_callback(self._cleanup_done)

    def _cleanup_done(self, task) -> None:
        if not task.cancelled() and task.exception() is not None:
            logger.error("Feishu cleanup failed", exc_info=task.exception())

    async def _cleanup(self) -> None:
        self._state = "stopping"
        try:
            if self._supervisor is not None:
                self._supervisor.cancel()
                await asyncio.gather(self._supervisor, return_exceptions=True)
                self._supervisor = None
            if self._platform is not None:
                await self._platform.close()
                self._platform = None
        except BaseException as exc:
            self._state = "stop_failed"
            self._last_error = str(exc)
            raise
        self._state = "stopped"
        self._last_error = None

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            self._active = False
            self._generation += 1
            if self._cleanup_task is None or self._cleanup_task.done():
                self._begin_cleanup()
            # Caller cancellation preserves cleanup and live handles.
            await asyncio.shield(self._cleanup_task)

    async def health_check(self) -> dict:
        connected = (
            self._active
            and self._platform is not None
            and bool(
                (await self._platform.health_snapshot()).get("connected"),
            )
        )
        return {
            "channel": self.channel,
            "status": "healthy" if connected else "unhealthy",
            "state": self._state,
            "error": self._last_error,
        }

    def _require_platform(self):
        if not self.enabled or not self._active or self._platform is None:
            raise RuntimeError("Feishu plugin transport is not available")
        return self._platform

    async def _handle_platform_message(self, value: object) -> None:
        if not self._active or not isinstance(value, dict):
            return
        sender = str(value.get("sender_open_id") or "")
        chat_id = str(value.get("chat_id") or "")
        chat_type = str(value.get("chat_type") or "p2p")
        parts = value.get("content_parts") or []
        if not sender or not parts:
            return
        is_group = chat_type == "group"
        meta = {
            "feishu_message_id": str(value.get("message_id") or ""),
            "feishu_chat_id": chat_id,
            "feishu_chat_type": chat_type,
            "feishu_sender_id": sender,
            "feishu_receive_id": chat_id if is_group else sender,
            "feishu_receive_id_type": "chat_id" if is_group else "open_id",
            "is_group": is_group,
            "user_name": str(value.get("sender_name") or ""),
            "bot_mentioned": bool(value.get("bot_mentioned")),
        }
        if not self._check_group_mention(is_group, meta):
            return
        session_id = self.resolve_session_id(sender, meta)
        thread_id = value.get("thread_id")
        if thread_id:
            meta["feishu_thread_id"] = thread_id
            meta[
                "feishu_sender_id"
            ] = f"thread:{short_session_id_from_full_id(thread_id)}"
        elif is_group and self.share_session_in_group:
            meta["feishu_sender_id"] = "group"
        # Typed native parts preserve the host no-text debounce behavior.
        from qwenpaw.schemas import Message

        native = {
            "channel_id": self.channel,
            "sender_id": sender_display_string(meta["user_name"], sender),
            "acl_sender_id": sender,
            "user_id": meta["feishu_sender_id"],
            "session_id": session_id,
            "content_parts": Message(content=parts).content,
            "meta": meta,
        }
        if self._enqueue is not None:
            self._enqueue(native)
        await self._platform.add_reaction(meta["feishu_message_id"], "Typing")

    async def _handle_card_action(self, value: object) -> None:
        if (
            not self._active
            or self._enqueue is None
            or not isinstance(value, dict)
        ):
            return
        action, request_id, operator = (
            str(value.get(k) or "")
            for k in ("action", "request_id", "operator_open_id")
        )
        if action not in {"approve", "deny"} or not request_id or not operator:
            return
        ctx = value.get("session_ctx") or {}
        sender = str(ctx.get("sender_id") or operator)
        meta = {
            f"feishu_{key}": ctx.get(key, "")
            for key in (
                "sender_id",
                "chat_id",
                "chat_type",
                "receive_id",
                "receive_id_type",
                "thread_id",
                "message_id",
            )
        }
        meta.update(
            feishu_sender_id=operator,
            is_group=bool(ctx.get("is_group")),
            from_card_action=True,
        )
        self._enqueue(
            {
                "channel_id": self.channel,
                "sender_id": operator,
                "user_id": operator,
                "acl_sender_id": operator,
                "session_id": ctx.get("session_id")
                or self.resolve_session_id(sender, meta),
                "content_parts": [
                    TextContent(text=f"/approval {action} {request_id}"),
                ],
                "meta": meta,
            },
        )

    def resolve_session_id(
        self,
        sender_id: str,
        channel_meta: dict | None = None,
    ) -> str:
        meta = channel_meta or {}
        chat_id = str(meta.get("feishu_chat_id") or "").strip()
        if meta.get("feishu_chat_type") == "group" and chat_id:
            return (
                f"{self.app_id[-4:]}_{short_session_id_from_full_id(chat_id)}"
            )
        return (
            short_session_id_from_full_id(sender_id or chat_id)
            if sender_id or chat_id
            else f"{self.channel}:{sender_id}"
        )

    def build_agent_request_from_native(self, native_payload: Any):
        payload = native_payload if isinstance(native_payload, dict) else {}
        meta = dict(payload.get("meta") or {})
        sender = payload.get("sender_id") or ""
        user_id = (
            meta.get("feishu_sender_id") or payload.get("user_id") or sender
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
                    raise ValueError("Feishu approval operator is unavailable")
        request = self.build_agent_request_from_user_content(
            channel_id=payload.get("channel_id") or self.channel,
            sender_id=user_id,
            session_id=payload.get("session_id")
            or self.resolve_session_id(sender, meta),
            content_parts=payload.get("content_parts") or [],
            channel_meta=meta,
        )
        request.channel_meta = meta
        return request

    def merge_native_items(self, items: list[Any]) -> Any:
        if not items:
            return None
        return {
            **items[0],
            **items[-1],
            "acl_sender_id": items[0].get("acl_sender_id", ""),
            "content_parts": [
                part
                for item in items
                for part in item.get("content_parts", [])
            ],
            "meta": dict(items[-1].get("meta") or {}),
        }

    def to_handle_from_target(self, *, user_id: str, session_id: str) -> str:
        return (
            f"feishu:sw:{session_id}"
            if session_id
            else f"feishu:open_id:{user_id}"
        )

    def get_to_handle_from_request(self, request: Any) -> str:
        return self.to_handle_from_target(
            user_id=getattr(request, "user_id", ""),
            session_id=getattr(request, "session_id", ""),
        )

    def get_on_reply_sent_args(self, request: Any, to_handle: str) -> tuple:
        return (
            getattr(request, "user_id", "") or "",
            getattr(request, "session_id", "") or "",
        )

    def _receive_id_store_path(self) -> Path:
        return (
            self._workspace_dir or get_config_path().parent
        ) / "feishu_receive_ids.json"

    def _load_receive_id_store_from_disk(self) -> None:
        path = self._receive_id_store_path()
        if not path.is_file():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            for key, pair in data.items():
                if isinstance(pair, (list, tuple)) and len(pair) >= 2:
                    a, b = str(pair[0]), str(pair[1])
                    self._receive_id_store[key] = (
                        (b, a) if b in {"open_id", "chat_id"} else (a, b)
                    )
        except (OSError, ValueError, AttributeError):
            logger.warning("Cannot load Feishu receive targets", exc_info=True)

    async def _save_receive_id(
        self,
        session_id: str,
        receive_id: str,
        receive_id_type: str,
    ) -> None:
        if not session_id or not receive_id:
            return
        async with self._receive_id_lock:
            self._receive_id_store[session_id] = (receive_id_type, receive_id)
            if receive_id_type == "open_id":
                self._receive_id_store[receive_id] = (
                    receive_id_type,
                    receive_id,
                )
            path = self._receive_id_store_path()
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    json.dumps(
                        self._receive_id_store,
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
            except OSError:
                logger.warning(
                    "Cannot save Feishu receive targets",
                    exc_info=True,
                )

    async def _load_receive_id(self, session_id: str):
        async with self._receive_id_lock:
            if session_id not in self._receive_id_store:
                self._load_receive_id_store_from_disk()
            return self._receive_id_store.get(session_id)

    async def _get_receive_for_send(self, to_handle: str, meta: dict | None):
        meta = meta or {}
        if meta.get("feishu_receive_id"):
            return (
                meta.get("feishu_receive_id_type") or "open_id",
                meta["feishu_receive_id"],
            )
        handle = (to_handle or "").strip()
        if handle.startswith("feishu:sw:"):
            key = handle[len("feishu:sw:") :]
            found = await self._load_receive_id(key)
            if found:
                return found
            if key.startswith("feishu:open_id:"):
                return ("open_id", key[len("feishu:open_id:") :])
            suffix = key.split("#", 1)[-1] if "#" in key else ""
            if len(suffix) >= 4:
                return next(
                    (
                        v
                        for v in self._receive_id_store.values()
                        if v[1].endswith(suffix)
                    ),
                    None,
                )
            return None
        for kind in ("open_id", "chat_id"):
            if handle.startswith(f"feishu:{kind}:"):
                return (kind, handle[len(f"feishu:{kind}:") :])
        return (
            ("chat_id" if handle.startswith("oc_") else "open_id", handle)
            if handle
            else None
        )

    async def _before_consume_process(self, request: Any) -> None:
        meta = getattr(request, "channel_meta", None) or {}
        await self._save_receive_id(
            request.session_id,
            meta.get("feishu_receive_id", ""),
            meta.get("feishu_receive_id_type", "open_id"),
        )

    async def _send_parts(
        self,
        to_handle: str,
        parts: list,
        meta: dict | None,
    ):
        platform = self._require_platform()
        target = await self._get_receive_for_send(to_handle, meta)
        if not target:
            raise ValueError("Feishu receive target is unavailable")
        reply = (
            (meta or {}).get("feishu_message_id", "")
            if (meta or {}).get("feishu_thread_id")
            else ""
        )
        message_id = await platform.send_message(
            *target,
            tuple(parts),
            reply_message_id=reply,
        )
        if meta is not None:
            meta["_last_sent_message_id"] = message_id
        return message_id

    async def send_content_parts(
        self,
        to_handle: str,
        parts: list,
        meta: dict | None = None,
    ) -> str | None:
        texts, media = [], []
        for part in parts:
            value = dict(part) if isinstance(part, dict) else part.model_dump()
            kind = value.get("type")
            if kind in {"text", "refusal"}:
                if text := value.get("text") or value.get("refusal"):
                    texts.append(text)
            elif kind in {"image", "file", "audio", "video"}:
                media.append(value)
        body = "\n".join(texts).strip()
        prefix = (meta or {}).get("bot_prefix") or self.bot_prefix
        if body and prefix:
            body = f"{prefix}  {body}"
        outgoing = ([{"type": "text", "text": body}] if body else []) + media
        if not outgoing:
            self._require_platform()
            return None
        return await self._send_parts(to_handle, outgoing, meta)

    async def send(
        self,
        to_handle: str,
        text: str,
        meta: dict | None = None,
    ) -> None:
        prefix = (meta or {}).get("bot_prefix") or self.bot_prefix
        await self._send_parts(
            to_handle,
            [{"type": "text", "text": f"{prefix}{text}"}],
            meta,
        )

    def _stream_text(self, stream_type: str, text: str, meta: dict) -> str:
        body = f"💭 {text}" if stream_type == "reasoning" and text else text
        prefix = meta.get("bot_prefix") or self.bot_prefix
        return f"{prefix}  {body}" if prefix and body else body

    async def on_streaming_start(
        self,
        request: Any,
        to_handle: str,
        event: Any,
        send_meta: dict,
        stream_type: str,
        accumulated_text: str = "",
    ) -> None:
        if not self.streaming_enabled or send_meta.get("feishu_thread_id"):
            return
        from .platform import FeishuDeliveryError

        platform = self._require_platform()
        target = await self._get_receive_for_send(to_handle, send_meta)
        if not target:
            return
        try:
            card = await platform.start_stream(
                *target,
                self._stream_text(stream_type, "...", send_meta),
                collapsible=stream_type == "reasoning",
            )
        except FeishuDeliveryError as exc:
            if exc.side_effect_possible:
                raise
            return
        if card:
            send_meta.setdefault("_fs_stream", {})[stream_type] = {
                "target": card,
                "sequence": 0,
            }

    async def on_streaming_delta(
        self,
        request: Any,
        to_handle: str,
        event: Any,
        send_meta: dict,
        stream_type: str,
        accumulated_text: str = "",
    ) -> None:
        state = send_meta.get("_fs_stream", {}).get(stream_type)
        if state:
            state["sequence"] += 1
            await self._require_platform().update_stream(
                state["target"],
                self._stream_text(stream_type, accumulated_text, send_meta),
                state["sequence"],
                final=False,
            )

    async def on_streaming_end(
        self,
        request: Any,
        to_handle: str,
        event: Any,
        send_meta: dict,
        stream_type: str,
        accumulated_text: str = "",
    ) -> None:
        state = send_meta.get("_fs_stream", {}).pop(stream_type, None)
        text = self._stream_text(stream_type, accumulated_text, send_meta)
        if state:
            state["sequence"] += 1
            ok = await self._require_platform().update_stream(
                state["target"],
                text,
                state["sequence"],
                final=True,
            )
            if ok:
                send_meta["_last_sent_message_id"] = state["target"][
                    "message_id"
                ]
                if stream_type == "reasoning" and self.auto_collapse_thinking:
                    await self._platform.collapse_reasoning_panel(
                        state["target"]["card_id"],
                        state["sequence"] + 2,
                    )
                return
        if accumulated_text.strip():
            # Already formatted: do not add bot_prefix twice.
            await self._send_parts(
                to_handle,
                [{"type": "text", "text": text}],
                send_meta,
            )

    async def on_event_message_completed(
        self,
        request: Any,
        to_handle: str,
        event: Any,
        send_meta: dict,
    ) -> None:
        metadata = getattr(event, "metadata", None) or {}
        metadata = metadata.get("metadata", metadata)
        if metadata.get(
            "message_type",
        ) != "tool_guard_approval" or not metadata.get("approval_request_id"):
            await super().on_event_message_completed(
                request,
                to_handle,
                event,
                send_meta,
            )
            return
        platform = self._require_platform()
        target = await self._get_receive_for_send(to_handle, send_meta)
        if not target:
            raise ValueError("Feishu receive target is unavailable")
        ctx = {
            key: send_meta.get(f"feishu_{key}", "")
            for key in (
                "sender_id",
                "chat_id",
                "chat_type",
                "thread_id",
                "message_id",
            )
        }
        ctx.update(
            session_id=getattr(request, "session_id", "")
            or to_handle.removeprefix("feishu:sw:"),
            receive_id_type=target[0],
            receive_id=target[1],
            is_group=bool(send_meta.get("is_group")),
        )
        content = getattr(event, "content", None) or []
        body = (
            content
            if isinstance(content, str)
            else "".join(
                (
                    p.get("text", "")
                    if isinstance(p, dict)
                    else getattr(p, "text", "")
                )
                for p in content
            )
        )
        send_meta["_last_sent_message_id"] = await platform.send_approval(
            *target,
            {
                "request_id": metadata["approval_request_id"],
                "tool_name": metadata.get("tool_name", "tool"),
                "severity": metadata.get("severity", "medium"),
            },
            body,
            session_ctx=ctx,
        )

    async def _on_process_completed(
        self,
        request: Any,
        to_handle: str,
        send_meta: dict,
    ) -> None:
        if message_id := send_meta.get("_last_sent_message_id"):
            await self._require_platform().add_reaction(message_id, "DONE")
