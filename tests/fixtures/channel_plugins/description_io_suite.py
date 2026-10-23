# -*- coding: utf-8 -*-
"""Source description I/O stays off the loop across real transactions."""

import asyncio
from collections import Counter
import inspect
import json
from pathlib import Path
import threading

import pytest

pytest.register_assert_rewrite("c5_support", "c5_transactions_suite")
from c5_support import KEY, eventually  # noqa: E402
from c5_transactions_suite import (  # noqa: E402
    test_active_update_swaps_all_workspaces_after_old_exit as active_update,
    test_first_install_runs_only_installed_source as first_install,
    test_partial_new_activation_obeys_quiescence_before_restore as restore,
)

pytest_plugins = ["c5_support"]


class SourceIO:
    """Observe actual filesystem calls underneath static descriptions."""

    def __init__(self, monkeypatch, transaction, *, extra_roots=()):
        self.transaction = transaction
        self.observations = []
        self.native_text = Path.read_text
        self.barrier = None
        self.entered = threading.Event()
        self.released = threading.Event()
        self.blocked = False
        self.loop_thread = threading.get_ident()
        roots = (
            tuple(extra_roots)
            + (transaction.installed.parent,)
            + tuple(
                transaction.installed.parent.parent
                / ("incoming-" + version)
                / KEY
                for version in ("1.0.0", "2.0.0")
            )
        )
        for method in ("rglob", "read_bytes", "read_text"):
            native = getattr(Path, method)

            def observe(path, *args, _native=native, _method=method, **kwargs):
                if self.description_stack() and any(
                    path.is_relative_to(root) for root in roots
                ):
                    try:
                        asyncio.get_running_loop()
                        running_loop = True
                    except RuntimeError:
                        running_loop = False
                    evidence = (
                        self.native_text(transaction.network)
                        if transaction.network.exists()
                        else ""
                    )
                    phase = (
                        "restore"
                        if '"kind": "failed-start"' in evidence
                        else "update"
                        if '"kind": "connected"' in evidence
                        else "load"
                    )
                    self.observations.append(
                        (phase, _method, threading.get_ident(), running_loop),
                    )
                    if (
                        self.barrier
                        and _method == "read_bytes"
                        and not self.blocked
                    ):
                        self.blocked = True
                        self.entered.set()
                        # Fail fast on the old implementation, never block its loop.
                        assert (
                            not running_loop
                        ), "source read blocked event loop"
                        assert self.barrier.wait(
                            10
                        ), "source read was not released"
                        self.released.set()
                return _native(path, *args, **kwargs)

            monkeypatch.setattr(Path, method, observe)

    @staticmethod
    def description_stack():
        frame = inspect.currentframe()
        try:
            while frame is not None:
                if frame.f_code.co_name in {
                    "describe",
                    "describe_channel",
                } and frame.f_code.co_filename.endswith("channel_adapter.py"):
                    return True
                frame = frame.f_back
            return False
        finally:
            del frame

    def assert_off_loop(self, phases):
        counts = Counter(
            (phase, method) for phase, method, _, _ in self.observations
        )
        threads = Counter(
            "event_loop"
            if thread == self.loop_thread or running_loop
            else "worker"
            for _, _, thread, running_loop in self.observations
        )
        print(
            "source-description-io "
            + json.dumps(
                {
                    "operations": {
                        f"{phase}:{method}": count
                        for (phase, method), count in counts.items()
                    },
                    "threads": threads,
                },
                sort_keys=True,
            )
        )
        for phase in phases:
            assert counts[phase, "rglob"], (phase, self.observations)
            assert counts[phase, "read_bytes"], (phase, self.observations)
            assert counts[phase, "read_text"], (phase, self.observations)
        assert self.observations
        assert all(
            thread != self.loop_thread and not running_loop
            for _, _, thread, running_loop in self.observations
        ), self.observations


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "flow", ["direct_load", "install", "update", "restore"]
)
async def test_description_io_off_loop(transaction, monkeypatch, flow):
    observer = SourceIO(monkeypatch, transaction)
    if flow == "direct_load":
        workspace = await transaction.workspace("direct")
        record = await transaction.seed()
        assert record.status == "active"
        _, identity = await transaction.identity(workspace)
        assert identity["version"] == "1.0.0"
    elif flow == "install":
        await first_install(transaction)
    elif flow == "update":
        await active_update(transaction)
    else:
        await restore(transaction, monkeypatch, False)
    observer.assert_off_loop(
        {"load", "update", "restore"}
        if flow == "restore"
        else {"load", "update"}
        if flow == "update"
        else {"load"},
    )


@pytest.mark.asyncio
async def test_cancelled_description_retains_transaction_until_read_finishes(
    transaction,
    monkeypatch,
):
    observer = SourceIO(monkeypatch, transaction)
    observer.barrier = threading.Event()
    source = transaction.artifact()
    loading = asyncio.create_task(
        transaction.loader.load_plugin_from_path(source)
    )
    contender = None
    entered_lock = asyncio.Event()
    try:
        await eventually(lambda: observer.entered.is_set() or loading.done())
        if loading.done():
            await loading
        assert observer.entered.is_set()
        heartbeat = asyncio.Event()
        asyncio.get_running_loop().call_soon(heartbeat.set)
        await asyncio.wait_for(heartbeat.wait(), 1)
        loading.cancel()
        await asyncio.sleep(0)

        async def acquire_transaction():
            async with transaction.loader.plugin_lifecycle(KEY):
                entered_lock.set()

        contender = asyncio.create_task(acquire_transaction())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not observer.released.is_set()
        assert not loading.done(), "cancellation abandoned a source read"
        assert (
            not entered_lock.is_set()
        ), "transaction released during source read"
    finally:
        observer.barrier.set()
        try:
            await loading
        except asyncio.CancelledError:
            pass
        if contender is not None:
            await asyncio.wait_for(contender, 2)
    assert observer.released.is_set()
    assert entered_lock.is_set()
    observer.assert_off_loop({"load"})
