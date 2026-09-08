# -*- coding: utf-8 -*-
# Exercise stream adapters directly, without a network request.
# pylint: disable=protected-access
"""Timing observes streams without changing their payload or lifetime."""

import asyncio
import json
import logging
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openai.types.chat import ChatCompletionChunk

from qwenpaw.providers.dashscope_provider import DashScopeProvider
from qwenpaw.providers import stream_diagnostics
from qwenpaw.providers.retry_chat_model import (
    RetryChatModel,
    RetryConfig,
    StreamIdleTimeoutError,
)


class RawStream:
    def __init__(self, error=None):
        self.error = error
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def __aiter__(self):
        deltas = [
            {},
            {"reasoning_content": "PRIVATE_REASONING"},
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call-test",
                        "type": "function",
                        "function": {
                            "name": "GenerateStructuredOutput",
                            "arguments": '{"answer":',
                        },
                    },
                ],
            },
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "function": {"arguments": '"PRIVATE_ARGUMENT"}'},
                    },
                ],
            },
        ]
        for delta in deltas:
            yield ChatCompletionChunk.model_validate(
                {
                    "id": "request-test",
                    "model": "offline",
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "choices": [
                        {"index": 0, "delta": delta, "finish_reason": None},
                    ],
                },
            )
        if self.error:
            raise self.error("PRIVATE_ERROR")


def model():
    return DashScopeProvider(
        id="dashscope",
        name="DashScope",
        api_key="PRIVATE_KEY",
        base_url="https://example.invalid/v1",
    ).get_chat_model_instance("qwen3.8-max")


def records(caplog):
    return [
        json.loads(r.getMessage().removeprefix("llm_timing "))
        for r in caplog.records
        if r.getMessage().startswith("llm_timing ")
    ]


@pytest.mark.parametrize("enabled", [False, True])
async def test_raw_and_sdk_timing_preserves_payload(
    enabled,
    monkeypatch,
    caplog,
):
    monkeypatch.setenv("QWENPAW_LLM_TIMING_LOG", "1" if enabled else "0")
    caplog.set_level(logging.INFO)
    source, instance = RawStream(), model()
    try:
        chunks = [
            c
            async for c in instance._parse_stream_response(
                datetime.now(),
                source,
            )
        ]
    finally:
        await instance.client.close()
    assert source.closed
    assert [c.content[0].type for c in chunks] == [
        "thinking",
        "tool_call",
        "tool_call",
    ]
    assert chunks[0].content[0].thinking == "PRIVATE_REASONING"
    assert (
        "".join(c.content[0].input for c in chunks[1:])
        == '{"answer":"PRIVATE_ARGUMENT"}'
    )
    entries = records(caplog)
    if not enabled:
        assert entries == []
        return
    assert [e["layer"] for e in entries] == [
        "stream_start",
        "raw",
        "raw",
        "sdk",
        "raw",
        "sdk",
        "raw",
        "sdk",
        "stream_end",
    ]
    assert len({e["stream_id"] for e in entries}) == 1
    assert all(
        e["gap_ms"] >= 0 and e["elapsed_ms"] >= 0 and e["at"] for e in entries
    )
    raw = [e for e in entries if e["layer"] == "raw"]
    assert raw[1]["choices"][0]["thinking_chars"] == len("PRIVATE_REASONING")
    sdk = [e for e in entries if e["layer"] == "sdk"]
    assert sdk[-1]["blocks"][0]["call_id"] == "call-test"
    assert sdk[-1]["blocks"][0]["name"] == "GenerateStructuredOutput"
    assert "PRIVATE_" not in caplog.text


@pytest.mark.parametrize("error", [RuntimeError, asyncio.CancelledError])
async def test_stream_error_is_preserved_and_closed(
    error,
    monkeypatch,
    caplog,
):
    monkeypatch.setenv("QWENPAW_LLM_TIMING_LOG", "1")
    caplog.set_level(logging.INFO)
    source, instance = RawStream(error), model()
    try:
        with pytest.raises(error, match="PRIVATE_ERROR"):
            _ = [
                c
                async for c in instance._parse_stream_response(
                    datetime.now(),
                    source,
                )
            ]
    finally:
        await instance.client.close()
    assert source.closed
    assert records(caplog)[-1]["outcome"] == error.__name__
    assert "PRIVATE_" not in caplog.text


async def test_real_watchdog_still_times_out_before_tool_execution(
    monkeypatch,
    caplog,
):
    monkeypatch.setenv("QWENPAW_LLM_TIMING_LOG", "1")
    caplog.set_level(logging.INFO)

    class PausedStream(RawStream):
        async def __aiter__(self):
            async for chunk in super().__aiter__():
                yield chunk
                if chunk.choices[0].delta.tool_calls:
                    await asyncio.Event().wait()

    source, instance = PausedStream(), model()
    monkeypatch.setattr(
        instance.client.chat.completions,
        "create",
        AsyncMock(return_value=source),
    )
    wrapped = RetryChatModel(
        instance,
        retry_config=RetryConfig(enabled=False),
        stream_idle_timeout=0.05,
    )
    try:
        stream = await wrapped(messages=[])
        with pytest.raises(StreamIdleTimeoutError):
            _ = [chunk async for chunk in stream]
    finally:
        await instance.client.close()
    assert source.closed
    entries = records(caplog)
    assert [e for e in entries if e["layer"] == "sdk"][-1]["blocks"][0][
        "name"
    ] == "GenerateStructuredOutput"
    assert entries[-1]["outcome"] == "CancelledError"
    assert not any(e["layer"] == "tool_start" for e in entries)
    assert "PRIVATE_" not in caplog.text


@pytest.mark.parametrize("error", [RuntimeError, asyncio.CancelledError])
async def test_tool_error_records_end_without_swallowing_error(error, caplog):
    caplog.set_level(logging.INFO)
    call = SimpleNamespace(id="call-test", name="GenerateStructuredOutput")
    agent = SimpleNamespace(state=SimpleNamespace(session_id="session-test"))

    async def fail():
        raise error("PRIVATE_ERROR")
        yield  # pylint: disable=unreachable

    with pytest.raises(error, match="PRIVATE_ERROR"):
        _ = [
            c
            async for c in stream_diagnostics.ToolTiming().on_acting(
                agent,
                {"tool_call": call},
                fail,
            )
        ]
    entries = records(caplog)
    assert [e["layer"] for e in entries] == ["tool_start", "tool_end"]
    assert entries[-1]["outcome"] == error.__name__
    assert "PRIVATE_" not in caplog.text
