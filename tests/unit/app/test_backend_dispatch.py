# -*- coding: utf-8 -*-
"""Agent CRUD and chat dispatch accept any admitted Harness ID."""

# The test host exposes no public workspace/loader accessor for real CRUD.
# Pytest fixtures are injected by their declared names.
# pylint: disable=protected-access,redefined-outer-name

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


async def test_third_harness_crud_and_workspace_chat_use_registry(host):
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
    with pytest.raises(SDKError, match="backend_unavailable"):
        _ = [
            item
            async for item in host.stream_chat("Hello", workspace_id="saved")
        ]


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
