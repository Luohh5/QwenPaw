"""One observed original probe attempt; no deadlines/returns are changed."""
import json
from time import monotonic

import pytest

from g3fd_dd_native_suite import (
    test_native_fetch_poll_cookie_form_and_credentials as original_case,
)
from g3fd_dd_native_support import g3_host_import_boundary, native_qr

__all__ = ["g3_host_import_boundary", "native_qr"]


@pytest.mark.asyncio
async def test_observe_original_feishu_probe(native_qr, monkeypatch, tmp_path):
    from qwenpaw.plugins.execution.process import PluginProcess
    from qwenpaw.app.channels.process_supervisor import ChannelProcess

    native_qr.probe_trace = True
    started = monotonic()
    trace = []

    def observe(cls, name):
        original = getattr(cls, name)

        async def wrapped(self, *args, **kwargs):
            row = {
                "method": name,
                "start_seconds": monotonic() - started,
                "mode": self._mode,
            }
            if name == "_control":
                row["notification"] = args[1].method
            trace.append(row)
            try:
                result = await original(self, *args, **kwargs)
                row["result"] = "returned"
                return result
            except BaseException as error:
                row["exception"] = type(error).__name__
                row["reason"] = getattr(error, "reason_code", None)
                raise
            finally:
                row["end_seconds"] = monotonic() - started
                row["health"] = self.health_snapshot()

        monkeypatch.setattr(cls, name, wrapped)

    for name in ("_open", "_evidence", "_control", "_cleanup"):
        observe(PluginProcess, name)
    observe(ChannelProcess, "_hello")
    try:
        await original_case(native_qr, "feishu", "feishu")
    finally:
        path = tmp_path / "observed-probe-trace.json"
        path.write_text(json.dumps(trace, indent=2) + "\n")
        print("OBSERVED_UNMODIFIED_PROBE_TRACE", json.dumps(trace))
        stack = tmp_path / "probe-stack.log"
        if stack.exists():
            print("CHILD_STACK", stack.read_text())
