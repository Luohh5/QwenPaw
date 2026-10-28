# -*- coding: utf-8 -*-
"""Bounded, best-effort worker stderr forwarding to host logging."""

from __future__ import annotations

import asyncio
import atexit
import logging
import queue
import re
import threading
from time import monotonic

_LINE_BYTES = 4096
_LINES_PER_SECOND = 32
_BYTES_PER_SECOND = 64 * 1024
_QUEUE_SIZE = 256
_TRUNCATED = " [truncated]"
_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_CONTROLS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u2028\u2029]")
_HEADERS = re.compile(
    r"(\b(?:(?:proxy-)?authorization|(?:set-)?cookie)"
    r"\b[\"']?\s*[:=]\s*).*",
    re.I,
)
_BEARER = re.compile(r"\b(Bearer|Basic)\s+[^\s,;\"'<>]+", re.I)
_TELEGRAM = re.compile(r"(/bot)\d+:[A-Za-z0-9_-]+", re.I)
_CREDENTIAL = re.compile(
    r"(\b(?:[\w.-]*(?:token|secret|password|passwd|api[_-]?key)"
    r"[\w.-]*|authorization|cookie)\b[\"']?\s*[:=]\s*)"
    r"(?:\"[^\"]*(?:\"|$)|'[^']*(?:'|$)|[^\s&,;}]+)",
    re.I,
)
_URL_AUTH = re.compile(r"(https?://)[^/\s@]+@", re.I)


def _clean(text: str) -> str:
    # This boundary handles recognizable credentials, not arbitrary SDK secrets.
    text = _CONTROLS.sub("", _ANSI.sub("", text)).replace("\n", " ")
    text = _HEADERS.sub(r"\1[REDACTED]", text)
    text = _TELEGRAM.sub(r"\1[REDACTED]", text)
    text = _BEARER.sub(r"\1 [REDACTED]", text)
    text = _CREDENTIAL.sub(r"\1[REDACTED]", text)
    return _URL_AUTH.sub(r"\1[REDACTED]@", text)


def _prefix_value(value: object) -> str:
    return _clean(str(value)).encode("utf-8")[:128].decode("utf-8", "ignore")


class _LogSink:
    """One host-owned consumer; never wait on handlers from the event loop."""

    def __init__(self) -> None:
        self.queue: queue.Queue[tuple[str, threading.Event]] = queue.Queue(
            maxsize=_QUEUE_SIZE,
        )
        self.closed = threading.Event()
        self.thread = threading.Thread(
            target=self._consume,
            name="qwenpaw-worker-logs",
            daemon=True,
        )
        self.thread.start()
        atexit.register(self.close)

    def submit(self, message: str) -> threading.Event | None:
        if self.closed.is_set():
            return None
        delivered = threading.Event()
        try:
            self.queue.put_nowait((message, delivered))
        except queue.Full:
            return None
        return delivered

    def _consume(self) -> None:
        logger = logging.getLogger("qwenpaw.plugins.worker")
        while not self.closed.is_set() or not self.queue.empty():
            try:
                message, delivered = self.queue.get(timeout=0.05)
            except queue.Empty:
                continue
            try:
                logger.warning("%s", message)
            except Exception:
                # A broken handler must not kill forwarding or pipe draining.
                pass
            finally:
                delivered.set()
                self.queue.task_done()

    def close(self) -> None:
        self.closed.set()
        # Bound this facility's exit work. Existing logging.shutdown can still
        # wait on a custom handler's own lock; do not alter global logging.
        self.thread.join(timeout=0.2)


_sink: _LogSink | None = None
_sink_lock = threading.Lock()


def _submit(message: str) -> threading.Event | None:
    global _sink
    if _sink is None:
        with _sink_lock:
            if _sink is None:
                _sink = _LogSink()
    return _sink.submit(message)


class WorkerLogs:
    """One bounded line/rate accumulator with an immutable launch identity."""

    def __init__(self, spec: object, mode: str) -> None:
        fields = [
            f"{name}={_prefix_value(getattr(spec, name))}"
            for name in ("plugin_id", "instance_id", "generation")
        ]
        fields.append(f"mode={_prefix_value(mode)}")
        workspace = getattr(spec, "workspace_id", None)
        if workspace is not None:
            fields.append(f"workspace_id={_prefix_value(workspace)}")
        self.prefix = "[worker stderr " + " ".join(fields) + "] "
        self.buffer = bytearray()
        self.line_bytes = 0
        self.window = monotonic()
        self.lines = self.bytes = 0
        self.omitted_lines = self.omitted_bytes = 0
        self.last_delivery: threading.Event | None = None

    def feed(self, chunk: bytes) -> None:
        for index, part in enumerate(chunk.split(b"\n")):
            if index:
                self._line()
            self.line_bytes += len(part)
            remaining = _LINE_BYTES - len(self.buffer)
            if remaining > 0:
                self.buffer.extend(part[:remaining])

    def _refresh(self) -> None:
        now = monotonic()
        if now - self.window >= 1.0:
            self.window, self.lines, self.bytes = now, 0, 0

    def _offer(self, text: str, *, summary: bool = False) -> bool:
        message = self.prefix + text
        size = len(message.encode("utf-8"))
        # Reserve one record and 256 bytes for a merged omission summary.
        line_limit = _LINES_PER_SECOND - (0 if summary else 1)
        byte_limit = _BYTES_PER_SECOND - (0 if summary else 256)
        if self.lines >= line_limit or self.bytes + size > byte_limit:
            return False
        delivered = _submit(message)
        if delivered is None:
            return False
        self.last_delivery = delivered
        self.lines += 1
        self.bytes += size
        return True

    def _summary(self) -> None:
        if self.omitted_lines and self._offer(
            f"omitted {self.omitted_lines} lines "
            f"({self.omitted_bytes} bytes)",
            summary=True,
        ):
            self.omitted_lines = self.omitted_bytes = 0

    def _line(self) -> None:
        self._refresh()
        # Keep only a bounded raw prefix; redact before final output truncation.
        text = _clean(self.buffer.decode("utf-8", "replace"))
        data = text.encode("utf-8")
        limit = _LINE_BYTES - len(self.prefix.encode("utf-8"))
        if self.line_bytes > len(self.buffer) or len(data) > limit:
            text = data[: limit - len(_TRUNCATED)].decode("utf-8", "ignore")
            text += _TRUNCATED
        if (
            self.lines < _LINES_PER_SECOND - 1
            and self.bytes + len((self.prefix + text).encode("utf-8"))
            <= _BYTES_PER_SECOND - 256
        ):
            self._summary()
        if not self._offer(text):
            self.omitted_lines += 1
            self.omitted_bytes += self.line_bytes
        self.buffer.clear()
        self.line_bytes = 0

    async def finish(self) -> None:
        if self.line_bytes:
            self._line()
        self._refresh()
        self._summary()
        # Normal EOF hands accepted lines to fast handlers. Slow host logging
        # gets no ownership over worker shutdown and remains shared on restart.
        deadline = asyncio.get_running_loop().time() + 0.05
        while (
            self.last_delivery is not None and not self.last_delivery.is_set()
        ):
            if asyncio.get_running_loop().time() >= deadline:
                break
            await asyncio.sleep(0.001)
