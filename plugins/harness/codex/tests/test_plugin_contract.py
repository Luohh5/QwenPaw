# -*- coding: utf-8 -*-
"""Codex is a lazy, optional Harness plugin owned by the SDK loader."""

import asyncio
import importlib
import shutil
import sys
from pathlib import Path

import pytest

from qwenpaw.plugins import PluginBusyError, StaleContextError
from qwenpaw.plugins.compliance import check_plugin
from qwenpaw.plugins.harnesses import HarnessRunRequest
from qwenpaw.plugins.testing import PluginTestHost

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


async def test_explicit_install_registers_only_codex(tmp_path, monkeypatch):
    binary = tmp_path / "codex"
    binary.write_text("fake executable", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setenv("CODEX_BINARY", str(binary))
    imported_before = set(sys.modules)
    original_import = importlib.import_module
    original_which = shutil.which
    original_is_file = Path.is_file

    def no_vendor_import(name, *args, **kwargs):
        if name == "codex_cli_bin":
            raise AssertionError("registration imported the Codex SDK")
        return original_import(name, *args, **kwargs)

    def no_binary_lookup(command, *args, **kwargs):
        if command == "codex":
            raise AssertionError("registration searched for the Codex CLI")
        return original_which(command, *args, **kwargs)

    def no_binary_stat(path):
        if path == binary:
            raise AssertionError("registration inspected the Codex CLI")
        return original_is_file(path)

    async def no_process(*_args, **_kwargs):
        raise AssertionError("registration started a process")

    monkeypatch.setattr(importlib, "import_module", no_vendor_import)
    monkeypatch.setattr(shutil, "which", no_binary_lookup)
    monkeypatch.setattr(Path, "is_file", no_binary_stat)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", no_process)

    async with PluginTestHost(tmp_path / "host") as host:
        assert host.list_harnesses() == ()
        assert check_plugin(PLUGIN_ROOT).ok
        result = await host.install(PLUGIN_ROOT)
        assert result.ok, result
        assert [
            (row["id"], row["plugin_id"], row["status"])
            for row in host.list_harnesses()
        ] == [("codex", "qwenpaw-harness-codex", "available")]
        assert host.list_harnesses()[0]["capabilities"]["external_history"]
        assert host.list_harnesses()[0]["capabilities"]["attachments"]
        newly_imported = set(sys.modules) - imported_before
        assert "codex_cli_bin" not in newly_imported
        assert not any(
            "qwenpaw-harness-codex"
            in str(getattr(sys.modules[name], "__file__", ""))
            and str(getattr(sys.modules[name], "__file__", "")).endswith(
                "adapter.py",
            )
            for name in newly_imported
        )

        # The test host has no public archive helper; use its real workspace.
        # pylint: disable-next=protected-access
        workspace = await host._workspace(
            "test",
        )
        async with workspace.backend_runtime.archive("codex") as archive:
            assert (
                await archive.list_external_threads(
                    limit=0,
                    archived=False,
                )
                == ()
            )
            with pytest.raises(PluginBusyError):
                await host.unload("qwenpaw-harness-codex")
        with pytest.raises(StaleContextError):
            await archive.list_external_threads(limit=0, archived=False)
        result = await host.unload("qwenpaw-harness-codex")
        assert result.clean and result.quiescent


async def test_reenable_resumes_original_codex_thread(
    tmp_path,
    monkeypatch,
    codex_client,
):
    async with PluginTestHost(tmp_path / "host") as host:
        assert (await host.install(PLUGIN_ROOT)).ok
        first = codex_client
        adapter = importlib.import_module(
            "plugin_qwenpaw_harness_codex.adapter",
        )
        monkeypatch.setattr(
            adapter,
            "CodexAppServerClient",
            lambda **_kwargs: first,
        )
        _ = [
            item
            async for item in host.run_harness(
                "codex",
                HarnessRunRequest("first"),
                workspace_id="test",
                session_id="original",
                user_id="alice",
            )
        ]
        assert [
            params["threadId"]
            for method, params in first.requests
            if method == "turn/start"
        ] == ["new-thread-1"]

        assert (await host.set_enabled("qwenpaw-harness-codex", False)).ok
        assert (await host.set_enabled("qwenpaw-harness-codex", True)).ok
        second = type(codex_client)()
        adapter = importlib.import_module(
            "plugin_qwenpaw_harness_codex.adapter",
        )
        monkeypatch.setattr(
            adapter,
            "CodexAppServerClient",
            lambda **_kwargs: second,
        )
        _ = [
            item
            async for item in host.run_harness(
                "codex",
                HarnessRunRequest("resume"),
                workspace_id="test",
                session_id="original",
                user_id="alice",
            )
        ]
        assert ("thread/resume", {"threadId": "new-thread-1"}) in (
            second.requests
        )
        assert [
            params["threadId"]
            for method, params in second.requests
            if method == "turn/start"
        ] == ["new-thread-1"]


async def test_failed_codex_turn_reaches_public_failed_stream(
    tmp_path,
    monkeypatch,
    codex_client,
):
    async with PluginTestHost(tmp_path / "host") as host:
        assert (await host.install(PLUGIN_ROOT)).ok
        adapter = importlib.import_module(
            "plugin_qwenpaw_harness_codex.adapter",
        )
        monkeypatch.setattr(
            adapter,
            "CodexAppServerClient",
            lambda **_kwargs: codex_client,
        )
        original = codex_client.request

        async def failed_turn(method, params):
            if method == "turn/start":
                await codex_client.queue.put(
                    {
                        "method": "turn/completed",
                        "params": {
                            "threadId": params["threadId"],
                            "turn": {
                                "id": "turn-1",
                                "status": "failed",
                                "error": {"message": "Vendor turn failed"},
                            },
                        },
                    },
                )
                return {"turn": {"id": "turn-1"}}
            return await original(method, params)

        codex_client.request = failed_turn
        output = [
            item
            async for item in host.run_harness(
                "codex",
                HarnessRunRequest("fail"),
                workspace_id="test",
                session_id="failed-turn",
            )
        ]
        assert output[-1].status == "failed"
        assert "Vendor turn failed" in str(output[-1])


async def test_force_unload_interrupts_live_codex_turn(
    tmp_path,
    monkeypatch,
    codex_client,
):
    started = asyncio.Event()
    original = codex_client.request

    async def pending_turn(method, params):
        if method == "turn/start":
            started.set()
            return {"turn": {"id": "turn-1"}}
        return await original(method, params)

    codex_client.request = pending_turn
    async with PluginTestHost(tmp_path / "host") as host:
        assert (await host.install(PLUGIN_ROOT)).ok
        adapter = importlib.import_module(
            "plugin_qwenpaw_harness_codex.adapter",
        )
        monkeypatch.setattr(
            adapter,
            "CodexAppServerClient",
            lambda **_kwargs: codex_client,
        )

        async def consume():
            return [
                item
                async for item in host.run_harness(
                    "codex",
                    HarnessRunRequest("keep working"),
                    workspace_id="test",
                    session_id="busy",
                )
            ]

        turn = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(started.wait(), 3)
            report = await asyncio.wait_for(
                host.unload("qwenpaw-harness-codex", interrupt_tasks=True),
                3,
            )
            assert report.clean and report.quiescent
            assert turn.done()
            assert codex_client.stopped
            assert all(
                row["status"] != "available" for row in host.list_harnesses()
            )
        finally:
            turn.cancel()
            await asyncio.gather(turn, return_exceptions=True)
