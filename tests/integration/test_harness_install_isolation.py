# -*- coding: utf-8 -*-
"""Install a real Harness ZIP in each vendor-specific test interpreter."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
VENDOR = os.environ.get("QWENPAW_HARNESS_VENDOR")
pytestmark = pytest.mark.skipif(
    VENDOR is None,
    reason="Run explicitly in each vendor-only SDK test interpreter",
)


@pytest.mark.asyncio
async def test_explicit_harness_zip_install_keeps_other_vendor_absent(
    tmp_path: Path,
) -> None:
    """Use the public transaction, not a direct source import."""
    from qwenpaw.plugins.testing import PluginTestHost

    assert VENDOR in {"codex", "qoder"}
    selected_module = (
        "codex_cli_bin" if VENDOR == "codex" else "qoder_agent_sdk"
    )
    other_module = "qoder_agent_sdk" if VENDOR == "codex" else "codex_cli_bin"
    assert importlib.util.find_spec(selected_module) is not None
    assert importlib.util.find_spec(other_module) is None

    plugin_id = f"qwenpaw-harness-{VENDOR}"
    dist = tmp_path / "dist"
    subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts/pack/generate_plugin_metadata.py"),
            "--plugins-root",
            str(REPO_ROOT / "plugins"),
            "--dist",
            str(dist),
            "--only",
            plugin_id,
        ],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    archive = dist / "harness" / plugin_id / f"{plugin_id}-1.0.0.zip"
    unpacked = tmp_path / "unpacked"
    with zipfile.ZipFile(archive) as source:
        source.extractall(unpacked)

    async with PluginTestHost(tmp_path / "host") as host:
        installed = await host.install(unpacked / VENDOR)
        assert installed.ok and installed.status == "active"
        assert [
            (row["id"], row["status"]) for row in host.list_harnesses()
        ] == [(VENDOR, "available")]
        assert not (
            tmp_path
            / "host/plugins"
            / f"qwenpaw-harness-{'qoder' if VENDOR == 'codex' else 'codex'}"
        ).exists()
        assert importlib.util.find_spec(other_module) is None
        assert other_module not in sys.modules
