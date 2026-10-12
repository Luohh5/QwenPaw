# -*- coding: utf-8 -*-
# pylint: disable=unused-argument,too-many-return-statements
"""SDK-free durable reply lifecycle shared by platform Drivers."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
import json
import uuid

from qwenpaw.channel_protocol.errors import RPC_LIFECYCLE_ERROR, RpcError
from qwenpaw.channel_protocol.models import (
    EventBatchAck,
    EventBatchParams,
    HostStateParams,
    IdentityParams,
    OutboundResult,
    DeliveryState,
    ResponseFinishResult,
)
from qwenpaw.channel_protocol.reliability import RetryPolicy, events_for_retry
from qwenpaw.channel_protocol.response_lifecycle import (
    ResponseCleanupState,
    ResponseResourceRef,
    ResponseRouteAggregate,
    ResponseRouteSnapshot,
    ResponseStateError,
)

_MAX_ROUTES = 128


class SharedResponseDriver:
    """Journal platform intent before effects and retain unknown receipts."""

    def __init__(self, kind, state_key, delivery_error):
        self._kind = kind
        self._state_key = state_key
        self.delivery_error = delivery_error
        self._context = None
        self._platform = None
        self._config = {}
        self._routes = ResponseRouteAggregate(max_entries=_MAX_ROUTES)
        self._locks = {}
        self._checkpoint_lock = asyncio.Lock()
        self._connected = False
        self._stopping = False
        self._connection_generation = 0

    async def _restore_responses(self):
        rows = (await self._state("get", self._state_key))["value"] or []
        if not isinstance(rows, list) or len(rows) > _MAX_ROUTES:
            raise ValueError("RESPONSE_STATE_INVALID")
        for row in rows:
            snapshot = ResponseRouteSnapshot.from_mapping(row)
            if snapshot.cleanup_state is ResponseCleanupState.COMPLETE:
                compact = self._compact_receipt(snapshot)
                if compact != snapshot:
                    snapshot = replace(compact, version=snapshot.version + 1)
            self._routes.restore(snapshot)

    async def _persist_route(self, handle, attrs):
        rows = self._routes.snapshots()
        if len(rows) >= _MAX_ROUTES:
            completed = sorted(
                (row for row in rows if self._evictable(row)),
                key=lambda row: row.closed_at_ms,
            )
            if completed:
                self._evict(completed[0].response_handle)
        for old in self._routes.expired_completed():
            if self._evictable(old):
                self._routes.commit_gc(old)
        snapshot, created = self._routes.open(
            handle,
            [ResponseResourceRef.create(self._kind + ".route", handle, attrs)],
        )
        try:
            await self._checkpoint()
        except BaseException:
            if created:
                self._routes.rollback_open(handle, snapshot.version)
            raise
        return handle

    def bind(self, context):
        self._context = context
        for name, handler in (
            ("channel.send", self._send),
            ("channel.reaction", self._reaction),
            ("channel.response.open", self._open),
            ("channel.response.finish", self._finish),
        ):
            context.peer.register_method(name, handler)

    def _identity(self, value):
        identity = self._context.identity
        if isinstance(value, dict):
            value = IdentityParams.from_mapping(
                {key: value[key] for key in identity.to_mapping()},
            )
        if any(
            getattr(value, key) != getattr(identity, key)
            for key in identity.to_mapping()
        ):
            raise RpcError(
                RPC_LIFECYCLE_ERROR,
                "stale channel generation",
                data={"reason_code": "STALE_GENERATION", "retryable": False},
            )
        return identity

    async def _state(self, operation, key, value=None):
        return await self._context.peer.call(
            "host.state." + operation,
            HostStateParams(
                **self._context.identity.to_mapping(),
                key=key,
                value=value,
            ).to_mapping(),
        )

    async def _diagnostic(self, reason):
        await self._context.peer.notify(
            "channel.diagnostic",
            {
                **self._context.identity.to_mapping(),
                "reason_code": reason,
            },
        )

    async def _checkpoint(self, candidate=None, *, reserve_bytes=0):
        async with self._checkpoint_lock:
            snapshots = {
                row.response_handle: row.to_mapping()
                for row in self._routes.snapshots()
            }
            if candidate is not None:
                snapshots[candidate.response_handle] = candidate.to_mapping()
            while (
                len(
                    json.dumps(
                        list(snapshots.values()),
                        ensure_ascii=False,
                    ).encode(),
                )
                > 768 * 1024 - reserve_bytes
            ):
                completed = sorted(
                    (
                        row
                        for row in self._routes.snapshots()
                        if self._evictable(row)
                        and (
                            candidate is None
                            or row.response_handle != candidate.response_handle
                        )
                    ),
                    key=lambda row: row.closed_at_ms,
                )
                if not completed:
                    raise ValueError("RESPONSE_STATE_CAPACITY")
                self._evict(completed[0].response_handle)
                snapshots.pop(completed[0].response_handle, None)
            await self._state("put", self._state_key, list(snapshots.values()))

    def _lock(self, handle):
        if handle not in self._locks:
            if len(self._locks) >= _MAX_ROUTES:
                for key in tuple(self._locks):
                    if (
                        self._routes.snapshot(key) is None
                        and not self._locks[key].locked()
                    ):
                        self._locks.pop(key)
            if len(self._locks) >= _MAX_ROUTES:
                raise ValueError("RESPONSE_CAPACITY")
            self._locks[handle] = asyncio.Lock()
        return self._locks[handle]

    async def _admit_event(self, event, created):
        generation = self._connection_generation
        policy = RetryPolicy(initial_delay=0.1, max_delay=1.0, max_attempts=5)
        batch = EventBatchParams(uuid.uuid4().hex, (event,))
        uncertain = False
        for attempt in range(1, policy.max_attempts + 1):
            self._check_connection(generation)
            ack = None
            try:
                ack = EventBatchAck.from_mapping(
                    await self._context.peer.call(
                        "event.batch",
                        batch.to_mapping(),
                    ),
                )
                if ack.batch_id not in {None, batch.batch_id}:
                    raise ValueError("HOST_ACK_IDENTITY_MISMATCH")
            except Exception:
                uncertain = True
                ack = None
            self._check_connection(generation)
            if ack and event.event_id in (
                *ack.accepted_event_ids,
                *ack.duplicate_event_ids,
            ):
                return ack, uncertain
            retries, delay = events_for_retry(
                batch,
                ack,
                attempt=attempt,
                policy=policy,
            )
            if not retries:
                rejected = (
                    next(
                        (
                            row
                            for row in ack.rejected_events
                            if row.event_id == event.event_id
                        ),
                        None,
                    )
                    if ack
                    else None
                )
                exhausted = not rejected or rejected.retryable
                if exhausted:
                    await self._diagnostic("HOST_ADMISSION_EXHAUSTED")
                if created and not uncertain:
                    from qwenpaw.channel_protocol.models import ResponseOutcome

                    async with self._lock(event.response_handle):
                        snapshot = self._routes.begin_finish(
                            event.response_handle,
                            ResponseOutcome.CANCELLED,
                        )
                        await self._checkpoint()
                        await self._cleanup(snapshot)
                        self._evict(event.response_handle)
                        await self._checkpoint()
                if not exhausted:
                    return None
                raise RuntimeError("HOST_ADMISSION_EXHAUSTED")
            await asyncio.sleep(delay)
        raise RuntimeError("HOST_ADMISSION_EXHAUSTED")

    def _result(self, delivery_id, state, reason=None, retryable=False):
        return OutboundResult(
            delivery_id,
            DeliveryState(state),
            reason,
            retryable,
        ).to_mapping()

    async def _send(self, params, request):
        self._identity(params)
        handle = params.to_handle
        async with self._lock(handle):
            return await self._delivery(params, self._perform_send)

    async def _delivery(self, params, perform):
        delivery_id, handle = params.delivery_id, params.to_handle
        try:
            self._require_connected()
            snapshot = self._routes.snapshot(handle)
            if snapshot is None:
                return self._result(
                    delivery_id,
                    "failed",
                    "RESPONSE_HANDLE_UNKNOWN",
                )
            fingerprint = hashlib.sha256(
                json.dumps(
                    {
                        key: value
                        for key, value in params.to_mapping().items()
                        if key != "generation"
                    },
                    sort_keys=True,
                ).encode(),
            ).hexdigest()
            previous = next(
                (
                    ref
                    for ref in self._routes.snapshot(handle).resource_refs
                    if ref.kind == self._kind + ".attempt"
                    and ref.resource_id == delivery_id
                ),
                None,
            )
            if previous:
                attrs = dict(previous.attributes)
                if attrs["fingerprint"] != fingerprint:
                    return self._result(
                        delivery_id,
                        "failed",
                        "DELIVERY_CONFLICT",
                    )
                return self._result(
                    delivery_id,
                    attrs["state"],
                    attrs.get("reason_code") or None,
                )
            self._routes.admit_operation(handle)
            intent = {
                "fingerprint": fingerprint,
                "operation": getattr(
                    getattr(params, "operation", None),
                    "value",
                    "reaction",
                ),
                "state": "unknown",
                "reason_code": "PLATFORM_RESULT_UNKNOWN",
                "target_delivery_id": getattr(
                    params,
                    "target_delivery_id",
                    None,
                )
                or "",
                "sequence": str(getattr(params, "sequence", None) or 0),
            }
            self._routes.record_delivery(
                handle,
                [
                    ResponseResourceRef.create(
                        self._kind + ".attempt",
                        delivery_id,
                        intent,
                    ),
                ],
            )
            try:
                # Reserve room for the acknowledged receipt and finish fence.
                await self._checkpoint(reserve_bytes=1024)
            except ValueError as exc:
                if str(exc) == "RESPONSE_STATE_CAPACITY":
                    current = self._routes.snapshot(handle)
                    self._routes.restore(
                        replace(snapshot, version=current.version + 1),
                    )
                raise
        except ResponseStateError as exc:
            return self._result(delivery_id, "failed", exc.reason_code)
        except ValueError:
            return self._result(
                delivery_id,
                "failed",
                (
                    "NOT_CONNECTED"
                    if not self._connected
                    else "LOCAL_ADMISSION_FAILED"
                ),
            )
        result = None
        try:
            resource = await perform(params)
            if resource:
                self._routes.record_delivery(handle, [resource])
            result = self._result(delivery_id, "acknowledged")
        except self.delivery_error as exc:
            result = self._result(
                delivery_id,
                "unknown" if exc.side_effect_possible else "failed",
                exc.reason_code,
                exc.retryable,
            )
        except asyncio.CancelledError:
            await asyncio.shield(self._checkpoint())
            raise
        except Exception:
            result = self._result(
                delivery_id,
                "unknown",
                "PLATFORM_RESULT_UNKNOWN",
            )
        intent.update(
            state=result["state"],
            reason_code=result.get("reason_code", ""),
        )
        self._routes.record_delivery(
            handle,
            [
                ResponseResourceRef.create(
                    self._kind + ".attempt",
                    delivery_id,
                    intent,
                ),
            ],
        )
        try:
            await self._checkpoint()
        except Exception:
            return self._result(
                delivery_id,
                "unknown",
                "CHECKPOINT_RESULT_UNKNOWN",
            )
        return result

    def _route_attrs(self, handle):
        return dict(self._routes.active_route_refs(handle)[0].attributes)

    def _delivery_resource(self, handle, delivery_id):
        return next(
            (
                ref
                for ref in self._routes.snapshot(handle).resource_refs
                if ref.kind == self._kind + ".delivery"
                and ref.resource_id == delivery_id
            ),
            None,
        )

    async def _reaction(self, params, request):
        self._identity(params)
        async with self._lock(params.to_handle):
            return await self._delivery(params, self._perform_reaction)

    async def _finish(self, params, request):
        self._identity(params)
        async with self._lock(params.response_handle):
            snapshot = self._routes.begin_finish(
                params.response_handle,
                params.outcome,
            )
            await self._checkpoint()
            try:
                await self._cleanup(snapshot)
            except Exception as exc:
                await self._diagnostic("RESPONSE_FINISH_FAILED")
                raise RpcError(
                    RPC_LIFECYCLE_ERROR,
                    "response finish handler failed",
                    data={
                        "reason_code": "RESPONSE_FINISH_FAILED",
                        "retryable": True,
                    },
                ) from exc
        return ResponseFinishResult(
            params.response_handle,
            params.outcome,
        ).to_mapping()

    async def _cleanup(self, snapshot):
        if snapshot.cleanup_state is ResponseCleanupState.COMPLETE:
            return
        platform = self._require_connected()
        for ref in snapshot.resource_refs:
            resource = dict(ref.attributes)
            if (
                ref.kind != self._kind + ".delivery"
                or not resource.get("card_id")
                or resource.get("final") == "true"
            ):
                continue
            unknown_end = any(
                attempt.kind == self._kind + ".attempt"
                and dict(attempt.attributes).get("target_delivery_id")
                == ref.resource_id
                and dict(attempt.attributes).get("operation") == "stream.end"
                and dict(attempt.attributes).get("state") == "unknown"
                for attempt in snapshot.resource_refs
            )
            if (
                unknown_end
                or resource.get("unsettled") == "true"
                or resource.get("cleanup") == "unknown"
            ):
                raise RuntimeError("STREAM_CLEANUP_UNKNOWN")
            sequences = [int(resource.get("sequence", "0"))]
            sequences.extend(
                int(dict(attempt.attributes).get("sequence", "0"))
                for attempt in snapshot.resource_refs
                if attempt.kind == self._kind + ".attempt"
                and dict(attempt.attributes).get("target_delivery_id")
                == ref.resource_id
            )
            sequence = max(sequences) + 1
            resource.update(sequence=str(sequence), cleanup="unknown")
            snapshot = self._record_cleanup(
                snapshot,
                ref.resource_id,
                resource,
            )
            await self._checkpoint()
            try:
                if not await platform.update_stream(
                    resource,
                    resource.get("text", ""),
                    sequence,
                    final=True,
                ):
                    raise self.delivery_error(
                        "STREAM_CLEANUP_FAILED",
                        side_effect_possible=False,
                    )
            except self.delivery_error as exc:
                if not exc.side_effect_possible:
                    resource["cleanup"] = "failed"
                    snapshot = self._record_cleanup(
                        snapshot,
                        ref.resource_id,
                        resource,
                    )
                    await self._checkpoint()
                raise
            resource.update(final="true", cleanup="complete")
            snapshot = self._record_cleanup(
                snapshot,
                ref.resource_id,
                resource,
            )
            await self._checkpoint()
        snapshot = await self._before_cleanup_complete(snapshot)
        candidate = self._routes.cleanup_candidate(
            snapshot.response_handle,
            snapshot.outcome,
        )
        candidate = self._compact_receipt(candidate)
        await self._checkpoint(candidate)
        self._routes.commit_cleanup(candidate)

    async def _before_cleanup_complete(self, snapshot):
        return snapshot

    def _record_cleanup(self, snapshot, delivery_id, resource):
        updated = replace(
            snapshot,
            version=snapshot.version + 1,
            resource_refs=tuple(
                sorted(
                    (
                        ResponseResourceRef.create(
                            self._kind + ".delivery",
                            delivery_id,
                            resource,
                        )
                        if ref.kind == self._kind + ".delivery"
                        and ref.resource_id == delivery_id
                        else ref
                    )
                    for ref in snapshot.resource_refs
                ),
            ),
        )
        self._routes.restore(updated)
        return updated

    def _compact_receipt(self, snapshot):
        unknown = tuple(
            ref
            for ref in snapshot.resource_refs
            if ref.kind == self._kind + ".attempt"
            and dict(ref.attributes).get("state") == "unknown"
        )
        return replace(
            snapshot,
            route_refs=(),
            resource_refs=unknown,
            expires_at_ms=(2**63 - 1) if unknown else snapshot.expires_at_ms,
        )

    def _evictable(self, snapshot):
        return (
            snapshot.cleanup_state is ResponseCleanupState.COMPLETE
            and not any(
                ref.kind == self._kind + ".attempt"
                and dict(ref.attributes).get("state") == "unknown"
                for ref in snapshot.resource_refs
            )
        )

    def _evict(self, handle):
        replacement = ResponseRouteAggregate(max_entries=_MAX_ROUTES)
        for row in self._routes.snapshots():
            if row.response_handle != handle:
                replacement.restore(row)
        self._routes = replacement
        lock = self._locks.get(handle)
        if lock is not None and not lock.locked():
            self._locks.pop(handle, None)
