# -*- coding: utf-8 -*-
"""Opt-in timing metadata; never log model text or tool arguments."""

import json
import logging
import os
import time
from contextlib import aclosing
from datetime import datetime, timezone
from uuid import uuid4

from agentscope.middleware import MiddlewareBase

logger = logging.getLogger(__name__)


def enabled():
    return os.getenv("QWENPAW_LLM_TIMING_LOG") == "1"


def emit(layer, **fields):
    logger.info(
        "llm_timing %s",
        json.dumps(
            {
                "at": datetime.now(timezone.utc).isoformat(
                    timespec="milliseconds",
                ),
                "layer": layer,
                **fields,
            },
            ensure_ascii=False,
        ),
    )


class StreamTiming:
    def __init__(self, model):
        self.stream_id = uuid4().hex
        self.model = model
        self.started = time.perf_counter()
        self.last = {}
        self.count = {}

    def record(self, layer, **fields):
        now = time.perf_counter()
        self.count[layer] = self.count.get(layer, 0) + 1
        emit(
            layer,
            stream_id=self.stream_id,
            model=self.model,
            seq=self.count[layer],
            elapsed_ms=round((now - self.started) * 1000, 3),
            gap_ms=round((now - self.last.get(layer, self.started)) * 1000, 3),
            **fields,
        )
        self.last[layer] = now


class RawStream:
    """Observe SDK-decoded API chunks before AgentScope parses them."""

    def __init__(self, source, timing):
        self.source, self.timing = source, timing

    async def __aenter__(self):
        await self.source.__aenter__()
        return self

    async def __aexit__(self, *args):
        return await self.source.__aexit__(*args)

    async def __aiter__(self):
        async for chunk in self.source:
            self.timing.record(
                "raw",
                request_id=chunk.id,
                usage=chunk.usage is not None,
                choices=[
                    {
                        "finish_reason": c.finish_reason,
                        "fields": sorted(c.delta.model_fields_set),
                        "thinking_chars": len(
                            getattr(c.delta, "reasoning_content", "") or "",
                        ),
                        "text_chars": len(c.delta.content or ""),
                        "tools": [
                            {
                                "index": t.index,
                                "call_id": t.id,
                                "name": t.function.name,
                                "argument_chars": len(
                                    t.function.arguments or "",
                                ),
                            }
                            for t in c.delta.tool_calls or []
                            if t.function
                        ],
                    }
                    for c in chunk.choices
                ],
            )
            yield chunk


async def parsed_stream(source, timing):
    outcome = "completed"
    try:
        async with aclosing(source):
            async for chunk in source:
                timing.record(
                    "sdk",
                    request_id=chunk.id,
                    is_last=chunk.is_last,
                    blocks=[
                        {
                            "type": b.type,
                            "call_id": b.id if b.type == "tool_call" else None,
                            "name": getattr(b, "name", None),
                            "thinking_chars": len(
                                getattr(b, "thinking", "") or "",
                            ),
                            "text_chars": len(getattr(b, "text", "") or ""),
                            "argument_chars": len(
                                getattr(b, "input", "") or "",
                            ),
                        }
                        for b in chunk.content
                    ],
                )
                yield chunk
    except BaseException as exc:
        outcome = type(exc).__name__
        raise
    finally:
        timing.record("stream_end", outcome=outcome)


class ToolTiming(MiddlewareBase):
    async def on_acting(self, agent, input_kwargs, next_handler):
        call = input_kwargs["tool_call"]
        fields = {
            "session_id": agent.state.session_id,
            "call_id": call.id,
            "name": call.name,
        }
        started, outcome = time.perf_counter(), "completed"
        emit("tool_start", **fields)
        try:
            async with aclosing(next_handler()) as stream:
                async for chunk in stream:
                    outcome = getattr(chunk.state, "value", chunk.state)
                    yield chunk
        except BaseException as exc:
            outcome = type(exc).__name__
            raise
        finally:
            emit(
                "tool_end",
                **fields,
                outcome=outcome,
                elapsed_ms=round((time.perf_counter() - started) * 1000, 3),
            )
