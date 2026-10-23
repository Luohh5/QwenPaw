# -*- coding: utf-8 -*-
"""Qoder loads only by explicit SDK installation in its own environment."""

import asyncio
import importlib
import importlib.util
import sys
from pathlib import Path

from qwenpaw.plugins.compliance import check_plugin
from qwenpaw.plugins.harnesses import HarnessRunRequest
from qwenpaw.plugins.testing import PluginTestHost

from plugins.harness.qoder.discovery import QoderBinaryResolution

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


async def test_qoder_registers_without_codex_sdk_or_vendor_probe(
    tmp_path,
    monkeypatch,
):
    assert importlib.util.find_spec("qoder_agent_sdk") is not None
    assert importlib.util.find_spec("codex_cli_bin") is None
    imported_before = set(sys.modules)

    async def no_process(*_args, **_kwargs):
        raise AssertionError("registration launched a vendor process")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", no_process)
    async with PluginTestHost(tmp_path / "host") as host:
        assert host.list_harnesses() == ()
        assert check_plugin(PLUGIN_ROOT).ok
        result = await host.install(PLUGIN_ROOT)
        assert result.ok, result
        assert [
            (row["id"], row["plugin_id"], row["status"])
            for row in host.list_harnesses()
        ] == [("qoder", "qwenpaw-harness-qoder", "available")]
        capabilities = host.list_harnesses()[0]["capabilities"]
        assert capabilities["attachments"] is True
        assert [command["name"] for command in capabilities["commands"]] == [
            "compact",
        ]
        assert "codex_cli_bin" not in set(sys.modules) - imported_before
        assert not any(
            "qwenpaw-harness-qoder"
            in str(getattr(sys.modules[name], "__file__", ""))
            and str(getattr(sys.modules[name], "__file__", "")).endswith(
                "adapter.py",
            )
            for name in set(sys.modules) - imported_before
        )


async def test_reenable_resumes_original_qoder_session(
    tmp_path,
    monkeypatch,
    client_factory,
):
    def fake_runtime():
        adapter = importlib.import_module(
            "plugin_qwenpaw_harness_qoder.adapter",
        )
        monkeypatch.setattr(adapter, "QoderSDKClient", client_factory)
        monkeypatch.setattr(
            adapter,
            "resolve_qoder_binary_info",
            lambda _binary: QoderBinaryResolution(
                Path("/fake/qodercli"),
                "configured",
            ),
        )

    async with PluginTestHost(tmp_path / "host") as host:
        assert (await host.install(PLUGIN_ROOT)).ok
        fake_runtime()
        _ = [
            item
            async for item in host.run_harness(
                "qoder",
                HarnessRunRequest("first"),
                workspace_id="test",
                session_id="original",
                user_id="alice",
            )
        ]
        native_id = client_factory.created[-1].options.session_id
        assert native_id

        assert (await host.set_enabled("qwenpaw-harness-qoder", False)).ok
        assert (await host.set_enabled("qwenpaw-harness-qoder", True)).ok
        fake_runtime()
        _ = [
            item
            async for item in host.run_harness(
                "qoder",
                HarnessRunRequest("resume"),
                workspace_id="test",
                session_id="original",
                user_id="alice",
            )
        ]
        assert client_factory.created[-1].options.resume == native_id


async def test_force_unload_interrupts_live_qoder_turn(
    tmp_path,
    monkeypatch,
):
    from plugins.harness.qoder.tests.conftest import FakeQoderClient

    started = asyncio.Event()
    created = []

    async def pending_response():
        started.set()
        await asyncio.Future()
        yield None

    def make_client(options):
        client = FakeQoderClient(options)
        client.receive_response = pending_response
        created.append(client)
        return client

    async with PluginTestHost(tmp_path / "host") as host:
        assert (await host.install(PLUGIN_ROOT)).ok
        adapter = importlib.import_module(
            "plugin_qwenpaw_harness_qoder.adapter",
        )
        monkeypatch.setattr(adapter, "QoderSDKClient", make_client)
        monkeypatch.setattr(
            adapter,
            "resolve_qoder_binary_info",
            lambda _binary: QoderBinaryResolution(
                Path("/fake/qodercli"),
                "configured",
            ),
        )

        async def consume():
            return [
                item
                async for item in host.run_harness(
                    "qoder",
                    HarnessRunRequest("keep working"),
                    workspace_id="test",
                    session_id="busy",
                )
            ]

        turn = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(started.wait(), 3)
            report = await asyncio.wait_for(
                host.unload("qwenpaw-harness-qoder", interrupt_tasks=True),
                3,
            )
            assert report.clean and report.quiescent
            assert turn.done()
            assert created[0].interrupted and created[0].disconnected
            assert all(
                row["status"] != "available" for row in host.list_harnesses()
            )
        finally:
            turn.cancel()
            await asyncio.gather(turn, return_exceptions=True)
