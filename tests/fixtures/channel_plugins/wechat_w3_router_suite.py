"""Actual config HTTP router with accepted real G3/session fixture.

This is router integration; app lifespan/Manager lifecycle remains separately
unproved. No Service/Loader/worker/Agent substitutes or platform SDK are used.
"""

import base64
import asyncio
from importlib import import_module
from types import SimpleNamespace
from urllib.parse import urlencode

from fastapi import FastAPI
import httpx
import psutil
import pytest
import pytest_asyncio

from management_process_suite import (
    KEY,
    management_runtime,
    local_management_http,
    active_management,
    eventually,
)
from wechat_w2_session_suite import qr, qr_leases, narrow_legacy_test_guard


@pytest_asyncio.fixture
async def api_http(qr, monkeypatch):
    app = FastAPI()
    app.include_router(import_module("qwenpaw.app.routers.config").router)
    app.state.plugin_loader = qr.rt.loader
    app.state.channel_management = qr.rt.service
    app.state.channel_qrcode_sessions = qr.sessions
    from qwenpaw.app.multi_agent_manager import MultiAgentManager

    manager = MultiAgentManager()
    manager.channel_qrcode_sessions = qr.sessions
    app.state.multi_agent_manager = manager
    reloads = []
    original_reload = manager.reload_agent

    async def observe_reload(agent_id):
        reloads.append(agent_id)
        return await original_reload(agent_id)

    def forbidden_workspace(*args, **kwargs):
        raise AssertionError("static QR request constructed a Workspace")

    monkeypatch.setattr(manager, "reload_agent", observe_reload)
    monkeypatch.setattr(manager, "_create_workspace", forbidden_workspace)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        headers={"X-Agent-Id": "A"},
    ) as client:
        yield SimpleNamespace(
            client=client, app=app, qr=qr, manager=manager, reloads=reloads
        )


@pytest.mark.asyncio
async def test_actual_http_fetch_dispatches_installed_descriptor_to_real_sessions(
    api_http,
):
    fixture = api_http
    assert (
        fixture.qr.rt.loader.registry.get_channel_catalog_entry(KEY)
        is not None
    )
    response = await fixture.client.get(f"/config/channels/{KEY}/qrcode")
    assert response.status_code == 200, response.text
    data = response.json()
    assert set(data) == {"qrcode_img", "poll_token"}
    assert base64.b64decode(data["qrcode_img"])[:8] == b"\x89PNG\r\n\x1a\n"
    assert data["poll_token"] != "fixture-continuation"
    assert len(fixture.qr.http.requests) == 1
    assert fixture.qr.http.requests[0]["workspace_id"] == "A"
    assert qr_leases(fixture.qr)


async def confirmed_http(fixture):
    fetched = await fixture.client.get(f"/config/channels/{KEY}/qrcode")
    assert fetched.status_code == 200, fetched.text
    token = fetched.json()["poll_token"]
    polled = await fixture.client.get(
        f"/config/channels/{KEY}/qrcode/status", params={"token": token}
    )
    assert polled.status_code == 200, polled.text
    assert polled.json() == {
        "status": "confirmed",
        "credentials": {"api_token": "fixture-result-secret"},
    }
    return token, {**fixture.qr.scope.config, **polled.json()["credentials"]}


def profile_bytes(fixture):
    return {
        name: {
            p.relative_to(path): p.read_bytes() for p in path.rglob("*.json")
        }
        for name, path in fixture.qr.paths.items()
    }


@pytest.mark.asyncio
async def test_http_provenance_save_uses_worker_and_reloads_captured_a(
    api_http, monkeypatch
):
    from qwenpaw.config.config import load_agent_config
    from qwenpaw.config.utils import load_config, save_config
    from wechat_qr_io_support import FileIOGate

    fixture = api_http
    token, values = await confirmed_http(fixture)
    before_b = profile_bytes(fixture)["B"]
    root = load_config()
    root.agents.active_agent = "B"
    save_config(root)
    config = import_module("qwenpaw.config.config")
    gate = FileIOGate(
        monkeypatch,
        config,
        "write_json_atomic",
        lambda path, *a, **kw: path == fixture.qr.paths["A"] / "agent.json",
    )
    task = asyncio.create_task(
        fixture.client.put(
            f"/config/channels/{KEY}",
            json=values,
            headers={"X-Channel-Auth-Session": token},
        )
    )
    try:
        await gate.wait(task)
        assert gate.calls == 1 and qr_leases(fixture.qr)
        assert fixture.reloads == []
        gate.release.set()
        response = await task
        assert response.status_code == 200, response.text
        await eventually(lambda: fixture.reloads == ["A"])
        assert not fixture.manager.agents
        assert (
            getattr(load_agent_config("A").channels, KEY)["api_token"]
            == values["api_token"]
        )
        assert profile_bytes(fixture)["B"] == before_b
        assert not qr_leases(fixture.qr)
        assert fixture.manager._config_generations == {"A": 1}
    finally:
        gate.release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid",
    [
        "unknown",
        "wrong-scope",
        "mismatch",
        "expired",
        "consumed",
        "source",
        "config",
        "path",
    ],
)
async def test_http_invalid_provenance_never_writes(
    api_http, monkeypatch, tmp_path, invalid
):
    from qwenpaw.config.config import (
        AgentProfileConfig,
        load_agent_config,
        save_agent_config,
    )
    from qwenpaw.config.utils import load_config, save_config

    fixture = api_http
    token, values = await confirmed_http(fixture)
    headers = {"X-Channel-Auth-Session": token}
    if invalid == "unknown":
        headers["X-Channel-Auth-Session"] = "not-a-session"
    elif invalid == "wrong-scope":
        headers["X-Agent-Id"] = "B"
    elif invalid == "mismatch":
        values["api_token"] = "edited-token"
    elif invalid == "expired":
        now = fixture.qr.api.monotonic() + 301
        monkeypatch.setattr(fixture.qr.api, "monotonic", lambda: now)
    elif invalid == "consumed":
        saved = await fixture.client.put(
            f"/config/channels/{KEY}", json=values, headers=headers
        )
        assert saved.status_code == 200, saved.text
        await eventually(lambda: fixture.reloads == ["A"])
    elif invalid == "source":
        driver = fixture.qr.rt.record.source_path / KEY / "driver.py"
        driver.write_text(driver.read_text() + "\n# changed source revision\n")
    elif invalid == "config":
        profile = load_agent_config("A")
        setattr(
            profile.channels,
            KEY,
            {**fixture.qr.scope.config, "behavior": "edited"},
        )
        save_agent_config("A", profile)
    elif invalid == "path":
        root = load_config()
        profile = AgentProfileConfig.model_validate(
            load_agent_config("A").model_dump()
        )
        root.agents.profiles["A"].workspace_dir = str(tmp_path / "moved-A")
        save_config(root)
        profile.workspace_dir = str(tmp_path / "moved-A")
        save_agent_config("A", profile)
    before = profile_bytes(fixture)
    reloads = list(fixture.reloads)
    response = await fixture.client.put(
        f"/config/channels/{KEY}", json=values, headers=headers
    )
    expected = {
        "unknown": 400,
        "wrong-scope": 403,
        "mismatch": 403,
        "expired": 400,
        "consumed": 400,
        "source": 409,
        "config": 403,
        "path": 403,
    }[invalid]
    assert response.status_code == expected, response.text
    if invalid == "source":
        assert response.json() == {"detail": "SOURCE_REVISION_MISMATCH"}
    assert "fixture-result-secret" not in response.text
    assert profile_bytes(fixture) == before and fixture.reloads == reloads


@pytest.mark.asyncio
async def test_actual_loader_disable_then_source_replace_revokes_original_http_provenance(
    api_http,
):
    from qwenpaw.config.utils import load_config

    fixture = api_http
    token, values = await confirmed_http(fixture)
    disabled = await fixture.qr.rt.loader.lifecycle.set_enabled(KEY, False)
    assert disabled.quiescent, disabled.errors
    assert not qr_leases(fixture.qr)
    driver = fixture.qr.rt.sample.source / KEY / "driver.py"
    driver.write_text(driver.read_text() + "\n# incoming updated source\n")
    original_revision = (
        fixture.qr.rt.record.backend_binding.description.payload.source_revision
    )
    replacement = await fixture.qr.rt.loader.load_plugin_from_path(
        fixture.qr.rt.sample.source, config={"enabled": True}, force=True
    )
    assert not load_config().plugins[KEY]["enabled"]
    replacement = await fixture.qr.rt.loader.lifecycle.set_enabled(KEY, True)
    assert replacement.enabled, replacement.diagnostics
    assert (
        replacement.backend_binding.description.payload.source_revision
        != original_revision
    )
    assert not qr_leases(fixture.qr)
    before = profile_bytes(fixture)
    response = await fixture.client.put(
        f"/config/channels/{KEY}",
        json=values,
        headers={"X-Channel-Auth-Session": token},
    )
    assert response.status_code == 400, response.text
    assert profile_bytes(fixture) == before and not fixture.reloads


@pytest.mark.asyncio
async def test_http_persistence_failure_keeps_confirmed_session_for_exact_retry(
    api_http, monkeypatch
):
    fixture = api_http
    token, values = await confirmed_http(fixture)
    before = profile_bytes(fixture)

    def fail(*args, **kwargs):
        raise OSError("isolated fixture disk failure")

    with monkeypatch.context() as patch:
        patch.setattr("qwenpaw.config.config.save_agent_config", fail)
        with pytest.raises(OSError, match="fixture disk failure"):
            await fixture.client.put(
                f"/config/channels/{KEY}",
                json=values,
                headers={"X-Channel-Auth-Session": token},
            )
    assert profile_bytes(fixture) == before and not fixture.reloads
    assert qr_leases(fixture.qr)
    response = await fixture.client.put(
        f"/config/channels/{KEY}",
        json=values,
        headers={"X-Channel-Auth-Session": token},
    )
    assert response.status_code == 200, response.text
    assert not qr_leases(fixture.qr)


async def asgi_poll(fixture, token, incoming, sent):
    await incoming.put(
        {"type": "http.request", "body": b"", "more_body": False}
    )
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": f"/config/channels/{KEY}/qrcode/status",
        "raw_path": f"/config/channels/{KEY}/qrcode/status".encode(),
        "root_path": "",
        "query_string": urlencode({"token": token}).encode(),
        "headers": [(b"x-agent-id", b"A")],
        "client": ("127.0.0.1", 1),
        "server": ("testserver", 80),
    }

    async def send(message):
        sent.append(message)

    await fixture.app(scope, incoming.get, send)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "end", ["delete", "disconnect", "disconnect-one-of-two"]
)
async def test_http_cancel_and_disconnect_reap_exact_real_worker(
    api_http, end
):
    fixture = api_http
    fetched = await fixture.client.get(f"/config/channels/{KEY}/qrcode")
    token = fetched.json()["poll_token"]
    fixture.qr.http.reply_gate.clear()
    incoming, sent = asyncio.Queue(), []
    first = asyncio.create_task(asgi_poll(fixture, token, incoming, sent))
    second = None
    try:
        await eventually(lambda: len(fixture.qr.http.requests) == 2)
        (lease,) = active_management(fixture.qr.rt)
        pid = fixture.qr.rt.owners.resource(lease).health_snapshot()["pid"]
        assert psutil.pid_exists(pid)
        if end == "disconnect-one-of-two":
            second = asyncio.create_task(
                fixture.client.get(
                    f"/config/channels/{KEY}/qrcode/status",
                    params={"token": token},
                )
            )
            await eventually(
                lambda: next(
                    iter(fixture.qr.sessions._sessions.values())
                ).waiters
                == 2
            )
        if end == "delete":
            response = await fixture.client.delete(
                f"/config/channels/{KEY}/qrcode", params={"token": token}
            )
            assert response.status_code == 204, response.text
        else:
            await incoming.put({"type": "http.disconnect"})
        outcome = (await asyncio.gather(first, return_exceptions=True))[0]
        if end == "delete":
            assert (
                outcome is None
            ), "server-side DELETE leaked a cancelled child operation out of HTTP"
            assert (
                next(m for m in sent if m["type"] == "http.response.start")[
                    "status"
                ]
                == 400
            )
            body = b"".join(m.get("body", b"") for m in sent)
            assert b"QR session is no longer active" in body
            assert b"fixture-result-secret" not in body
        else:
            assert outcome is None
        if second is not None:
            assert psutil.pid_exists(pid) and not second.done()
            fixture.qr.http.reply_gate.set()
            response = await second
            assert response.json()["status"] == "confirmed"
        else:
            assert not psutil.pid_exists(pid) and not active_management(
                fixture.qr.rt
            )
            assert bool(qr_leases(fixture.qr)) == (end == "disconnect")
        assert not psutil.pid_exists(pid)
        if end != "delete":
            assert (
                next(m for m in sent if m["type"] == "http.response.start")[
                    "status"
                ]
                == 499
            )
    finally:
        fixture.qr.http.reply_gate.set()
        for task in (first, second):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (first, second) if task), return_exceptions=True
        )


@pytest.mark.asyncio
async def test_manual_edit_invalidates_old_confirmed_scope_before_reload(
    api_http, monkeypatch
):
    from qwenpaw.app.workspace import Workspace
    from qwenpaw.config.config import load_agent_config

    fixture = api_http
    token, values = await confirmed_http(fixture)
    workspace = Workspace("A", str(fixture.qr.paths["A"]))
    workspace._config = load_agent_config("A")
    fixture.manager.agents["A"] = workspace
    reloads = []
    monkeypatch.setattr(
        "qwenpaw.app.routers.config.schedule_agent_reload",
        lambda request, agent_id: reloads.append(agent_id),
    )
    values["api_token"] = "manual-replacement-token"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(
            app=fixture.app, raise_app_exceptions=False
        ),
        base_url="http://testserver",
        headers={"X-Agent-Id": "A"},
    ) as client:
        response = await client.put(f"/config/channels/{KEY}", json=values)
    assert (
        getattr(load_agent_config("A").channels, KEY)["api_token"]
        == "manual-replacement-token"
    )
    assert (response.status_code, len(qr_leases(fixture.qr)), reloads) == (
        200,
        0,
        ["A"],
    ), "manual save must persist, revoke the old scope, and schedule reload without a post-commit error"


@pytest.mark.asyncio
async def test_actual_outer_http_task_cancellation_propagates_after_worker_cleanup(
    api_http,
):
    fixture = api_http
    fetched = await fixture.client.get(f"/config/channels/{KEY}/qrcode")
    token = fetched.json()["poll_token"]
    fixture.qr.http.reply_gate.clear()
    incoming, sent = asyncio.Queue(), []
    task = asyncio.create_task(asgi_poll(fixture, token, incoming, sent))
    try:
        await eventually(lambda: len(fixture.qr.http.requests) == 2)
        (lease,) = active_management(fixture.qr.rt)
        pid = fixture.qr.rt.owners.resource(lease).health_snapshot()["pid"]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not psutil.pid_exists(pid) and not active_management(
            fixture.qr.rt
        )
        assert qr_leases(fixture.qr)
        assert not any(
            message["type"] == "http.response.start" for message in sent
        )
        assert not any(
            getattr(pending.get_coro(), "__qualname__", "")
            == "_qrcode_request.<locals>.disconnected"
            for pending in asyncio.all_tasks()
            if not pending.done()
        )
        fixture.qr.http.reply_gate.set()
        polled = await fixture.client.get(
            f"/config/channels/{KEY}/qrcode/status", params={"token": token}
        )
        assert (
            polled.status_code == 200
            and polled.json()["status"] == "confirmed"
        )
        assert len(fixture.qr.http.requests) == 3
    finally:
        fixture.qr.http.reply_gate.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_actual_http_delete_releases_real_seeded_qr_session(api_http):
    fixture = api_http
    fetched = await fixture.qr.sessions.fetch(fixture.qr.scope, {})
    assert qr_leases(fixture.qr) and len(fixture.qr.http.requests) == 1
    response = await fixture.client.delete(
        f"/config/channels/{KEY}/qrcode",
        params={"token": fetched.poll_token},
    )
    assert response.status_code == 204, response.text
    assert response.content == b""
    assert not qr_leases(fixture.qr)
    assert len(fixture.qr.http.requests) == 1


@pytest.mark.asyncio
async def test_http_provenance_merge_keeps_unrelated_latest_config(
    api_http, monkeypatch
):
    from qwenpaw.config.config import load_agent_config, save_agent_config
    from wechat_w2_session_suite import source_gate

    fixture = api_http
    token, values = await confirmed_http(fixture)
    gate = source_gate(fixture.qr, monkeypatch)
    task = asyncio.create_task(
        fixture.client.put(
            f"/config/channels/{KEY}",
            json=values,
            headers={"X-Channel-Auth-Session": token},
        )
    )
    try:
        await gate.wait(task)
        profile = load_agent_config("A")
        profile.name = "updated while authorization scanned"
        setattr(
            profile.channels,
            "unrelated_manual",
            {"enabled": False, "keep": "latest"},
        )
        save_agent_config("A", profile)
        gate.release.set()
        response = await task
        assert response.status_code == 200, response.text
        latest = load_agent_config("A")
        assert latest.name == "updated while authorization scanned"
        assert getattr(latest.channels, "unrelated_manual") == {
            "enabled": False,
            "keep": "latest",
        }
        assert (
            getattr(latest.channels, KEY)["api_token"] == values["api_token"]
        )
    finally:
        gate.release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_http_provenance_manual_edit_waits_for_commit_then_wins(
    api_http, monkeypatch
):
    from qwenpaw.app.workspace import Workspace
    from qwenpaw.config.config import load_agent_config
    from wechat_qr_io_support import FileIOGate

    fixture = api_http
    token, values = await confirmed_http(fixture)
    workspace = Workspace("A", str(fixture.qr.paths["A"]))
    workspace._config = load_agent_config("A")
    fixture.manager.agents["A"] = workspace
    reloads = []
    monkeypatch.setattr(
        "qwenpaw.app.routers.config.schedule_agent_reload",
        lambda request, agent_id: reloads.append(agent_id),
    )
    config = import_module("qwenpaw.config.config")
    gate = FileIOGate(
        monkeypatch,
        config,
        "write_json_atomic",
        lambda path, *a, **kw: path == fixture.qr.paths["A"] / "agent.json",
    )
    save = asyncio.create_task(
        fixture.client.put(
            f"/config/channels/{KEY}",
            json=values,
            headers={"X-Channel-Auth-Session": token},
        )
    )
    manual = None
    try:
        await gate.wait(save)
        manual = asyncio.create_task(
            fixture.client.put(
                f"/config/channels/{KEY}",
                json={**values, "api_token": "manual-final"},
            )
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not manual.done() and qr_leases(fixture.qr)
        gate.release.set()
        first, last = await asyncio.gather(save, manual)
        assert first.status_code == last.status_code == 200
        assert (
            getattr(load_agent_config("A").channels, KEY)["api_token"]
            == "manual-final"
        )
        assert reloads == ["A", "A"] and not qr_leases(fixture.qr)
        assert gate.calls == 2
    finally:
        gate.release.set()
        await asyncio.gather(
            save, *([manual] if manual else []), return_exceptions=True
        )
