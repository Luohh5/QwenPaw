# -*- coding: utf-8 -*-
# pylint: disable=protected-access
"""Absolute-path worker; only trusted stdlib mechanics run before stdin."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys
import types
from typing import Any


def _sibling(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(
        "_qwenpaw_execution_" + name,
        Path(__file__).with_name(name + ".py"),
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("BOOTSTRAP_UNAVAILABLE")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _configure(
    boot: Any,
    packet: dict[str, Any],
    protected_namespaces: tuple[str, ...],
) -> None:
    root = boot._absolute_existing_directory(packet["code_root"], "code_root")
    support = Path(__file__).resolve().parents[3]
    if support != Path(packet["support_root"]).resolve(strict=True):
        raise RuntimeError("BOOTSTRAP_UNAVAILABLE")
    # Keep trusted execution support ahead of any plugin-provided namespace.
    for namespace in (
        "qwenpaw/plugins/execution",
        "qwenpaw/plugins/execution.py",
        *protected_namespaces,
    ):
        if (root / namespace).exists() or (root / namespace).is_symlink():
            raise RuntimeError("BOOTSTRAP_UNAVAILABLE")
    sys.dont_write_bytecode = True
    sys.path[:] = [str(root), str(support)] + [
        item
        for item in sys.path
        if item and Path(item).resolve() not in {root, support}
    ]
    package = types.ModuleType("qwenpaw")
    package.__package__ = "qwenpaw"
    package.__path__ = [str(root / "qwenpaw"), str(support / "qwenpaw")]
    sys.modules["qwenpaw"] = package
    plugins = types.ModuleType("qwenpaw.plugins")
    plugins.__package__ = "qwenpaw.plugins"
    plugins.__path__ = [str(support / "qwenpaw/plugins")]
    sys.modules["qwenpaw.plugins"] = plugins
    for value in reversed(packet["dependency_paths"]):
        path = boot._absolute_existing_directory(value, "dependency_path")
        # Explicit directories do not execute .pth files.
        sys.path.insert(2, str(path))
    boot._clean_environment(())


async def run(
    boot: Any,
    packet: dict[str, Any],
    output: Any,
    runtime: Any,
) -> None:
    from .errors import ProtocolValidationError
    from .framing import FramedTransport
    from .rpc import RpcResponsePublication

    transport = await boot._open_protocol_transport(
        FramedTransport,
        output,
        input_handle=sys.stdin.buffer,
    )
    peer = runtime.peer(transport)
    identity = runtime.identity(packet)
    context = runtime.context(peer, identity)
    component = None
    start_task = None
    stopped = asyncio.Event()

    async def stop(params: Any, _request: Any) -> Any:
        if type(identity).from_mapping(params) != identity:
            raise ProtocolValidationError("identity mismatch")
        if start_task is not None:
            start_task.cancel()
        if component is not None:
            await component.stop()
        return RpcResponsePublication(
            result={"stopped": True},
            on_prepare=lambda: {"stopped": True},
            on_published=stopped.set,
            on_write_failed=stopped.set,
            on_write_deferred=stopped.set,
            on_aborted=lambda _reason: stopped.set(),
        )

    async def connect() -> None:
        try:
            await component.start(packet["config"])
        except Exception:
            await runtime.state(context, runtime.failed_state)

    peer.register_method(runtime.control_prefix + "stop", stop)
    await peer.start()
    try:
        entrypoint, source, hello = runtime.verify(boot, packet, identity)
        accepted = await peer.call(
            runtime.hello_method,
            hello,
            timeout=packet["startup_timeout"],
        )
        if accepted != {"accepted": True}:
            raise boot.BootstrapError("HELLO_REJECTED", "hello rejected")
        component_class = boot._load_driver_class(entrypoint, source)
        component = component_class()
        if any(
            not callable(getattr(component, name, None))
            for name in ("bind", "start", "stop")
        ):
            raise boot.BootstrapError(
                "ENTRYPOINT_INVALID",
                "invalid component",
            )
        component.bind(context)
        await runtime.state(context, runtime.initial_state)
        await peer.notify(
            runtime.control_prefix + "ready",
            identity.to_mapping(),
        )
        start_task = asyncio.create_task(connect())
        closed_task = asyncio.create_task(peer.wait_closed())
        stop_task = asyncio.create_task(stopped.wait())
        await asyncio.wait(
            (closed_task, stop_task),
            return_when=asyncio.FIRST_COMPLETED,
        )
        await asyncio.sleep(0)
        for task in (closed_task, stop_task):
            task.cancel()
        await asyncio.gather(closed_task, stop_task, return_exceptions=True)
    except Exception as exc:
        await _report_error(peer, identity, runtime, exc)
    finally:
        if start_task is not None:
            start_task.cancel()
        await peer.aclose()


async def _report_error(
    peer: Any,
    identity: Any,
    runtime: Any,
    exc: Exception,
) -> None:
    from .process import PluginProcess

    reason = getattr(exc, "reason_code", "WORKER_START_FAILED")
    if reason not in PluginProcess.startup_errors:
        reason = "WORKER_START_FAILED"
    try:
        await peer.notify(
            runtime.control_prefix + "error",
            {**identity.to_mapping(), "reason_code": reason},
        )
    except Exception:
        pass


def main(
    runtime_factory: Any = None,
    *,
    protected_namespaces: tuple[str, ...] = (),
) -> int:
    output = None
    try:
        _sibling("process_tree").watch_parent(int(sys.argv[1]))
        boot = _sibling("bootstrap")
        boot._require_isolated_python()
        output = boot._capture_protocol_output()
        boot._redirect_ordinary_output()
        raw = sys.stdin.buffer.readline(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024 or not raw.endswith(b"\n"):
            return 70
        packet = json.loads(raw)
        _configure(boot, packet, protected_namespaces)
        from qwenpaw.plugins.execution.runtime import Runtime

        run_component = importlib.import_module(
            "qwenpaw.plugins.execution.worker",
        ).run

        runtime = Runtime() if runtime_factory is None else runtime_factory()
        asyncio.run(run_component(boot, packet, output, runtime))
        return 0
    except BaseException:
        return 70
    finally:
        if output is not None:
            output.close()


if __name__ == "__main__":
    os._exit(main())
