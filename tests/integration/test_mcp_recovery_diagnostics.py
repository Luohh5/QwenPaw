# -*- coding: utf-8 -*-
"""Real upgrade coordinator outcomes remain visible through passive status."""

# The imported autouse fixture forbids network and installer activity.
# pylint: disable=protected-access,unused-import
import json
from types import SimpleNamespace

import pytest

from qwenpaw.app.mcp.plugin_service import plugin_state
from qwenpaw.app.mcp_plugin_upgrade import MARKER, prepare_startup_plugins
from qwenpaw.plugins.testing import PluginTestHost
from tests.integration.test_mcp_plugin_upgrade import (
    SECRET,
    seed,
    inventory,
    bytes_under,
    prohibit_network_and_installers,  # noqa: F401
)
from tests.unit.plugins.mcp.fixtures import card


async def prepare(host, root):
    outcome = await prepare_startup_plugins(
        host._loader,
        host._workspace_manager,
        app=host._http_app,
        snapshot=inventory(root),
    )
    await host._loader.activate_all_loaded()
    return outcome


async def status(host, workspace="test"):
    request = SimpleNamespace(
        app=host._http_app,
        state=SimpleNamespace(agent_id=workspace),
        headers={"X-Agent-Id": workspace},
    )
    return await plugin_state(request)


async def passive_status(host, root, workspace="test"):
    before = bytes_under(root)
    result = await status(host, workspace)
    assert bytes_under(root) == before
    assert not host._workspaces
    assert SECRET not in json.dumps(result)
    assert "snapshot" not in result and "config" not in result
    return result


async def test_invalid_marker_reaches_status_and_real_retry_clears_error(
    tmp_path,
):
    root = tmp_path / "host"
    seed(
        root,
        [("test", root / "workspaces/test", {}, [card(enabled=False)], True)],
    )
    marker = root / "plugins" / MARKER
    marker.parent.mkdir(parents=True)
    marker.write_text('{"private":"' + SECRET)
    damaged = marker.read_bytes()
    async with PluginTestHost(root) as host:
        result = await prepare(host, root)
        assert result.status == "needs_review"
        response = await passive_status(host, root)
        assert "invalid_upgrade_record" in response["diagnostics"]
        assert response["upgrade"]["status"] == "needs_review"
        assert marker.read_bytes() == damaged
        # Retry the production coordinator against repaired on-disk evidence.
        marker.unlink()
        assert (await prepare(host, root)).status == "installed"
        response = await passive_status(host, root)
        assert response["status"] == "active"
        assert "invalid_upgrade_record" not in response["diagnostics"]
        assert response["upgrade"]["status"] == "installed"


@pytest.mark.parametrize("failure", ["dependencies", "copy"])
async def test_preload_offline_failure_is_sanitized_visible_and_retryable(
    tmp_path,
    monkeypatch,
    failure,
):
    from qwenpaw.plugins.loader import PluginLoader
    from qwenpaw.plugins.dependency_gate import GateDecision

    root = tmp_path / "host"
    workspace = root / "workspaces/test"
    seed(root, [("test", workspace, {}, [card(enabled=False)], True)])
    preserved = bytes_under(workspace)
    async with PluginTestHost(root) as host:
        with monkeypatch.context() as patch:
            if failure == "dependencies":

                async def unavailable(
                    _self,
                    _path,
                    _id,
                    *,
                    allow_install=False,
                ):
                    assert allow_install is False
                    return GateDecision(
                        allow_install=False,
                        already_satisfied=False,
                        missing=[SECRET],
                        reason="credential error " + SECRET,
                    )

                patch.setattr(
                    PluginLoader,
                    "_ensure_dependencies_installed",
                    unavailable,
                )
            else:

                async def fail_copy(*_args, **_kwargs):
                    raise OSError("credential error " + SECRET)

                patch.setattr(host._loader, "install_plugin_files", fail_copy)
            result = await prepare(host, root)
        assert result.status == "failed"
        assert host._loader.get_loaded_plugin("qwenpaw-mcp") is None
        response = await passive_status(host, root)
        assert (
            "offline_upgrade_failed_check_dependencies_and_storage"
            in response["diagnostics"]
        )
        assert response["upgrade"]["status"] == "failed"
        assert bytes_under(workspace) == preserved
        from qwenpaw.app.routers.mcp_plugin import install_mcp_plugin

        retried = await install_mcp_plugin(
            SimpleNamespace(
                app=host._http_app,
                state=SimpleNamespace(agent_id="test"),
                headers={"X-Agent-Id": "test"},
            ),
        )
        assert retried["status"] == "active"
        response = await passive_status(host, root)
        assert response["status"] == "active"
        assert not response["diagnostics"]


async def test_blocked_workspace_is_visible_beside_healthy_global_plugin(
    tmp_path,
):
    root = tmp_path / "host"
    blocked = root / "workspaces/blocked"
    seed(
        root,
        [
            (
                "safe",
                root / "workspaces/safe",
                {},
                [card(enabled=False)],
                True,
            ),
            (
                "blocked",
                blocked,
                {"mcp": {"unknown": SECRET}},
                [card(enabled=False)],
                True,
            ),
        ],
    )
    preserved = bytes_under(blocked)
    async with PluginTestHost(root) as host:
        outcome = await prepare(host, root)
        assert outcome.status == "installed"
        response = await passive_status(host, root, "blocked")
        assert response["status"] == "active"
        assert response["workspace"]["id"] == "blocked"
        assert response["workspace"]["status"] == "needs_review"
        assert response["workspace"]["eligible"] is False
        assert "workspace_needs_review" in response["workspace"]["diagnostics"]
        healthy = await passive_status(host, root, "safe")
        assert healthy["workspace"]["eligible"] is True
        assert not healthy["workspace"]["diagnostics"]
        assert bytes_under(blocked) == preserved


@pytest.mark.parametrize("off", [False, True])
async def test_ordinary_unused_and_explicit_off_are_not_blocked_recovery(
    tmp_path,
    off,
):
    root = tmp_path / "host"
    seed(
        root,
        [("test", root / "workspaces/test", {}, [], True)],
        plugins={"qwenpaw-mcp": {"enabled": False}} if off else None,
    )
    async with PluginTestHost(root) as host:
        assert (await prepare(host, root)).status == "skipped"
        response = await passive_status(host, root)
        assert not response["diagnostics"]
        assert response["workspace"]["status"] == "unused"
        assert not response["workspace"]["diagnostics"]


async def test_loaded_record_diagnostics_never_expose_raw_errors(tmp_path):
    root = tmp_path / "host"
    seed(
        root,
        [("test", root / "workspaces/test", {}, [card(enabled=False)], True)],
    )
    async with PluginTestHost(root) as host:
        assert (await prepare(host, root)).status == "installed"
        record = host._loader.get_loaded_plugin("qwenpaw-mcp")
        record.diagnostics.append("credential parser error " + SECRET)
        response = await passive_status(host, root)
        assert response["status"] == "active"
        assert "plugin_runtime_failed" not in response["diagnostics"]
        record.status = "failed"
        response = await passive_status(host, root)
        assert response["status"] == "failed"
        assert "plugin_runtime_failed" in response["diagnostics"]
