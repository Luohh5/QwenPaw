"""Actual manual HTTP plus host Session/Owners/WeChat-claim lifecycle units.

WeChat is not registered yet. Confirmed host custody is seeded directly with
real production Session and RuntimeOwners objects, as in the accepted W2 units.
No WeChat Driver, management protocol result or official owner is fabricated.
These tests establish manual persistence/revocation, not WeChat QR integration.
"""

import asyncio
from hashlib import sha256
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
import httpx
import pytest
import pytest_asyncio

from management_process_suite import (
    KEY,
    management_runtime,
    local_management_http,
)
from wechat_w2_config_support import write_profiles
from wechat_w2_scope_suite import request
from wechat_w2_session_suite import narrow_legacy_test_guard


@pytest_asyncio.fixture
async def manual_scope(management_runtime, tmp_path, monkeypatch):
    from qwenpaw.app.multi_agent_manager import MultiAgentManager
    from qwenpaw.app.workspace import Workspace
    from qwenpaw.config.config import load_agent_config, save_agent_config
    from qwenpaw.plugins.ownership import RuntimeKey

    rt = await management_runtime()
    _, paths = write_profiles(tmp_path, monkeypatch, "wechat", {})
    profile = load_agent_config("A")
    profile.channels.wechat.bot_token_file = str(tmp_path / "auth-A" / "token")
    profile.channels.wechat.bot_token = "saved-A-token"
    profile.channels.wechat.base_url = "https://fixture.invalid"
    setattr(
        profile.channels,
        KEY,
        {"enabled": False, "host": "127.0.0.1", "port": 9},
    )
    save_agent_config("A", profile)
    b = load_agent_config("B")
    b.channels.wechat.bot_token_file = str(tmp_path / "auth-B" / "token")
    b.channels.wechat.bot_token = "saved-B-token"
    save_agent_config("B", b)
    api = import_module("qwenpaw.app.channels.qrcode_sessions")
    auth = import_module("qwenpaw.app.channels.wechat_auth_custody")
    sessions = api.ChannelQRCodeSessions(rt.loader, rt.service)
    manager = MultiAgentManager()
    manager.channel_qrcode_sessions = sessions
    workspace = Workspace("A", str(paths["A"]))
    workspace._config = load_agent_config("A")
    manager.agents["A"] = workspace
    app = FastAPI()
    app.include_router(import_module("qwenpaw.app.routers.config").router)
    app.state.plugin_loader = rt.loader
    app.state.channel_management = rt.service
    app.state.channel_qrcode_sessions = sessions
    app.state.multi_agent_manager = manager
    reloads = []
    monkeypatch.setattr(
        "qwenpaw.app.routers.config.schedule_agent_reload",
        lambda req, agent_id: reloads.append(agent_id),
    )

    def seed(agent_id, channel="wechat"):
        scope = api.resolve_channel_management_scope(
            request(header=agent_id), channel
        )
        lease = rt.owners.reserve(
            KEY,
            RuntimeKey("qrcode_session", channel, str(scope.workspace_dir)),
            rt.record.source_path,
            instance_id=f"seeded-host-custody-{agent_id}-{channel}",
        )
        session = api._Session(
            scope,
            lease,
            rt.record.backend_binding.description.payload.source_revision,
            {},
            api.monotonic() + 300,
        )
        session.status = "confirmed"
        session.credentials = (
            {"bot_token": f"confirmed-{agent_id}-token"}
            if channel == "wechat"
            else {"api_token": "confirmed-generic-token"}
        )
        rt.owners.attach(
            lease, lambda: sessions._stop(session), resource=session
        )
        if channel == "wechat":
            session.claims = auth.reserve_wechat_auth(
                rt.loader.registry,
                owner=lease,
                workspace_id=agent_id,
                workspace_dir=scope.workspace_dir,
                config={**scope.config, **session.credentials},
            )
        sessions._sessions[
            sha256(f"unit-{agent_id}-{channel}".encode()).hexdigest()
        ] = session
        return session

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        headers={"X-Agent-Id": "A"},
    ) as client:
        try:
            yield SimpleNamespace(
                rt=rt,
                api=api,
                sessions=sessions,
                manager=manager,
                workspace=workspace,
                app=app,
                client=client,
                paths=paths,
                reloads=reloads,
                seed=seed,
            )
        finally:
            await sessions.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["token", "path", "noncredential", "bulk"])
@pytest.mark.parametrize("persistence", ["success", "failure"])
async def test_manual_changed_scope_releases_exact_claims_only_after_commit(
    manual_scope, monkeypatch, tmp_path, change, persistence
):
    from qwenpaw.app.channels.management import ManagementError
    from qwenpaw.config.config import load_agent_config, save_agent_config

    fixture = manual_scope
    a, b = fixture.seed("A"), fixture.seed("B")
    extra = fixture.seed("A", KEY) if change == "bulk" else None
    extra_claims = ()
    if extra is not None:
        from qwenpaw.app.channels.wechat_auth_custody import (
            reserve_wechat_auth,
        )

        extra.claims = reserve_wechat_auth(
            fixture.rt.loader.registry,
            owner=extra.lease,
            workspace_id="A",
            workspace_dir=extra.scope.workspace_dir,
            config={
                "bot_token_file": str(
                    tmp_path / "untouched-channel-auth" / "token"
                )
            },
        )
        extra_claims = extra.claims
    a_claims, b_claims = a.claims, b.claims
    original_values = load_agent_config("A").channels.wechat.model_dump()
    values = dict(original_values)
    field, value = {
        "token": ("bot_token", "manual-new-token"),
        "path": ("bot_token_file", str(tmp_path / "changed-auth" / "token")),
        "noncredential": ("bot_prefix", "changed-prefix"),
        "bulk": ("bot_prefix", "bulk-prefix"),
    }[change]
    values[field] = value
    before = (fixture.paths["A"] / "agent.json").read_bytes()
    from wechat_qr_io_support import FileIOGate

    config = import_module("qwenpaw.config.config")
    atomic = config.write_json_atomic
    entered, release = asyncio.Event(), asyncio.Event()
    original_stop = fixture.sessions._stop

    async def gated_stop(session):
        if session is a:
            entered.set()
            await release.wait()
        return await original_stop(session)

    def publish(path, payload, *args, **kwargs):
        if Path(path) == fixture.paths["A"] / "agent.json":
            if persistence == "failure":
                raise OSError("isolated manual persistence failure")
        return atomic(path, payload, *args, **kwargs)

    monkeypatch.setattr(fixture.sessions, "_stop", gated_stop)
    monkeypatch.setattr(config, "write_json_atomic", publish)
    gate = FileIOGate(
        monkeypatch,
        config,
        "write_json_atomic",
        lambda path, *a, **kw: Path(path) == fixture.paths["A"] / "agent.json",
    )
    operation = asyncio.create_task(
        fixture.client.put(
            "/config/channels"
            if change == "bulk"
            else "/config/channels/wechat",
            json={"wechat": values} if change == "bulk" else values,
        )
    )
    try:
        async with asyncio.timeout(5):
            await entered.wait()
        assert (fixture.paths["A"] / "agent.json").read_bytes() == before
        assert fixture.sessions._revoking[("A", "wechat")] == 1
        assert a.status == "cancelled" and not a.credentials
        assert set(a_claims).issubset(fixture.rt.owners.leases())
        with pytest.raises(ManagementError) as busy:
            await fixture.sessions.fetch(a.scope, {})
        assert busy.value.reason_code == "MANAGEMENT_BUSY"
        release.set()
        await gate.wait(operation)
        assert fixture.sessions._revoking[("A", "wechat")] == 1
        assert a.lease not in fixture.rt.owners.leases()
        assert set(a_claims).isdisjoint(fixture.rt.owners.leases())
        assert fixture.reloads == []
        assert (fixture.paths["A"] / "agent.json").read_bytes() == before
        gate.release.set()
        if persistence == "failure":
            with pytest.raises(
                OSError, match="isolated manual persistence failure"
            ):
                await operation
            assert (fixture.paths["A"] / "agent.json").read_bytes() == before
            assert fixture.reloads == []
        else:
            response = await operation
            assert response.status_code == 200, response.text
            assert fixture.reloads == ["A"]
            assert (
                getattr(fixture.workspace.config.channels.wechat, field)
                == value
            )
        assert not a.claims and a.lease not in fixture.rt.owners.leases()
        assert set(a_claims).isdisjoint(fixture.rt.owners.leases())
        assert not fixture.sessions._revoking
        if extra is not None:
            assert (
                extra.status == "confirmed"
            ), "sparse bulk PUT revoked an untouched channel's QR scope"
            assert extra.lease in fixture.rt.owners.leases()
            assert set(extra_claims).issubset(fixture.rt.owners.leases())
        assert b.status == "confirmed" and b.credentials
        assert b.lease in fixture.rt.owners.leases()
        assert set(b_claims).issubset(fixture.rt.owners.leases())
    finally:
        release.set()
        gate.release.set()
        await asyncio.gather(operation, return_exceptions=True)
        monkeypatch.setattr(fixture.sessions, "_stop", original_stop)


@pytest.mark.asyncio
@pytest.mark.parametrize("persistence", ["success", "failure"])
async def test_manual_disable_closes_admission_before_persist_and_releases_claims(
    manual_scope, monkeypatch, persistence
):
    from qwenpaw.app.channels.management import ManagementError
    from qwenpaw.config.config import load_agent_config, save_agent_config

    fixture = manual_scope
    profile = load_agent_config("A")
    profile.channels.wechat.enabled = True
    save_agent_config("A", profile)
    before = (fixture.paths["A"] / "agent.json").read_bytes()
    a, b = fixture.seed("A"), fixture.seed("B")
    claims = a.claims
    values = load_agent_config("A").channels.wechat.model_dump()
    values["enabled"] = False
    entered, release = asyncio.Event(), asyncio.Event()
    original_stop = fixture.sessions._stop

    async def gated_stop(session):
        if session is a:
            entered.set()
            await release.wait()
        return await original_stop(session)

    from wechat_qr_io_support import FileIOGate

    config = import_module("qwenpaw.config.config")
    atomic = config.write_json_atomic

    def publish(path, payload, *args, **kwargs):
        if Path(path) == fixture.paths["A"] / "agent.json":
            if persistence == "failure":
                raise OSError("isolated disable persistence failure")
        return atomic(path, payload, *args, **kwargs)

    monkeypatch.setattr(fixture.sessions, "_stop", gated_stop)
    monkeypatch.setattr(config, "write_json_atomic", publish)
    gate = FileIOGate(
        monkeypatch,
        config,
        "write_json_atomic",
        lambda path, *a, **kw: Path(path) == fixture.paths["A"] / "agent.json",
    )
    operation = asyncio.create_task(
        fixture.client.put("/config/channels/wechat", json=values)
    )
    try:
        async with asyncio.timeout(5):
            await entered.wait()
        assert load_agent_config("A").channels.wechat.enabled
        assert not fixture.reloads
        with pytest.raises(ManagementError) as busy:
            await fixture.sessions.fetch(a.scope, {})
        assert busy.value.reason_code == "MANAGEMENT_BUSY"
        release.set()
        await gate.wait(operation)
        assert fixture.sessions._revoking[("A", "wechat")] == 1
        assert a.lease not in fixture.rt.owners.leases()
        assert set(claims).isdisjoint(fixture.rt.owners.leases())
        gate.release.set()
        if persistence == "failure":
            with pytest.raises(
                OSError, match="isolated disable persistence failure"
            ):
                await operation
            assert (fixture.paths["A"] / "agent.json").read_bytes() == before
            assert not fixture.reloads
        else:
            response = await operation
            assert response.status_code == 200, response.text
            assert not load_agent_config("A").channels.wechat.enabled
            assert fixture.reloads == ["A"]
        assert not fixture.sessions._revoking
        assert a.lease not in fixture.rt.owners.leases() and not a.claims
        assert b.lease in fixture.rt.owners.leases() and b.claims
    finally:
        release.set()
        gate.release.set()
        await asyncio.gather(operation, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["ordinary", "disable"])
async def test_bulk_closes_all_changed_admission_before_first_cleanup_await(
    manual_scope, local_management_http, monkeypatch, phase
):
    from qwenpaw.app.channels.management import ManagementError
    from qwenpaw.config.config import load_agent_config, save_agent_config

    fixture = manual_scope
    second_key = "w3_second_changed_scope"
    profile = load_agent_config("A")
    profile.channels.wechat.enabled = phase == "disable"
    setattr(
        profile.channels,
        second_key,
        {"enabled": phase == "disable", "marker": "old"},
    )
    setattr(
        profile.channels,
        KEY,
        {
            "enabled": False,
            "host": local_management_http.host,
            "port": local_management_http.port,
        },
    )
    save_agent_config("A", profile)
    first, second, b = (
        fixture.seed("A"),
        fixture.seed("A", second_key),
        fixture.seed("B"),
    )
    first_claims = first.claims
    before_second = dict(getattr(profile.channels, second_key))
    proposed_wechat = profile.channels.wechat.model_dump()
    proposed_second = dict(before_second)
    if phase == "disable":
        proposed_wechat["enabled"] = proposed_second["enabled"] = False
    else:
        proposed_wechat["bot_prefix"] = "group-new-prefix"
        proposed_second["marker"] = "new"
    untouched_scope = fixture.api.resolve_channel_management_scope(
        request(header="A"), KEY
    )
    entered, release = asyncio.Event(), asyncio.Event()
    original_stop = fixture.sessions._stop

    async def gated_stop(session):
        if session is first:
            entered.set()
            await release.wait()
        return await original_stop(session)

    monkeypatch.setattr(fixture.sessions, "_stop", gated_stop)
    operation = asyncio.create_task(
        fixture.client.put(
            "/config/channels",
            json={"wechat": proposed_wechat, second_key: proposed_second},
        )
    )
    fetched = None
    try:
        async with asyncio.timeout(5):
            await entered.wait()
        assert first.status == second.status == "cancelled"
        assert not first.credentials and not second.credentials
        assert (
            first.lease in fixture.rt.owners.leases()
            and second.lease in fixture.rt.owners.leases()
        )
        assert set(first_claims).issubset(fixture.rt.owners.leases())
        for target in (first, second):
            with pytest.raises(ManagementError) as busy:
                await fixture.sessions.fetch(target.scope, {})
            assert busy.value.reason_code == "MANAGEMENT_BUSY"
        current = load_agent_config("A")
        if phase == "disable":
            assert current.channels.wechat.enabled
            assert getattr(current.channels, second_key)["enabled"]
        else:
            assert (
                current.channels.wechat.bot_prefix
                == profile.channels.wechat.bot_prefix
            )
            assert getattr(current.channels, second_key)["marker"] == "old"
        # The real installed, untouched descriptor remains admitted and runs
        # its actual management worker while selected cleanup is still held.
        fetched = await fixture.sessions.fetch(untouched_scope, {})
        assert fetched.poll_token and len(local_management_http.requests) == 1
        assert local_management_http.requests[0]["workspace_id"] == "A"
        assert not operation.done() and b.status == "confirmed" and b.claims
        release.set()
        response = await operation
        assert response.status_code == 200, response.text
        assert not fixture.sessions._revoking
        assert (
            first.lease not in fixture.rt.owners.leases()
            and second.lease not in fixture.rt.owners.leases()
        )
        assert set(first_claims).isdisjoint(fixture.rt.owners.leases())
        assert b.lease in fixture.rt.owners.leases() and b.claims
        assert any(
            lease.key.name == KEY
            and lease.key.scope_id == str(fixture.paths["A"].resolve())
            for lease in fixture.rt.owners.leases()
        )
        assert fixture.reloads == ["A"]
    finally:
        release.set()
        await asyncio.gather(operation, return_exceptions=True)
        monkeypatch.setattr(fixture.sessions, "_stop", original_stop)
        if fetched is not None:
            await fixture.sessions.cancel(untouched_scope, fetched.poll_token)


@pytest.mark.asyncio
@pytest.mark.parametrize("bulk", [False, True])
async def test_manual_atomic_write_repeated_cancellation_keeps_guard_and_reload(
    manual_scope, monkeypatch, bulk
):
    from management_process_suite import eventually
    from qwenpaw.app.utils import schedule_agent_reload
    from qwenpaw.config.config import load_agent_config
    from wechat_qr_io_support import FileIOGate

    fixture = manual_scope
    a, b = fixture.seed("A"), fixture.seed("B")
    b_bytes = (fixture.paths["B"] / "agent.json").read_bytes()
    profile = load_agent_config("A")
    unrelated = dict(getattr(profile.channels, KEY))
    values = profile.channels.wechat.model_dump()
    values["bot_prefix"] = "persist despite repeated caller cancellation"
    reloads = []
    reload_agent = fixture.manager.reload_agent

    async def observed_reload(agent_id):
        reloads.append(agent_id)
        return await reload_agent(agent_id)

    monkeypatch.setattr(fixture.manager, "reload_agent", observed_reload)
    monkeypatch.setattr(
        "qwenpaw.app.routers.config.schedule_agent_reload",
        schedule_agent_reload,
    )
    config = import_module("qwenpaw.config.config")
    gate = FileIOGate(
        monkeypatch,
        config,
        "write_json_atomic",
        lambda path, *a, **kw: Path(path) == fixture.paths["A"] / "agent.json",
    )
    operation = asyncio.create_task(
        fixture.client.put(
            "/config/channels" if bulk else "/config/channels/wechat",
            json={"wechat": values} if bulk else values,
        )
    )
    try:
        await gate.wait(operation)
        # The request has captured its real Workspace. Keep the manager's
        # scheduled reload on its real unloaded-agent path, without native I/O.
        fixture.manager.agents.pop("A")
        operation.cancel()
        await asyncio.sleep(0)
        operation.cancel()
        await asyncio.sleep(0)
        assert not operation.done()
        assert fixture.sessions._revoking[("A", "wechat")] == 1
        assert a.lease not in fixture.rt.owners.leases() and not a.claims
        assert b.lease in fixture.rt.owners.leases() and b.claims
        assert reloads == [] and fixture.manager._config_generations == {}
        gate.release.set()
        with pytest.raises(asyncio.CancelledError):
            await operation
        persisted = load_agent_config("A")
        assert persisted.channels.wechat.bot_prefix == values["bot_prefix"]
        assert dict(getattr(persisted.channels, KEY)) == unrelated
        assert (fixture.paths["B"] / "agent.json").read_bytes() == b_bytes
        await eventually(lambda: reloads == ["A"])
        assert fixture.manager._config_generations == {"A": 1}
        assert not fixture.sessions._revoking and gate.calls == 1
    finally:
        gate.release.set()
        await asyncio.gather(operation, return_exceptions=True)
