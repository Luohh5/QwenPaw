# -*- coding: utf-8 -*-
"""Metadata and dynamic gateway use production registration and live guards."""

from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from starlette.routing import Match

# The fixtures retain their declared pytest names without loader aliases.
# pylint: disable-next=unused-import
from tests.unit.plugins.conftest import fresh_registry

# pylint: disable-next=unused-import
from tests.unit.plugins.test_sdk_entry_contract import plugin_loader
from tests.unit.plugins.harness_bridge_fixture import load_backend


@pytest.fixture(name="gateway")
async def gateway_fixture(tmp_path, loader):
    from qwenpaw.app.routers.agents import router
    from qwenpaw.config.config import AgentProfileRef, Config
    from qwenpaw.config.utils import save_config
    from qwenpaw.runtime.backends.execution import BackendExecution

    app = FastAPI()
    app.include_router(router, prefix="/api")
    registry = loader.registry
    registry.set_plugin_http_app(app)
    app.state.plugin_registry = registry
    path = tmp_path / "workspace"
    path.mkdir()
    config = Config()
    config.agents.profiles["one"] = AgentProfileRef(
        id="one",
        workspace_dir=str(path),
    )
    save_config(config)
    runtime = BackendExecution(
        path,
        agent_id="one",
        registry=registry.backend_registry,
    )
    workspace = SimpleNamespace(
        backend_runtime=runtime,
        agent_id="one",
        workspace_dir=path,
        config=SimpleNamespace(backend="fake-backend", backend_settings={}),
    )

    async def get_agent(agent_id):
        assert agent_id == "one"
        return workspace

    app.state.multi_agent_manager = SimpleNamespace(get_agent=get_agent)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Agent-Id": "one"},
    ) as client:
        yield SimpleNamespace(app=app, client=client, loader=loader)
    await runtime.close()
    from qwenpaw.plugins.lifecycle import UnloadMode

    await loader.lifecycle.unload_all(UnloadMode.UNLOAD)


async def test_catalog_is_lazy_and_precedes_dynamic_agent_route(
    tmp_path,
    gateway,
):
    plugin = await load_backend(tmp_path, gateway.loader)
    response = await gateway.client.get("/api/agents/backends")
    assert response.status_code == 200
    rows = response.json()["backends"]
    assert any(row["id"] == "qwenpaw" for row in rows)
    descriptor = next(row for row in rows if row["id"] == "fake-backend")
    assert {
        "id",
        "label",
        "plugin_id",
        "status",
        "capabilities",
        "config_fields",
    } <= descriptor.keys()
    assert (
        descriptor["plugin_id"] == "bridge-owner"
        and descriptor["status"] == "available"
    )
    assert not plugin.created
    unknown_agent = await gateway.client.post(
        "/api/harnesses/fake-backend/status",
        headers={"X-Agent-Id": "missing"},
        json={},
    )
    assert unknown_agent.status_code == 404
    assert not plugin.created


async def test_gateway_preserves_other_owner_and_disappears_after_last_release(
    tmp_path,
    gateway,
):
    assert (await gateway.client.get("/api/harnesses")).status_code == 404
    one = await load_backend(tmp_path, gateway.loader)
    two = await load_backend(
        tmp_path,
        gateway.loader,
        owner="second-owner",
        backend="second-backend",
    )
    assert (await gateway.client.get("/api/harnesses")).status_code == 200
    assert not one.created and not two.created
    await gateway.loader.unload_plugin("bridge-owner")
    response = await gateway.client.post(
        "/api/harnesses/second-backend/status",
        json={},
    )
    assert response.status_code == 200 and response.json()["installed"]
    assert not one.created and len(two.created) == 1
    await gateway.loader.unload_plugin("second-owner")
    assert (await gateway.client.get("/api/harnesses")).status_code == 404


async def test_matched_old_handler_cannot_bypass_generation(tmp_path, gateway):
    old = await load_backend(tmp_path, gateway.loader)
    path = "/api/harnesses/fake-backend/status"
    scope = {"type": "http", "method": "POST", "path": path, "root_path": ""}
    matched = next(
        route
        for route in gateway.app.routes
        if route.matches(scope)[0] is Match.FULL
    )
    frozen = FastAPI()
    frozen.router.routes = [matched]
    frozen.state.plugin_registry = gateway.app.state.plugin_registry
    frozen.state.multi_agent_manager = gateway.app.state.multi_agent_manager
    await gateway.loader.unload_plugin("bridge-owner")
    replacement = await load_backend(tmp_path / "replacement", gateway.loader)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=frozen),
        base_url="http://test",
        headers={"X-Agent-Id": "one"},
    ) as client:
        stale = await client.post(path, json={})
    assert stale.status_code in {404, 409}
    assert not old.created and not replacement.created
    assert (await gateway.client.post(path, json={})).status_code == 200
    assert len(replacement.created) == 1


@pytest.mark.parametrize("state", ["disabled", "failed"])
async def test_catalog_keeps_installed_unavailable_manifest_metadata(
    tmp_path,
    gateway,
    state,
):
    from qwenpaw.config.utils import load_config, save_config
    from tests.unit.plugins.test_sdk_entry_contract import write_plugin

    marker = tmp_path / "module-executed"
    owner = "unavailable-owner"
    write_plugin(
        tmp_path / "plugins" / owner,
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
        "raise RuntimeError('intentional activation failure')\n",
        sdk={"version": "1.1.0", "capabilities": ["harnesses"]},
        meta={
            "harnesses": [
                {"id": "unavailable-backend", "label": "Unavailable backend"},
            ],
        },
    )
    config = load_config()
    config.plugins[owner] = {"enabled": state != "disabled"}
    save_config(config)
    await gateway.loader.load_all_plugins(config.plugins, activate=True)
    assert marker.exists() is (state == "failed")
    response = await gateway.client.get("/api/agents/backends")
    assert response.status_code == 200
    row = next(
        row
        for row in response.json()["backends"]
        if row["id"] == "unavailable-backend"
    )
    assert (row["plugin_id"], row["status"]) == (owner, state)
    assert row["capabilities"] == {} and row["config_fields"] == []
    assert (
        gateway.loader.registry.backend_registry.get("unavailable-backend")
        is None
    )
    assert (await gateway.client.get("/api/harnesses")).status_code == 404
