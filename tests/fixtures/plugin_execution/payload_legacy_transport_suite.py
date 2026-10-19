"""Exact selected old framing/publication oracles, execution imports only."""
from __future__ import annotations
import asyncio
import pytest
from qwenpaw.plugins.execution.framing import FrameReader, FramedTransport, FramingLimits, encode_frame
from qwenpaw.plugins.execution.errors import FrameClosedError, FrameTimeoutError, FrameWriteError

class FakeWriter:
    """Minimal asyncio StreamWriter substitute with controllable drain."""

    def __init__(self) -> None:
        self.frames: list[bytes] = []
        self.closed = False
        self.drain_started = asyncio.Event()
        self.drain_release = asyncio.Event()
        self.drain_release.set()
        self.write_error: BaseException | None = None
        self.drain_error: BaseException | None = None
        self.active_drains = 0
        self.max_active_drains = 0

    def write(self, data: bytes) -> None:
        """Record an atomic transport write."""
        if self.closed:
            raise BrokenPipeError("writer closed")
        if self.write_error is not None:
            raise self.write_error
        self.frames.append(data)

    async def drain(self) -> None:
        """Wait until the test permits the pending write to finish."""
        self.active_drains += 1
        self.max_active_drains = max(
            self.max_active_drains,
            self.active_drains,
        )
        self.drain_started.set()
        try:
            await self.drain_release.wait()
            if self.drain_error is not None:
                raise self.drain_error
        finally:
            self.active_drains -= 1

    def close(self) -> None:
        """Close the fake write side."""
        self.closed = True
        self.drain_release.set()

    async def wait_closed(self) -> None:
        """Match the asyncio writer shutdown API."""


def _idle_reader() -> asyncio.StreamReader:
    """Return an open reader with no available bytes."""
    return asyncio.StreamReader()


def _body(frame: bytes) -> str:
    """Extract a body from a frame emitted by the writer."""
    _, body = frame.split(b"\r\n\r\n", 1)
    return body.decode("utf-8")


def _record_prepared_frame(events: list[str]) -> str:
    """Record preparation and return a replacement test frame."""
    events.append("prepared")
    return '{"state":"acknowledged"}'


@pytest.mark.asyncio
async def test_concurrent_sends_use_one_non_interleaving_writer() -> None:
    """Concurrent callers produce complete frames in queue order."""
    writer = FakeWriter()
    transport = FramedTransport(_idle_reader(), writer)
    messages = [f'{{"index":{index}}}' for index in range(20)]

    await asyncio.gather(*(transport.send(message) for message in messages))

    assert [_body(frame) for frame in writer.frames] == messages
    assert writer.max_active_drains == 1
    await transport.aclose()


@pytest.mark.asyncio
async def test_write_boundary_prepares_before_frame_visibility() -> None:
    """Preparation and publication run before drain can yield control."""
    writer = FakeWriter()
    writer.drain_release.clear()
    transport = FramedTransport(_idle_reader(), writer)
    events: list[str] = []

    pending = asyncio.create_task(
        transport.send(
            '{"state":"provisional"}',
            prepare_write=lambda: _record_prepared_frame(events),
            on_write_succeeded=lambda: events.append("published"),
            on_write_failed=lambda: events.append("failed"),
        ),
    )
    await writer.drain_started.wait()

    assert _body(writer.frames[0]) == '{"state":"acknowledged"}'
    assert events == ["prepared", "published"]
    writer.drain_release.set()
    await pending
    await transport.aclose()


@pytest.mark.asyncio
async def test_write_failure_rolls_back_before_close_wait() -> None:
    """A rejected writer frame rolls back before asynchronous cleanup."""
    writer = FakeWriter()
    writer.write_error = BrokenPipeError("peer exited")
    transport = FramedTransport(_idle_reader(), writer)
    events: list[str] = []

    with pytest.raises(FrameWriteError):
        await transport.send(
            '{"state":"provisional"}',
            prepare_write=lambda: _record_prepared_frame(events),
            on_write_succeeded=lambda: events.append("published"),
            on_write_failed=lambda: events.append("failed"),
        )

    assert events == ["prepared", "failed"]
    await transport.aclose()


@pytest.mark.asyncio
async def test_drain_failure_does_not_rollback_visible_frame() -> None:
    """Drain failure cannot retract a frame accepted by writer.write."""
    writer = FakeWriter()
    writer.drain_error = BrokenPipeError("peer exited")
    transport = FramedTransport(_idle_reader(), writer)
    events: list[str] = []

    with pytest.raises(FrameWriteError):
        await transport.send(
            '{"state":"provisional"}',
            prepare_write=lambda: _record_prepared_frame(events),
            on_write_succeeded=lambda: events.append("published"),
            on_write_failed=lambda: events.append("failed"),
        )

    assert _body(writer.frames[0]) == '{"state":"acknowledged"}'
    assert events == ["prepared", "published"]
    await transport.aclose()


@pytest.mark.asyncio
async def test_bounded_queue_times_out_and_closes_transport() -> None:
    """A stalled peer applies bounded backpressure to producers."""
    writer = FakeWriter()
    writer.drain_release.clear()
    limits = FramingLimits(write_queue_size=1, write_timeout=0.02)
    transport = FramedTransport(_idle_reader(), writer, limits=limits)
    first = asyncio.create_task(transport.send('{"index":1}'))
    await writer.drain_started.wait()
    second = asyncio.create_task(transport.send('{"index":2}'))
    await asyncio.sleep(0)

    with pytest.raises(FrameTimeoutError):
        await transport.send('{"index":3}')

    results = await asyncio.gather(first, second, return_exceptions=True)
    assert all(
        isinstance(result, (FrameClosedError, FrameTimeoutError))
        for result in results
    )
    assert transport.is_closed
    assert writer.closed
    await transport.aclose()


@pytest.mark.asyncio
async def test_drain_timeout_closes_transport() -> None:
    """A stalled OS pipe terminates the whole IPC transport."""
    writer = FakeWriter()
    writer.drain_release.clear()
    limits = FramingLimits(write_timeout=0.01)
    transport = FramedTransport(_idle_reader(), writer, limits=limits)

    with pytest.raises(FrameTimeoutError):
        await transport.send("{}")

    assert transport.is_closed
    assert writer.closed
    await transport.aclose()


@pytest.mark.asyncio
async def test_explicit_close_fails_pending_writes() -> None:
    """Closing never leaves callers waiting on queued write futures."""
    writer = FakeWriter()
    writer.drain_release.clear()
    transport = FramedTransport(_idle_reader(), writer)
    pending = asyncio.create_task(transport.send("{}"))
    await writer.drain_started.wait()

    await transport.aclose()

    with pytest.raises(FrameClosedError):
        await pending


@pytest.mark.asyncio
async def test_cancelled_receive_releases_stream_reader() -> None:
    """Caller cancellation leaves no read task attached to the stream."""
    reader = asyncio.StreamReader()
    transport = FramedTransport(reader, FakeWriter())
    pending = asyncio.create_task(transport.receive())
    await asyncio.sleep(0)

    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    reader.feed_data(b"Content-Length: 2\r\n\r\n{}")

    assert await asyncio.wait_for(transport.receive(), timeout=0.1) == "{}"
    await transport.aclose()


def _reader_with_data(
    *chunks: bytes,
    eof: bool = True,
) -> asyncio.StreamReader:
    """Build a StreamReader with deterministic input chunks."""
    reader = asyncio.StreamReader()
    for chunk in chunks:
        reader.feed_data(chunk)
    if eof:
        reader.feed_eof()
    return reader


@pytest.mark.asyncio
async def test_reader_handles_fragmented_and_sticky_frames() -> None:
    """One reader preserves partial and surplus bytes across messages."""
    first = encode_frame('{"first":"é"}')
    second = encode_frame("[1,2,3]")
    reader = _reader_with_data(
        first[:1],
        first[1:12],
        first[12:] + second[:9],
        second[9:],
    )
    framing = FrameReader(reader)

    assert await framing.read_message() == '{"first":"é"}'
    assert await framing.read_message() == "[1,2,3]"
