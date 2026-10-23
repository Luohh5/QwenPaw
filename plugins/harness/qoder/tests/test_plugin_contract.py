# -*- coding: utf-8 -*-
"""Qoder loads only by explicit SDK installation in its own environment."""

import asyncio
import importlib.util
import sys
from pathlib import Path

from qwenpaw.plugins.compliance import check_plugin
from qwenpaw.plugins.testing import PluginTestHost

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
