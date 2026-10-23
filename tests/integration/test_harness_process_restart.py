# -*- coding: utf-8 -*-
"""A saved native session and enabled flag survive two real host processes."""

import asyncio
import importlib
import importlib.util
import os
from pathlib import Path
import subprocess
import sys

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
VENDOR = os.environ.get("QWENPAW_HARNESS_VENDOR")


async def _stage(root: Path, vendor: str, first: bool) -> None:
    from qwenpaw.config.utils import load_config
    from qwenpaw.plugins.harnesses import HarnessRunRequest
    from qwenpaw.plugins.testing import PluginTestHost

    plugin_id = f"qwenpaw-harness-{vendor}"
    async with PluginTestHost(root) as host:
        if first:
            assert (
                await host.install(REPO_ROOT / "plugins/harness" / vendor)
            ).ok
        else:
            # Exercise the same installed-directory/config discovery as boot.
            # pylint: disable-next=protected-access
            await host._loader.load_all_plugins(
                configs=load_config().plugins,
                activate=True,
            )
            assert all(
                row["status"] != "available" for row in host.list_harnesses()
            )
            assert load_config().plugins[plugin_id]["enabled"] is False
            assert (await host.set_enabled(plugin_id, True)).ok

        adapter = importlib.import_module(
            f"plugin_{plugin_id.replace('-', '_')}.adapter",
        )
        if vendor == "codex":
            from plugins.harness.codex.tests.conftest import FakeCodexClient

            client = FakeCodexClient()
            adapter.CodexAppServerClient = lambda **_kwargs: client
        else:
            from plugins.harness.qoder.discovery import QoderBinaryResolution
            from plugins.harness.qoder.tests.conftest import FakeQoderClient

            clients = []

            def make_client(options):
                client = FakeQoderClient(options)
                clients.append(client)
                return client

            adapter.QoderSDKClient = make_client
            adapter.resolve_qoder_binary_info = lambda _binary: (
                QoderBinaryResolution(Path("/fake/qodercli"), "configured")
            )

        output = [
            item
            async for item in host.run_harness(
                vendor,
                HarnessRunRequest("first" if first else "resume"),
                workspace_id="test",
                session_id="same-session",
                user_id="alice",
            )
        ]
        assert output[-1].status == "completed"
        if vendor == "codex":
            if first:
                native_id = next(
                    params["threadId"]
                    for method, params in client.requests
                    if method == "turn/start"
                )
                (root / "native-id").write_text(native_id)
            else:
                native_id = (root / "native-id").read_text()
                assert ("thread/resume", {"threadId": native_id}) in (
                    client.requests
                )
        elif first:
            (root / "native-id").write_text(clients[-1].options.session_id)
        else:
            assert (
                clients[-1].options.resume == (root / "native-id").read_text()
            )

        if first:
            assert (await host.set_enabled(plugin_id, False)).ok
            assert load_config().plugins[plugin_id]["enabled"] is False
        else:
            assert load_config().plugins[plugin_id]["enabled"] is True


@pytest.mark.skipif(
    VENDOR not in {"codex", "qoder"},
    reason="Run explicitly in each vendor-only SDK interpreter",
)
def test_host_restart_retains_disabled_state_and_resumes_native_id(tmp_path):
    selected = "codex_cli_bin" if VENDOR == "codex" else "qoder_agent_sdk"
    other = "qoder_agent_sdk" if VENDOR == "codex" else "codex_cli_bin"
    assert importlib.util.find_spec(selected) is not None
    assert importlib.util.find_spec(other) is None
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [
            str(REPO_ROOT),
            str(REPO_ROOT / "src"),
            str(REPO_ROOT / "packages/qwenpawmail-mcp/src"),
        ],
    )
    env["QWENPAW_WORKING_DIR"] = str(tmp_path)
    env["QWENPAW_RUNTIME_ID"] = "protocol-restart"
    env["PIP_NO_INDEX"] = "1"
    env["UV_OFFLINE"] = "1"
    for phase in ("first", "restart"):
        subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                phase,
                str(tmp_path),
                VENDOR,
            ],
            cwd=REPO_ROOT,
            env=env,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )


if __name__ == "__main__":
    asyncio.run(_stage(Path(sys.argv[2]), sys.argv[3], sys.argv[1] == "first"))
