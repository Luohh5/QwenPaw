# -*- coding: utf-8 -*-
# pylint: disable=unused-argument
"""SDK-free BaseChannel adapter for an explicitly owned channel process."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import replace
import hashlib
import json
import logging
import os
from pathlib import Path
import tempfile
import uuid

from qwenpaw.channel_protocol.errors import RpcError
from qwenpaw.channel_protocol.models import (
    DeliveryState,
    EventBatchAck,
    EventBatchParams,
    HostStateParams,
    IdentityParams,
    OutboundResult,
    RejectedEvent,
)
from qwenpaw.channel_protocol.shared import SharedChannelDescriptor
from qwenpaw.schemas import Message
from .base import BaseChannel
from .process_supervisor import ChannelCallUnknown, ChannelProcess, LaunchSpec
from .renderer import ChannelDisplayConfig

logger = logging.getLogger(__name__)

_STATE_MAX_BYTES = 1024 * 1024
_STATE_MAX_KEYS = 64


class ChannelDeliveryError(RuntimeError):
    """Expose a safe, determinate or uncertain platform operation result."""

    def __init__(self, result):
        self.state = result.state.value
        self.reason_code = result.reason_code or "PLATFORM_SEND_FAILED"
        self.side_effect_possible = self.state in {"unknown", "timeout"}
        super().__init__(self.reason_code)


class IsolatedChannelProxy(BaseChannel):
    """Keep queue, policy and Agent execution in the parent process."""

    def __init__(
        self,
        process,
        *,
        launch_spec: LaunchSpec,
        channel_process: ChannelProcess | None = None,
        workspace_dir: Path | None = None,
        on_reply_sent=None,
        display_config=None,
    ):
        descriptor = SharedChannelDescriptor.load(launch_spec.descriptor_path)
        if descriptor.channel_key != launch_spec.channel_key:
            raise ValueError("IDENTITY_MISMATCH")
        self.channel = descriptor.channel_key
        self.descriptor = descriptor
        config = dict(launch_spec.config)
        self.enabled = bool(
            launch_spec.enabled and config.get("enabled", True),
        )
        self.bot_prefix = str(config.get("bot_prefix") or "")
        if self.channel == "dingtalk":
            config["streaming_enabled"] = bool(
                config.get("streaming_enabled")
                and str(config.get("message_type") or "markdown").lower()
                == "card",
            )
        super().__init__(
            process,
            on_reply_sent=on_reply_sent,
            display_config=display_config
            or ChannelDisplayConfig(
                **{
                    key: config[key]
                    for key in ChannelDisplayConfig.__dataclass_fields__
                    if key in config
                },
            ),
            **{
                key: config[key]
                for key in (
                    "dm_policy",
                    "group_policy",
                    "allow_from",
                    "deny_message",
                    "require_mention",
                    "no_text_debounce",
                    "streaming_enabled",
                    "access_control_dm",
                    "access_control_group",
                )
                if key in config
            },
        )
        self._workspace_dir = (
            Path(workspace_dir).expanduser().resolve()
            if workspace_dir
            else None
        )
        if self._workspace_dir:
            config.setdefault("workspace_dir", str(self._workspace_dir))
            config.setdefault("media_dir", str(self._workspace_dir / "media"))
        self._launch_spec = replace(
            launch_spec,
            config=config,
            enabled=self.enabled,
        )
        self.channel_process = channel_process or ChannelProcess()
        self._accepted = OrderedDict()
        self._enqueue_async = None
        self._ingress_open = True
        self.last_stop_result = None
        self._owner_registry = None
        self._admission_wakeup = None
        self._state = {}
        self._state_lock = asyncio.Lock()
        self._ingress_lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()
        self._started = False
        self._response_context = ContextVar(
            f"channel_response_{id(self)}",
            default=None,
        )
        self._buffered_handles = {}
        self._last_diagnostic = None
        self.channel_process.register_notification(
            "channel.diagnostic",
            self._diagnostic,
        )
        self.channel_process.register_method("event.batch", self._event_batch)
        for operation in ("get", "put", "delete"):

            async def handle(params, request, operation=operation):
                return await self._host_state(operation, params)

            self.channel_process.register_method(
                "host.state." + operation,
                handle,
            )

    @classmethod
    def from_config(
        cls,
        process,
        config,
        on_reply_sent=None,
        display_config=None,
        no_text_debounce=True,
        *,
        launch_spec,
        channel_process=None,
        workspace_dir=None,
    ):
        values = (
            config.model_dump()
            if hasattr(config, "model_dump")
            else dict(config)
        )
        values["no_text_debounce"] = no_text_debounce
        return cls(
            process,
            launch_spec=replace(launch_spec, config=values),
            channel_process=channel_process,
            workspace_dir=workspace_dir,
            on_reply_sent=on_reply_sent,
            display_config=display_config,
        )

    def _identity(self):
        snapshot = self.channel_process.health_snapshot()
        return IdentityParams(
            self.channel,
            self._launch_spec.instance_id,
            snapshot["generation"] or self._launch_spec.generation,
        )

    def _check_identity(self, params):
        identity = self._identity()
        if any(
            getattr(params, key) != getattr(identity, key)
            for key in ("channel_key", "instance_id", "generation")
        ):
            raise ValueError("STALE_GENERATION")

    async def start(self):
        async with self._lifecycle_lock:
            if self._started or not self.enabled:
                return
            self.last_stop_result = None
            self._ingress_open = True
            generation = self.channel_process.health_snapshot()["generation"]
            if generation and self._owner_registry is not None:
                raise RuntimeError("CHANNEL_GENERATION_RETIRED")
            if generation:
                self._launch_spec = replace(
                    self._launch_spec,
                    generation=generation + 1,
                )
            await self.channel_process.start(self._launch_spec)
            self._started = True
            await self._wait_connected()

    async def _wait_connected(self):
        async with asyncio.timeout(self._launch_spec.startup_timeout):
            while True:
                snapshot = self.channel_process.health_snapshot()
                if (
                    snapshot["state"] == "ready"
                    and snapshot["platform_state"] == "connected"
                ):
                    return
                if snapshot["state"] != "ready" or snapshot[
                    "platform_state"
                ] in {
                    "failed",
                    "disconnected",
                }:
                    raise RuntimeError("CHANNEL_NOT_CONNECTED")
                await asyncio.sleep(0.01)

    async def stop(self):
        self.last_stop_result = None
        self._ingress_open = False
        if self._admission_wakeup is not None:
            self._admission_wakeup()
        async with self._lifecycle_lock:
            result = await self.channel_process.stop()
            self.last_stop_result = result
            if not result.exited:
                raise RuntimeError("CHANNEL_STOP_INCOMPLETE")
            self._started = False
            if self._owner_registry is not None:
                self._owner_registry.release_channel_owner(
                    self._launch_spec.instance_id,
                    self,
                )
            return result

    def set_enqueue_async(self, callback):
        self._enqueue_async = callback
        if callback is None:
            self._ingress_open = False

    async def health_check(self):
        snapshot = self.channel_process.health_snapshot()
        return {
            "channel": self.channel,
            "status": (
                "healthy"
                if snapshot["state"] == "ready"
                and snapshot["platform_state"] == "connected"
                else "unhealthy"
            ),
            **snapshot,
            "error": snapshot["last_error"],
            "last_diagnostic": self._last_diagnostic,
        }

    async def _diagnostic(self, params, notification):
        identity = self._identity().to_mapping()
        if not isinstance(params, dict) or set(params) != {
            *identity,
            "reason_code",
        }:
            raise ValueError("DIAGNOSTIC_INVALID")
        self._check_identity(
            IdentityParams.from_mapping(
                {key: params[key] for key in identity},
            ),
        )
        reason = params["reason_code"]
        if reason not in {
            "HOST_ADMISSION_EXHAUSTED",
            "RESPONSE_FINISH_FAILED",
            "RESPONSE_FINISH_UNKNOWN",
        }:
            raise ValueError("DIAGNOSTIC_INVALID")
        self._record_diagnostic(reason)

    def _record_diagnostic(self, reason):
        self._last_diagnostic = reason
        logger.warning("Channel diagnostic: %s", reason)

    async def _event_batch(self, params, request):
        from ...utils.io_utils import run_async_to_completion

        # RPC cancellation cannot split queue commit from the dedup record.
        return await run_async_to_completion(self._admit_batch(params))

    async def _admit_batch(self, params):
        batch = (
            params
            if isinstance(params, EventBatchParams)
            else EventBatchParams.from_mapping(params)
        )
        accepted, duplicates, rejected = [], [], list(batch.invalid_events)
        async with self._ingress_lock:
            for event in batch.events:
                try:
                    self._check_identity(event)
                except ValueError:
                    rejected.append(
                        RejectedEvent(
                            event.event_id,
                            "STALE_GENERATION",
                            False,
                        ),
                    )
                    continue
                if event.event_id in self._accepted:
                    duplicates.append(event.event_id)
                    continue
                meta = dict(event.metadata)
                meta["response_handle"] = event.response_handle
                payload = {
                    "channel_id": self.channel,
                    "sender_id": event.sender_id,
                    "acl_sender_id": event.acl_sender_id,
                    "user_id": meta.get("user_id") or event.acl_sender_id,
                    "session_id": meta.get("session_id")
                    or event.conversation["id"],
                    "content_parts": Message(
                        content=list(event.content_parts),
                    ).content,
                    "meta": meta,
                }
                if self.channel == "dingtalk":
                    payload["message_id"] = str(
                        meta.get("message_id") or event.event_id,
                    )
                    if meta.get("session_webhook"):
                        payload["session_webhook"] = meta["session_webhook"]
                if not self._check_group_mention(
                    bool(meta.get("is_group")),
                    meta,
                ):
                    rejected.append(
                        RejectedEvent(
                            event.event_id,
                            "MENTION_REQUIRED",
                            False,
                        ),
                    )
                    continue
                if not self._ingress_open:
                    rejected.append(
                        RejectedEvent(
                            event.event_id,
                            "CHANNEL_STOPPING",
                            True,
                        ),
                    )
                    continue
                if self._enqueue_async is None:
                    rejected.append(
                        RejectedEvent(
                            event.event_id,
                            "QUEUE_UNAVAILABLE",
                            True,
                        ),
                    )
                    continue
                try:
                    result = await self._enqueue_async(payload)
                except Exception:
                    rejected.append(
                        RejectedEvent(
                            event.event_id,
                            "QUEUE_UNAVAILABLE",
                            True,
                        ),
                    )
                    continue
                if not result.accepted:
                    rejected.append(
                        RejectedEvent(
                            event.event_id,
                            result.reason_code,
                            result.retryable,
                        ),
                    )
                    continue
                self._accepted[event.event_id] = None
                while len(self._accepted) > 4096:
                    self._accepted.popitem(last=False)
                accepted.append(event.event_id)
        return EventBatchAck(
            tuple(accepted),
            tuple(duplicates),
            tuple(rejected),
            batch.batch_id,
        ).to_mapping()

    def _state_path(self):
        if self._workspace_dir is None:
            return None
        scope = hashlib.sha256(
            (self.channel + ":" + self._launch_spec.instance_id).encode(),
        ).hexdigest()
        return self._workspace_dir / "channel_state" / (scope + ".json")

    async def _host_state(self, operation, params):
        params = (
            params
            if isinstance(params, HostStateParams)
            else HostStateParams.from_mapping(params)
        )
        self._check_identity(params)
        async with self._state_lock:
            self._check_identity(params)
            task = asyncio.create_task(
                asyncio.to_thread(self._state_operation, operation, params),
            )
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                await asyncio.shield(task)
                raise

    def _state_operation(self, operation, params):
        path = self._state_path()
        if not self._state and path and path.exists():
            with path.open("rb") as source:
                raw = source.read(_STATE_MAX_BYTES + 1)
            if len(raw) > _STATE_MAX_BYTES:
                raise ValueError("HOST_STATE_CAPACITY")
            restored = json.loads(raw)
            if (
                not isinstance(restored, dict)
                or len(restored) > _STATE_MAX_KEYS
            ):
                raise ValueError("HOST_STATE_INVALID")
            if any(
                not isinstance(row, dict)
                or set(row) != {"schema_version", "value"}
                or type(row["schema_version"]) is not int
                or row["schema_version"] < 1
                for row in restored.values()
            ):
                raise ValueError("HOST_STATE_INVALID")
            self._state = restored
        key = params.key
        if operation == "get":
            row = self._state.get(key)
            if row and row["schema_version"] != params.schema_version:
                raise ValueError("HOST_STATE_VERSION")
            return {"value": row["value"] if row else None}
        updated = dict(self._state)
        if operation == "delete":
            updated.pop(key, None)
        else:
            updated[key] = {
                "schema_version": params.schema_version,
                "value": params.value,
            }
        raw = json.dumps(updated, ensure_ascii=False, allow_nan=False).encode()
        if len(updated) > _STATE_MAX_KEYS or len(raw) > _STATE_MAX_BYTES:
            raise ValueError("HOST_STATE_CAPACITY")
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                dir=path.parent,
                delete=False,
            ) as output:
                temporary = Path(output.name)
                try:
                    output.write(raw)
                    output.flush()
                    os.fsync(output.fileno())
                    os.replace(temporary, path)
                finally:
                    temporary.unlink(missing_ok=True)
        self._state = updated
        return {"stored": True}

    def _debounce_payload(self, payload):
        ready = super()._debounce_payload(payload)
        if isinstance(payload, dict):
            meta = payload.setdefault("meta", {})
        else:
            meta = getattr(payload, "channel_meta", None)
            if meta is None:
                meta = {}
                payload.channel_meta = meta
        session_id = self.get_debounce_key(payload)
        if ready:
            handles = self._buffered_handles.pop(session_id, [])
            if handles:
                meta["_merged_response_handles"] = list(
                    dict.fromkeys(
                        [
                            *handles,
                            *(meta.get("_merged_response_handles") or []),
                        ],
                    ),
                )
        else:
            handles = self._buffered_handles.setdefault(session_id, [])
            for handle in [
                *meta.get("_merged_response_handles", []),
                meta.get("response_handle"),
            ]:
                if handle and handle not in handles:
                    handles.append(handle)
        return ready

    def merge_native_items(self, items):
        if not items:
            return None
        meta = dict(items[-1].get("meta") or {})
        handles = []
        for item in items:
            current = item.get("meta") or {}
            handles.extend(current.get("_merged_response_handles") or [])
            if current.get("response_handle"):
                handles.append(current["response_handle"])
        meta["_merged_response_handles"] = list(dict.fromkeys(handles))
        return {
            **items[0],
            **items[-1],
            "acl_sender_id": items[0].get("acl_sender_id", ""),
            "content_parts": [
                part
                for item in items
                for part in item.get("content_parts", [])
            ],
            "meta": meta,
        }

    def build_agent_request_from_native(self, native_payload):
        meta = dict(native_payload.get("meta") or {})
        user_id = (
            native_payload.get("user_id")
            or meta.get("user_id")
            or meta.get("feishu_sender_id")
            or native_payload.get("sender_id", "")
        )
        query = self._extract_query_from_payload(native_payload)
        if query.lstrip().startswith("/"):
            from qwenpaw.runtime.commands.control import (
                _COMMAND_REGISTRY,
                _extract_command_token,
                ApprovalCommandHandler,
                ApproveCommandHandler,
                DenyCommandHandler,
            )

            if isinstance(
                _COMMAND_REGISTRY.get(_extract_command_token(query)),
                (
                    ApprovalCommandHandler,
                    ApproveCommandHandler,
                    DenyCommandHandler,
                ),
            ):
                user_id = str(
                    native_payload.get("acl_sender_id") or "",
                ).strip()
                if not user_id:
                    raise ValueError("APPROVAL_OPERATOR_UNAVAILABLE")
        request = self.build_agent_request_from_user_content(
            channel_id=self.channel,
            sender_id=user_id,
            session_id=native_payload.get("session_id")
            or self.resolve_session_id(user_id, meta),
            content_parts=native_payload.get("content_parts") or [],
            channel_meta=meta,
        )
        request.channel_meta = meta
        return request

    def resolve_session_id(self, sender_id, channel_meta=None):
        meta = channel_meta or {}
        if meta.get("session_id"):
            return meta["session_id"]
        if self.channel == "dingtalk":
            cid = str(meta.get("conversation_id") or "")
            if cid:
                return (
                    hashlib.sha256(cid.encode()).hexdigest()[:16]
                    if meta.get("is_group")
                    and self._launch_spec.config.get("share_session_in_group")
                    else cid[-8:]
                )
            return f"dingtalk:{sender_id}"
        if self.channel == "feishu":
            chat = str(meta.get("feishu_chat_id") or "")
            if meta.get("feishu_chat_type") == "group" and chat:
                app_suffix = str(self._launch_spec.config.get("app_id") or "")[
                    -4:
                ]
                return f"{app_suffix}_{chat[-8:]}"
            return (
                str(sender_id or chat)[-8:]
                if sender_id or chat
                else f"{self.channel}:{sender_id}"
            )
        return super().resolve_session_id(sender_id, meta)

    def get_debounce_key(self, payload):
        key = super().get_debounce_key(payload)
        if self.channel == "dingtalk" and isinstance(payload, dict):
            meta = payload.get("meta") or {}
            if not (
                meta.get("is_group")
                and self._launch_spec.config.get("share_session_in_group")
            ) and payload.get("sender_id"):
                return key + ":" + payload["sender_id"]
        return key

    def to_handle_from_target(self, *, user_id, session_id):
        if self.channel == "dingtalk":
            return (
                f"dingtalk:sw:{user_id}_{session_id}"
                if user_id
                else f"dingtalk:sw:{session_id}"
            )
        return (
            f"{self.channel}:sw:{session_id}"
            if session_id
            else f"{self.channel}:open_id:{user_id}"
        )

    def get_to_handle_from_request(self, request):
        meta = getattr(request, "channel_meta", None) or {}
        return meta.get("response_handle") or self.to_handle_from_target(
            user_id=request.user_id,
            session_id=request.session_id,
        )

    def get_on_reply_sent_args(self, request, to_handle):
        return request.user_id, request.session_id

    async def _route(self, to_handle, meta):
        await self._wait_connected()
        if meta.get("response_handle"):
            return meta["response_handle"]
        try:
            result = await self.channel_process.call(
                "channel.response.open",
                {
                    **self._identity().to_mapping(),
                    "to_handle": to_handle,
                    "meta": meta,
                },
            )
        except RpcError as exc:
            if (
                isinstance(exc.data, dict)
                and exc.data.get("reason_code") == "OUTBOUND_TARGET_UNKNOWN"
            ):
                raise ChannelDeliveryError(
                    OutboundResult(
                        "unroutable",
                        DeliveryState.FAILED,
                        "OUTBOUND_TARGET_UNKNOWN",
                    ),
                ) from exc
            raise
        meta["response_handle"] = result["response_handle"]
        return result["response_handle"]

    async def _send_operation(self, to_handle, meta, operation, **fields):
        meta = meta if meta is not None else {}
        if self._response_context.get() is None and not meta.get(
            "response_handle",
        ):
            try:
                async with self._response_scope(meta):
                    return await self._send_operation_owned(
                        to_handle,
                        meta,
                        operation,
                        **fields,
                    )
            finally:
                if not meta.get("_reply_unknown"):
                    meta.pop("response_handle", None)
        return await self._send_operation_owned(
            to_handle,
            meta,
            operation,
            **fields,
        )

    async def _send_operation_owned(
        self,
        to_handle,
        meta,
        operation,
        **fields,
    ):
        scope = self._response_context.get()
        if scope is not None:
            scope["meta"] = meta
        route = await self._route(to_handle, meta)
        fingerprint = hashlib.sha256(
            json.dumps([route, operation, fields], sort_keys=True).encode(),
        ).hexdigest()
        pending = meta.get("_pending_delivery")
        if meta.get("_reply_unknown") and (
            not pending or pending["fingerprint"] != fingerprint
        ):
            raise ChannelDeliveryError(
                OutboundResult(
                    "unsettled",
                    DeliveryState.UNKNOWN,
                    "PLATFORM_RESULT_UNKNOWN",
                ),
            )
        delivery_id = (
            pending["delivery_id"]
            if pending and pending["fingerprint"] == fingerprint
            else uuid.uuid4().hex
        )
        meta["_pending_delivery"] = {
            "delivery_id": delivery_id,
            "fingerprint": fingerprint,
        }
        try:
            result = OutboundResult.from_mapping(
                await self.channel_process.call(
                    "channel.send",
                    {
                        **self._identity().to_mapping(),
                        "delivery_id": delivery_id,
                        "to_handle": route,
                        "operation": operation,
                        **fields,
                    },
                ),
            )
            if result.state.value != "acknowledged":
                error = ChannelDeliveryError(result)
                if error.side_effect_possible:
                    meta["_reply_unknown"] = True
                else:
                    meta.pop("_pending_delivery", None)
                raise error
        except (ChannelCallUnknown, asyncio.CancelledError):
            meta["_reply_unknown"] = True
            raise
        meta.pop("_pending_delivery", None)
        meta["_last_delivery_id"] = delivery_id
        return delivery_id

    async def _finish_route(self, meta, outcome):
        handles = list(meta.get("_merged_response_handles") or [])
        if meta.get("response_handle"):
            handles.append(meta["response_handle"])
        failure = None
        for handle in dict.fromkeys(handles):
            try:
                await self.channel_process.call(
                    "channel.response.finish",
                    {
                        **self._identity().to_mapping(),
                        "response_handle": handle,
                        "outcome": outcome,
                    },
                )
            except Exception as exc:
                failure = exc
        if failure is not None:
            raise failure

    async def send(self, to_handle, text, meta=None):
        meta = meta if meta is not None else {}
        prefix = meta.get("bot_prefix") or self.bot_prefix
        await self._send_operation(
            to_handle,
            meta,
            "message.create",
            content_parts=[{"type": "text", "text": prefix + text}],
        )

    async def send_content_parts(self, to_handle, parts, meta=None):
        meta = meta if meta is not None else {}
        texts, media = [], []
        for part in parts:
            value = (
                dict(part)
                if isinstance(part, dict)
                else part.model_dump(mode="json", exclude_none=True)
            )
            kind = value.get("type")
            if kind in {"text", "refusal"}:
                if body := value.get("text") or value.get("refusal"):
                    texts.append(body)
            elif kind in {"image", "file", "audio", "video"}:
                locator = "data" if kind == "audio" else kind + "_url"
                media.append(
                    {
                        key: value[key]
                        for key in (
                            "type",
                            locator,
                            "filename",
                            "mime_type",
                            "format",
                        )
                        if key in value
                    },
                )
        body = "\n".join(texts).strip()
        prefix = meta.get("bot_prefix") or self.bot_prefix
        if prefix and body:
            body = prefix + "  " + body
        outgoing = ([{"type": "text", "text": body}] if body else []) + media
        if not outgoing:
            return None
        result = await self._send_operation(
            to_handle,
            meta,
            "message.create",
            content_parts=outgoing,
        )
        return result

    async def _access_control_gate(self, payload):
        blocked = await super()._access_control_gate(payload)
        if blocked:
            meta = (
                payload.get("meta", {})
                if isinstance(payload, dict)
                else getattr(payload, "channel_meta", {})
            )
            await self._finish_route(meta, "completed")
        return blocked

    async def _on_consume_error(self, request, to_handle, err_text):
        scope = self._response_context.get()
        meta = (
            scope["meta"]
            if scope is not None
            else getattr(request, "channel_meta", None) or {}
        )
        request.channel_meta = meta
        meta["_reply_failed"] = True
        if not meta.get("_reply_unknown"):
            await super()._on_consume_error(request, to_handle, err_text)

    async def _before_consume_process(self, request):
        if self.channel != "dingtalk":
            return
        scope = self._response_context.get()
        meta = getattr(request, "channel_meta", None) or {}
        if scope is not None:
            scope["meta"] = meta
        if not meta.get("message_id") or not meta.get("conversation_id"):
            return
        route = await self._route(
            self.get_to_handle_from_request(request),
            meta,
        )
        try:
            result = OutboundResult.from_mapping(
                await self.channel_process.call(
                    "channel.response.prepare",
                    {
                        **self._identity().to_mapping(),
                        "response_handle": route,
                        "delivery_id": hashlib.sha256(
                            (route + ":prepare").encode(),
                        ).hexdigest(),
                    },
                ),
            )
            if result.state.value != "acknowledged":
                error = ChannelDeliveryError(result)
                if error.side_effect_possible:
                    meta["_reply_unknown"] = True
                    raise error
                # Ordinary precreation refusal is retryable by Agent hooks.
                return
        except (ChannelCallUnknown, asyncio.CancelledError):
            meta["_reply_unknown"] = True
            raise

    def _stream_text(self, stream_type, text, meta):
        if stream_type == "reasoning" and text:
            text = "💭 " + text
        prefix = meta.get("bot_prefix") or self.bot_prefix
        return prefix + "  " + text if prefix and text else text

    async def on_streaming_start(
        self,
        request,
        to_handle,
        event,
        send_meta,
        stream_type,
        accumulated_text="",
    ):
        if not self.streaming_enabled or send_meta.get("feishu_thread_id"):
            return
        try:
            delivery = await self._send_operation(
                to_handle,
                send_meta,
                "stream.start",
                stream_type=stream_type,
                sequence=0,
                accumulated_text=self._stream_text(
                    stream_type,
                    "...",
                    send_meta,
                ),
            )
        except ChannelDeliveryError as exc:
            if exc.side_effect_possible:
                raise
            return
        send_meta.setdefault("_isolated_streams", {})[stream_type] = {
            "delivery_id": delivery,
            "sequence": 0,
        }

    async def on_streaming_delta(
        self,
        request,
        to_handle,
        event,
        send_meta,
        stream_type,
        accumulated_text="",
    ):
        state = send_meta.get("_isolated_streams", {}).get(stream_type)
        if state:
            state["sequence"] += 1
            await self._send_operation(
                to_handle,
                send_meta,
                "stream.delta",
                stream_type=stream_type,
                target_delivery_id=state["delivery_id"],
                sequence=state["sequence"],
                accumulated_text=self._stream_text(
                    stream_type,
                    accumulated_text,
                    send_meta,
                ),
            )

    async def on_streaming_end(
        self,
        request,
        to_handle,
        event,
        send_meta,
        stream_type,
        accumulated_text="",
    ):
        state = send_meta.get("_isolated_streams", {}).get(stream_type)
        if state:
            state["sequence"] += 1
            await self._send_operation(
                to_handle,
                send_meta,
                "stream.end",
                stream_type=stream_type,
                target_delivery_id=state["delivery_id"],
                sequence=state["sequence"],
                accumulated_text=self._stream_text(
                    stream_type,
                    accumulated_text,
                    send_meta,
                ),
            )
            send_meta["_last_delivery_id"] = state["delivery_id"]
            send_meta["_isolated_streams"].pop(stream_type, None)
        elif accumulated_text.strip():
            text = (
                f"💭 {accumulated_text}"
                if stream_type == "reasoning"
                else accumulated_text
            )
            await self.send_content_parts(
                to_handle,
                [{"type": "text", "text": text}],
                send_meta,
            )

    async def on_event_message_completed(
        self,
        request,
        to_handle,
        event,
        send_meta,
    ):
        metadata = getattr(event, "metadata", None) or {}
        metadata = metadata.get("metadata", metadata)
        if metadata.get(
            "message_type",
        ) == "tool_guard_approval" and metadata.get("approval_request_id"):
            content = getattr(event, "content", None) or []
            body = (
                content
                if isinstance(content, str)
                else "".join(
                    (
                        part.get("text", "")
                        if isinstance(part, dict)
                        else getattr(part, "text", "")
                    )
                    for part in content
                )
            )
            await self._send_operation(
                to_handle,
                send_meta,
                "message.create",
                approval={
                    "request_id": metadata["approval_request_id"],
                    "tool_name": metadata.get("tool_name", "tool"),
                    "severity": metadata.get("severity", "medium"),
                },
                content_parts=[{"type": "text", "text": body}],
            )
        else:
            await super().on_event_message_completed(
                request,
                to_handle,
                event,
                send_meta,
            )

    async def _on_process_completed(self, request, to_handle, send_meta):
        if send_meta.get("_reply_unknown"):
            raise ChannelDeliveryError(
                OutboundResult(
                    "unsettled",
                    DeliveryState.UNKNOWN,
                    "PLATFORM_RESULT_UNKNOWN",
                ),
            )
        if self.channel == "dingtalk" or not send_meta.get(
            "_last_delivery_id",
        ):
            return
        try:
            result = OutboundResult.from_mapping(
                await self.channel_process.call(
                    "channel.reaction",
                    {
                        **self._identity().to_mapping(),
                        "delivery_id": uuid.uuid4().hex,
                        "to_handle": send_meta["response_handle"],
                        "target_delivery_id": send_meta["_last_delivery_id"],
                        "reaction": "completed",
                    },
                ),
            )
            if result.state.value != "acknowledged":
                error = ChannelDeliveryError(result)
                if error.side_effect_possible:
                    send_meta["_reply_unknown"] = True
                raise error
        except (ChannelCallUnknown, asyncio.CancelledError):
            send_meta["_reply_unknown"] = True
            raise

    @asynccontextmanager
    async def _response_scope(self, meta):
        scope = {"meta": meta}
        token = self._response_context.set(scope)
        outcome = "completed"
        try:
            yield
        except (asyncio.CancelledError, GeneratorExit):
            outcome = "cancelled"
            raise
        except BaseException:
            outcome = "failed"
            raise
        finally:
            current = scope["meta"]
            if outcome == "completed" and (
                current.get("_reply_failed") or current.get("_reply_unknown")
            ):
                outcome = "failed"
            try:
                if current.get("response_handle"):
                    task = asyncio.create_task(
                        self._finish_route(current, outcome),
                    )
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        task.add_done_callback(self._observe_cleanup)
                        raise
                    except Exception:
                        self._record_diagnostic("RESPONSE_FINISH_FAILED")
            finally:
                self._response_context.reset(token)

    def _observe_cleanup(self, task):
        if task.cancelled() or task.exception() is not None:
            self._record_diagnostic("RESPONSE_FINISH_FAILED")

    async def _run_process_loop(self, request, to_handle, send_meta):
        request.channel_meta = send_meta
        async with self._response_scope(send_meta):
            await super()._run_process_loop(request, to_handle, send_meta)

    async def _stream_with_tracker(self, payload):
        meta = (
            dict(payload.get("meta") or {})
            if isinstance(payload, dict)
            else dict(getattr(payload, "channel_meta", None) or {})
        )
        async with self._response_scope(meta):
            iterator = super()._stream_with_tracker(payload)
            try:
                async for item in iterator:
                    yield item
            finally:
                await iterator.aclose()
