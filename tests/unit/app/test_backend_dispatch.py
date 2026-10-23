# -*- coding: utf-8 -*-
"""Agent CRUD and chat dispatch accept any admitted Harness ID."""

# The test host exposes no public workspace/loader accessor for real CRUD.
# Pytest fixtures are injected by their declared names.
# pylint: disable=protected-access,redefined-outer-name

import asyncio
from unittest.mock import AsyncMock

import pytest

from qwenpaw.app.agent_startup import AgentStartupStatus
from qwenpaw.app.routers.agents import router as agents_router
from qwenpaw.plugins import SDKError
from qwenpaw.plugins.testing import PluginTestHost

from tests.unit.plugins.harness_bridge_fixture import SOURCE
from tests.unit.plugins.test_sdk_entry_contract import write_plugin


@pytest.fixture()
async def host(tmp_path):
    source = tmp_path / "third-owner"
    write_plugin(
        source,
        SOURCE.replace("fake-backend", "third-backend"),
        sdk={
            "version": "1.1.0",
            "capabilities": ["harnesses", "storage", "resources"],
        },
        meta={
            "harnesses": [{"id": "third-backend", "label": "Third backend"}],
        },
    )
    async with PluginTestHost(tmp_path / "host") as test_host:
        manager = (
            test_host._workspace_manager
        )  # pylint: disable=protected-access
        manager.schedule_agent_startup = lambda _agent_id: None
        manager.note_agent_config_changed = lambda _agent_id: None
        manager.reload_agent = AsyncMock(return_value=False)
        manager.is_agent_startup_in_progress = lambda _agent_id: False
        manager.stop_agent = AsyncMock()
        manager.get_agent_startup_status = lambda _agent_id, *, enabled: (
            AgentStartupStatus.RUNNING
            if enabled
            else AgentStartupStatus.DISABLED
        )
        test_host._http_app.include_router(  # pylint: disable=protected-access
            agents_router,
            prefix="/api",
        )
        result = await test_host.install(source)
        assert result.ok, result
        yield test_host


async def test_third_harness_crud_and_workspace_chat_use_registry(
    host,
    tmp_path,
):
    created = await host.request(
        "POST",
        "/api/agents",
        json={"id": "third", "name": "Third", "backend": "third-backend"},
    )
    assert created.status == 201, created.text
    listed = await host.request("GET", "/api/agents")
    row = next(
        item for item in listed.json()["agents"] if item["id"] == "third"
    )
    assert (row["backend_status"], row["backend_plugin_id"]) == (
        "available",
        "third-owner",
    )
    updated = await host.request(
        "PUT",
        "/api/agents/third",
        json={"id": "third", "name": "Renamed", "backend": "third-backend"},
    )
    assert updated.status == 200, updated.text
    assert updated.json()["name"] == "Renamed"
    events = [
        item async for item in host.stream_chat("Hello", workspace_id="third")
    ]
    assert any(
        block.text == "first" for item in events for block in item.content
    ), events
    agent_file = tmp_path / "host/workspaces/third/agent.json"
    account_file = tmp_path / "host/plugin-data/third-owner/account.json"
    account_file.parent.mkdir(parents=True, exist_ok=True)
    account_file.write_bytes(b'{"account":"retained"}')
    agent_bytes = agent_file.read_bytes()
    account_bytes = account_file.read_bytes()
    removed = await host.uninstall("third-owner")
    assert removed.clean and removed.quiescent
    assert agent_file.read_bytes() == agent_bytes
    assert account_file.read_bytes() == account_bytes
    saved = await host.request("GET", "/api/agents/third")
    assert saved.status == 200, saved.text
    assert saved.json()["backend"] == "third-backend"
    listed = await host.request("GET", "/api/agents")
    row = next(
        item for item in listed.json()["agents"] if item["id"] == "third"
    )
    assert row["backend_status"] == "missing"
    deleted = await host.request("DELETE", "/api/agents/third")
    assert deleted.status == 200, deleted.text


async def test_saved_unknown_backend_does_not_fall_back_to_native(host):
    from qwenpaw.config.config import save_agent_config

    workspace = await host._workspace(
        "saved",
    )  # pylint: disable=protected-access
    config = workspace.config
    config.backend = "unknown-backend"
    save_agent_config("saved", config)
    listed = await host.request("GET", "/api/agents")
    row = next(
        item for item in listed.json()["agents"] if item["id"] == "saved"
    )
    assert (row["backend_status"], row["backend_plugin_id"]) == (
        "missing",
        None,
    )
    fetched = await host.request("GET", "/api/agents/saved")
    assert (
        fetched.status == 200
        and fetched.json()["backend"] == "unknown-backend"
    )
    renamed = await host.request(
        "PUT",
        "/api/agents/saved",
        json={
            "id": "saved",
            "name": "Still saved",
            "backend": "unknown-backend",
        },
    )
    assert renamed.status == 200, renamed.text
    assert renamed.json()["backend"] == "unknown-backend"
    assert renamed.json()["name"] == "Still saved"
    with pytest.raises(SDKError, match="backend_unavailable"):
        _ = [
            item
            async for item in host.stream_chat("Hello", workspace_id="saved")
        ]
    deleted = await host.request("DELETE", "/api/agents/saved")
    assert deleted.status == 200, deleted.text


@pytest.mark.parametrize("unavailable", ["missing", "disabled"])
async def test_backend_switch_checks_live_owner_before_saving(
    host,
    tmp_path,
    unavailable,
    monkeypatch,
):
    from qwenpaw.app.routers import agents as agent_routes

    created = await host.request(
        "POST",
        "/api/agents",
        json={"id": "switch", "name": "Native", "backend": "qwenpaw"},
    )
    assert created.status == 201, created.text
    agent_file = tmp_path / "host/workspaces/switch/agent.json"
    before = agent_file.read_bytes()
    if unavailable == "disabled":
        assert (await host.set_enabled("third-owner", False)).ok
    loop = asyncio.get_running_loop()
    original_catalog = agent_routes.backend_catalog

    def catalog_on_loop(*args, **kwargs):
        assert asyncio.get_running_loop() is loop
        return original_catalog(*args, **kwargs)

    monkeypatch.setattr(agent_routes, "backend_catalog", catalog_on_loop)
    rejected = await host.request(
        "PUT",
        "/api/agents/switch",
        json={
            "id": "switch",
            "name": "Native",
            "backend": (
                "unknown-backend"
                if unavailable == "missing"
                else "third-backend"
            ),
        },
    )
    assert rejected.status == 409, rejected.text
    assert rejected.json()["detail"]["code"] == "backend_unavailable"
    assert agent_file.read_bytes() == before
    saved = await host.request("GET", "/api/agents/switch")
    assert saved.status == 200 and saved.json()["backend"] == "qwenpaw"

    if unavailable == "disabled":
        assert (await host.set_enabled("third-owner", True)).ok
    switched = await host.request(
        "PUT",
        "/api/agents/switch",
        json={"id": "switch", "name": "Now third", "backend": "third-backend"},
    )
    assert switched.status == 200, switched.text
    assert switched.json()["backend"] == "third-backend"


async def test_disabled_backend_stays_manageable_but_cannot_run(host):
    created = await host.request(
        "POST",
        "/api/agents",
        json={
            "id": "disabled",
            "name": "Disabled",
            "backend": "third-backend",
        },
    )
    assert created.status == 201, created.text
    assert (await host.set_enabled("third-owner", False)).ok
    listed = await host.request("GET", "/api/agents")
    row = next(
        item for item in listed.json()["agents"] if item["id"] == "disabled"
    )
    assert (row["backend_status"], row["backend_plugin_id"]) == (
        "disabled",
        "third-owner",
    )
    with pytest.raises(SDKError, match="backend_unavailable"):
        _ = [
            item
            async for item in host.stream_chat(
                "Hello",
                workspace_id="disabled",
            )
        ]
    deleted = await host.request("DELETE", "/api/agents/disabled")
    assert deleted.status == 200, deleted.text


async def test_failed_backend_reports_unavailable_without_native_fallback(
    host,
):
    created = await host.request(
        "POST",
        "/api/agents",
        json={"id": "failed", "name": "Failed", "backend": "third-backend"},
    )
    assert created.status == 201, created.text
    _ = [
        item async for item in host.stream_chat("Hello", workspace_id="failed")
    ]
    record = (
        host._loader.get_loaded_plugin(  # pylint: disable=protected-access
            "third-owner",
        )
    )
    record.instance.close_fail = True
    try:
        result = await host._loader.lifecycle.reload("third-owner")
        assert not result.ok
        listed = await host.request("GET", "/api/agents")
        row = next(
            item for item in listed.json()["agents"] if item["id"] == "failed"
        )
        assert row["backend_status"] == "failed"
        with pytest.raises(SDKError, match="backend_unavailable"):
            _ = [
                item
                async for item in host.stream_chat(
                    "Hello",
                    workspace_id="failed",
                )
            ]
    finally:
        record.instance.close_fail = False
