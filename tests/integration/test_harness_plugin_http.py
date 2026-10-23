# -*- coding: utf-8 -*-
"""Dynamic Harness HTTP routes and executable frontend entry ownership."""

# The integration host exposes its app and loader for lifecycle checks.
# pylint: disable=protected-access

import asyncio

import pytest

from qwenpaw.app.routers.frontend_plugin import router as frontend_router
from qwenpaw.plugins.testing import PluginTestHost

from tests.unit.plugins.harness_bridge_fixture import SOURCE
from tests.unit.plugins.test_sdk_entry_contract import write_plugin


@pytest.mark.integration
async def test_disabled_harness_script_is_not_served_from_installed_copy(
    tmp_path,
):
    """Ordinary disable revokes the installed Harness executable."""
    source = tmp_path / "harness-owner"
    write_plugin(
        source,
        SOURCE,
        entry={"backend": "main.py", "frontend": "frontend/dist/index.js"},
        sdk={"version": "1.1.0", "capabilities": ["harnesses"]},
        meta={"harnesses": [{"id": "fake-backend", "label": "Fake"}]},
    )
    entry = source / "frontend" / "dist" / "index.js"
    entry.parent.mkdir(parents=True)
    entry.write_text("globalThis.fakeHarnessLoaded = true;", encoding="utf-8")

    async with PluginTestHost(tmp_path / "host") as host:
        host._http_app.state.plugin_loader = (
            host._loader
        )  # pylint: disable=protected-access
        host._http_app.include_router(  # pylint: disable=protected-access
            frontend_router,
            prefix="/api",
        )
        assert (await host.install(source)).ok
        path = (
            "/api/frontend_plugin/harness-owner/files/frontend/dist/index.js"
        )
        active = await host.request("GET", path)
        assert active.status == 200 and "fakeHarnessLoaded" in active.text

        assert (await host.set_enabled("harness-owner", False)).ok
        listed = await host.request("GET", "/api/frontend_plugin")
        assert not any(
            item.get("frontend_entry")
            for item in listed.json()
            if item["id"] == "harness-owner"
        )
        disabled = await host.request("GET", path)
        assert disabled.status in {404, 409}


@pytest.mark.integration
async def test_unactivated_harness_disk_fallback_cannot_execute(tmp_path):
    """Disk fallback keeps frontend-only plugins but denies SDK Harnesses."""
    async with PluginTestHost(tmp_path / "host") as host:
        host._http_app.include_router(  # pylint: disable=protected-access
            frontend_router,
            prefix="/api",
        )
        # Model a failed or not-yet-activated copy with no live loader. A
        # legitimate frontend-only plugin beside it must remain available.
        for plugin_id, sdk in (
            (
                "failed-harness",
                {"version": "1.1.0", "capabilities": ["harnesses"]},
            ),
            ("frontend-only", None),
        ):
            source = host.root / "plugins" / plugin_id
            fields = {
                "entry": {
                    **({"backend": "main.py"} if sdk is not None else {}),
                    "frontend": "frontend/dist/index.js",
                },
            }
            if sdk is not None:
                fields.update(
                    {
                        "sdk": sdk,
                        "meta": {
                            "harnesses": [
                                {"id": "failed-backend", "label": "Failed"},
                            ],
                        },
                    },
                )
            write_plugin(source, "plugin = object()\n", **fields)
            entry = source / "frontend" / "dist" / "index.js"
            entry.parent.mkdir(parents=True)
            entry.write_text(
                f"globalThis.{plugin_id.replace('-', '_')} = true;",
                encoding="utf-8",
            )
        host._http_app.state.plugin_loader = (
            None  # pylint: disable=protected-access
        )

        listed = await host.request("GET", "/api/frontend_plugin")
        assert not any(
            item.get("frontend_entry")
            for item in listed.json()
            if item["id"] == "failed-harness"
        )
        blocked = await host.request(
            "GET",
            "/api/frontend_plugin/failed-harness/files/frontend/dist/index.js",
        )
        assert blocked.status in {404, 409}
        compatible = await host.request(
            "GET",
            "/api/frontend_plugin/frontend-only/files/frontend/dist/index.js",
        )
        assert compatible.status == 200


@pytest.mark.integration
async def test_dynamic_gateway_keeps_other_harness_available_on_disable(
    tmp_path,
):
    """Independent owners keep their routes and unknown paths stay closed."""
    async with PluginTestHost(tmp_path / "host") as host:
        for plugin_id, backend_id in (
            ("owner-one", "first"),
            ("owner-two", "second"),
        ):
            source = tmp_path / plugin_id
            write_plugin(
                source,
                SOURCE.replace("fake-backend", backend_id),
                sdk={"version": "1.1.0", "capabilities": ["harnesses"]},
                meta={"harnesses": [{"id": backend_id, "label": backend_id}]},
            )
            assert (await host.install(source)).ok

        catalog = await host.request("GET", "/api/harnesses")
        assert catalog.status == 200
        assert {item["id"] for item in catalog.json()["providers"]} == {
            "first",
            "second",
        }
        assert all(
            item["id"] and item["label"]
            for item in catalog.json()["providers"]
        )
        for method, suffix in (
            ("GET", "models"),
            ("GET", "skills"),
            ("GET", "mcp"),
            ("POST", "status"),
            ("POST", "login"),
            ("POST", "logout"),
        ):
            unknown = await host.request(
                method,
                f"/api/harnesses/unknown/{suffix}",
                **({"json": {"settings": {}}} if method == "POST" else {}),
            )
            assert unknown.status == 404
        for backend_id in ("first", "second"):
            models = await host.request(
                "GET",
                f"/api/harnesses/{backend_id}/models",
                headers={"X-Agent-Id": "test"},
            )
            assert models.status == 200
            assert models.json()["models"][0]["id"] == "fake-model"

        assert (await host.set_enabled("owner-one", False)).ok
        assert (
            await host.request("GET", "/api/harnesses/first/models")
        ).status in {404, 409}
        assert (
            await host.request("GET", "/api/harnesses/second/models")
        ).status == 200


@pytest.mark.integration
async def test_harness_gateway_uses_real_auth_and_selected_agent(
    tmp_path,
    monkeypatch,
):
    """Actual auth middleware and Agent header govern a dynamic route."""
    from qwenpaw.app.auth import AuthMiddleware, register_user
    from qwenpaw.config.utils import load_config, save_config

    async with PluginTestHost(tmp_path / "host") as host:
        monkeypatch.setenv("QWENPAW_AUTH_ENABLED", "true")
        config = load_config()
        config.security.allow_no_auth_hosts = []
        save_config(config)
        token = register_user("harness-test", "local-only-password")
        assert token
        host._http_app.add_middleware(
            AuthMiddleware,
        )  # pylint: disable=protected-access

        source = tmp_path / "auth-owner"
        write_plugin(
            source,
            SOURCE,
            sdk={"version": "1.1.0", "capabilities": ["harnesses"]},
            meta={"harnesses": [{"id": "fake-backend", "label": "Fake"}]},
        )
        assert (await host.install(source)).ok
        assert (
            await host.request("GET", "/api/harnesses/fake-backend/models")
        ).status == 401
        assert (
            await host.request(
                "GET",
                "/api/harnesses/fake-backend/models",
                headers={"Authorization": "Bearer invalid"},
            )
        ).status == 401

        await host._workspace("other")  # pylint: disable=protected-access
        authorized = await host.request(
            "GET",
            "/api/harnesses/fake-backend/models",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Agent-Id": "other",
            },
        )
        assert authorized.status == 200
        plugin = host._loader.get_loaded_plugin(
            "auth-owner",
        ).instance  # pylint: disable=protected-access
        assert plugin.created[-1].context.workspace_id == "other"


@pytest.mark.integration
async def test_dynamic_gateway_keeps_unsupported_logout_a_structured_conflict(
    tmp_path,
):
    """Unsupported backend operations retain the public 409 shape."""
    source = tmp_path / "unsupported-owner"
    code = SOURCE.replace(
        "    async def logout(self, context):\n"
        "        self.calls.append(context)",
        "    async def logout(self, context):\n"
        "        from qwenpaw.plugins.harnesses import "
        "HarnessOperationNotSupportedError\n"
        "        raise HarnessOperationNotSupportedError("
        "'CLI logout is unavailable')",
    )
    write_plugin(
        source,
        code,
        sdk={"version": "1.1.0", "capabilities": ["harnesses"]},
        meta={"harnesses": [{"id": "fake-backend", "label": "Fake"}]},
    )
    async with PluginTestHost(tmp_path / "host") as host:
        assert (await host.install(source)).ok
        response = await host.request(
            "POST",
            "/api/harnesses/fake-backend/logout",
            json={"settings": {}},
        )
        assert response.status == 409
        assert response.json()["detail"] == {
            "code": "logout_not_supported",
            "message": "CLI logout is unavailable",
        }


@pytest.mark.integration
async def test_matched_gateway_rejects_disable_during_agent_resolution(
    tmp_path,
):
    """A matched request cannot cross owner removal while finding its Agent."""
    source = tmp_path / "race-owner"
    write_plugin(
        source,
        SOURCE,
        sdk={"version": "1.1.0", "capabilities": ["harnesses"]},
        meta={"harnesses": [{"id": "fake-backend", "label": "Fake"}]},
    )
    async with PluginTestHost(tmp_path / "host") as host:
        assert (await host.install(source)).ok
        entered, release = asyncio.Event(), asyncio.Event()
        original = (
            host._workspace_manager.get_agent
        )  # pylint: disable=protected-access

        async def held_agent(agent_id):
            entered.set()
            await release.wait()
            return await original(agent_id)

        host._workspace_manager.get_agent = (
            held_agent  # pylint: disable=protected-access
        )
        pending = asyncio.create_task(
            host.request(
                "POST",
                "/api/harnesses/fake-backend/status",
                json={"settings": {}},
            ),
        )
        try:
            await asyncio.wait_for(entered.wait(), 5)
            assert (await host.set_enabled("race-owner", False)).ok
        finally:
            release.set()
        response = await asyncio.wait_for(pending, 5)
        assert response.status in {404, 409}


@pytest.mark.integration
async def test_old_gateway_cannot_call_reenabled_owner_after_config_read(
    tmp_path,
    monkeypatch,
):
    """A request bound to an old generation cannot invoke its replacement."""
    from qwenpaw.config import config as agent_config

    source = tmp_path / "same-owner"
    write_plugin(
        source,
        SOURCE,
        sdk={"version": "1.1.0", "capabilities": ["harnesses"]},
        meta={"harnesses": [{"id": "fake-backend", "label": "Fake"}]},
    )
    async with PluginTestHost(tmp_path / "host") as host:
        assert (await host.install(source)).ok
        entered, release = asyncio.Event(), asyncio.Event()
        original = agent_config.load_agent_config_async

        async def held_config(agent_id):
            entered.set()
            await release.wait()
            return await original(agent_id)

        monkeypatch.setattr(
            agent_config,
            "load_agent_config_async",
            held_config,
        )
        pending = asyncio.create_task(
            host.request(
                "GET",
                "/api/harnesses/fake-backend/models",
            ),
        )
        try:
            await asyncio.wait_for(entered.wait(), 5)
            assert (await host.set_enabled("same-owner", False)).ok
            assert (await host.set_enabled("same-owner", True)).ok
        finally:
            release.set()
        response = await asyncio.wait_for(pending, 5)
        assert response.status in {404, 409}
        current = host._loader.get_loaded_plugin(
            "same-owner",
        ).instance  # pylint: disable=protected-access
        assert not current.created
