"""R1 actual authorized HTTP save with real static WeChat metadata custody.

Only future catalog inventory is unit-local: the legacy builtin reservation is
removed. The repository's real WeChat payload is copied intact and registered
through the real Loader without activation/probe/SDK import. Confirmed Session
state uses production objects and exact owner/claims, as accepted W2 units do.
This is not formal WeChat release or native QR/Agent integration evidence.
"""

import asyncio
from hashlib import sha256
from importlib import import_module
import json
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace

from fastapi import FastAPI
import httpx
import pytest
import pytest_asyncio

from wechat_w2_config_support import write_profiles
from wechat_w2_scope_suite import request as scope_request
from wechat_w2_session_suite import narrow_legacy_test_guard


@pytest_asyncio.fixture
async def protected_save(tmp_path, monkeypatch, request):
    from qwenpaw.app.channels import registry as channel_registry
    from qwenpaw.app.channels.management import ChannelManagementService
    from qwenpaw.app.channels.wechat_auth_custody import reserve_wechat_auth
    from qwenpaw.app.multi_agent_manager import MultiAgentManager
    from qwenpaw.channel_protocol.artifacts import code_root_digest
    from qwenpaw.config.config import load_agent_config, save_agent_config
    from qwenpaw.plugins.architecture import PluginManifest
    from qwenpaw.plugins.channel_adapter import ChannelRuntimeSettings
    from qwenpaw.plugins.loader import PluginLoader
    from qwenpaw.plugins.ownership import RuntimeKey
    from qwenpaw.plugins.registry import PluginRegistry

    root = Path(__file__).resolve().parents[3]
    monkeypatch.setattr(PluginRegistry, "_instance", None)
    monkeypatch.setattr(
        channel_registry,
        "BUILTIN_CHANNEL_KEYS",
        channel_registry.BUILTIN_CHANNEL_KEYS - {"wechat"},
    )
    installed = tmp_path / "installed" / "wechat"
    shutil.copytree(
        root / "plugins/channel/wechat",
        installed,
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    manifest = PluginManifest.from_dict(
        json.loads((installed / "plugin.json").read_text())
    )
    loader = PluginLoader(
        [installed.parent],
        channel_runtime=ChannelRuntimeSettings(
            python_executable=sys.executable,
            support_root=root / "src",
            qwenpaw_version="2.2.2b4",
            selected_channels=frozenset({"wechat"}),
        ),
    )
    record = await loader.load_plugin(
        manifest, installed, {"enabled": True}, activate=False
    )
    assert record.enabled, record.diagnostics
    row = loader.registry.get_channel_catalog_entry("wechat")
    assert row.description_record is record.backend_binding.description.payload
    assert (
        code_root_digest(installed) == row.description_record.source_revision
    )
    _, paths = write_profiles(tmp_path, monkeypatch, "wechat", {})
    protected_path = str(tmp_path / "A" / "private-token")
    old_url = "https://custom.invalid"
    profile = load_agent_config("A")
    profile.channels.wechat.bot_token_file = protected_path
    profile.channels.wechat.base_url = old_url
    save_agent_config("A", profile)
    # Exercise the existing legacy-field migration before acceptance gates;
    # a failed write's later cold read must start from current on-disk schema.
    from qwenpaw.config.utils import _agent_config_cache

    _agent_config_cache.pop("A", None)
    load_agent_config("A")
    api = import_module("qwenpaw.app.channels.qrcode_sessions")
    service = ChannelManagementService(loader)
    sessions = api.ChannelQRCodeSessions(loader, service)
    scope = api.resolve_channel_management_scope(
        scope_request(header="A"), "wechat"
    )
    owners = loader.registry.runtime_owners
    lease = owners.reserve(
        "wechat",
        RuntimeKey("qrcode_session", "wechat", str(paths["A"].resolve())),
        installed,
        instance_id="r1-confirmed-custody",
    )
    session = api._Session(
        scope,
        lease,
        row.description_record.source_revision,
        {},
        api.monotonic() + 300,
    )
    session.status = "confirmed"
    session.credentials = {
        "bot_token": "confirmed-R1-token",
        "base_url": getattr(request, "param", ""),
    }
    owners.attach(lease, lambda: sessions._stop(session), resource=session)
    session.claims = reserve_wechat_auth(
        loader.registry,
        owner=lease,
        workspace_id="A",
        workspace_dir=scope.workspace_dir,
        config=sessions._auth_config(
            session, session.credentials, working_dir=sessions._working_dir
        ),
    )
    token = "r1-exact-confirmed-session-token"
    sessions._sessions[sha256(token.encode()).hexdigest()] = session
    manager = MultiAgentManager()
    manager.channel_qrcode_sessions = sessions
    app = FastAPI()
    app.include_router(import_module("qwenpaw.app.routers.config").router)
    app.state.plugin_loader = loader
    app.state.channel_management = service
    app.state.channel_qrcode_sessions = sessions
    app.state.multi_agent_manager = manager
    try:
        assert session.claims and not manager.agents
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            yield SimpleNamespace(
                client=client,
                app=app,
                loader=loader,
                service=service,
                sessions=sessions,
                session=session,
                owners=owners,
                manager=manager,
                paths=paths,
                token=token,
                protected_path=protected_path,
                old_url=old_url,
                scope=scope,
            )
    finally:
        await asyncio.sleep(0)  # Settle the actual unloaded-manager reload.
        await sessions.aclose()
        await service.aclose()
        if loader.get_loaded_plugin("wechat") is not None:
            await loader.unload_plugin("wechat")


def auth_headers(fixture, token=None):
    return {
        "X-Agent-Id": "A",
        "X-Channel-Auth-Session": fixture.token if token is None else token,
    }


@pytest.mark.asyncio
async def test_partial_authorized_save_preserves_bound_auth_path_and_custom_url(
    protected_save,
):
    from qwenpaw.config.config import load_agent_config

    fixture = protected_save
    response = await fixture.client.put(
        "/config/channels/wechat",
        json={"enabled": True, "bot_token": "confirmed-R1-token"},
        headers=auth_headers(fixture),
    )
    assert response.status_code == 200, response.text
    persisted = load_agent_config("A").channels.wechat
    assert persisted.bot_token == "confirmed-R1-token"
    assert (persisted.bot_token_file, persisted.base_url) == (
        fixture.protected_path,
        fixture.old_url,
    ), "authorized partial body cleared bound authentication path or custom URL during final normalization/persistence"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "protected_save,mode,expected",
    [
        ("", "full", 200),
        ("", "path-mismatch", 403),
        ("", "explicit-empty-url", 403),
        ("https://platform.invalid", "url-match", 200),
        ("https://platform.invalid", "url-mismatch", 403),
        ("", "wrong-session-token", 400),
    ],
    indirect=["protected_save"],
)
async def test_final_protected_candidate_authorized_exactly_and_rejected_without_writes(
    protected_save, mode, expected
):
    from qwenpaw.config.config import load_agent_config

    fixture = protected_save
    values = {
        **fixture.scope.config,
        "enabled": True,
        "bot_token": "confirmed-R1-token",
    }
    if mode == "path-mismatch":
        values["bot_token_file"] = fixture.protected_path + "-different"
    elif mode == "explicit-empty-url":
        values["base_url"] = ""
    elif mode == "url-match":
        values["base_url"] = "https://platform.invalid"
    elif mode == "url-mismatch":
        values["base_url"] = "https://wrong.invalid"
    before = (fixture.paths["A"] / "agent.json").read_bytes()
    claims, leases = fixture.session.claims, fixture.owners.leases()
    response = await fixture.client.put(
        "/config/channels/wechat",
        json=values,
        headers=auth_headers(
            fixture,
            "unknown-session" if mode == "wrong-session-token" else None,
        ),
    )
    assert response.status_code == expected, response.text
    if expected == 200:
        saved = load_agent_config("A").channels.wechat
        assert saved.bot_token_file == fixture.protected_path
        assert saved.base_url == (
            "https://platform.invalid"
            if mode == "url-match"
            else fixture.old_url
        )
        assert saved.bot_token == "confirmed-R1-token"
        assert (
            fixture.session.status == "consumed" and not fixture.session.claims
        )
        assert set(claims).isdisjoint(fixture.owners.leases())
    else:
        assert (fixture.paths["A"] / "agent.json").read_bytes() == before
        assert (
            fixture.session.status == "confirmed"
            and fixture.session.credentials
        )
        assert (
            fixture.owners.leases() == leases
            and fixture.session.claims == claims
        )
        assert fixture.manager._config_generations == {}


@pytest.mark.asyncio
async def test_actual_persistence_error_does_not_consume_protected_authorization(
    protected_save, monkeypatch
):
    config = import_module("qwenpaw.config.config")
    fixture = protected_save
    profile_path = fixture.paths["A"] / "agent.json"
    before = profile_path.read_bytes()
    claims = fixture.session.claims
    atomic_write = config.write_json_atomic

    def fail_exact_file(path, payload, *args, **kwargs):
        if Path(path) == profile_path:
            raise OSError("isolated protected-save disk failure")
        return atomic_write(path, payload, *args, **kwargs)

    values = {"enabled": True, "bot_token": "confirmed-R1-token"}
    with monkeypatch.context() as disk:
        disk.setattr(config, "write_json_atomic", fail_exact_file)
        with pytest.raises(
            OSError, match="isolated protected-save disk failure"
        ):
            await fixture.client.put(
                "/config/channels/wechat",
                json=values,
                headers=auth_headers(fixture),
            )
    assert profile_path.read_bytes() == before
    assert (
        fixture.session.status == "confirmed" and fixture.session.credentials
    )
    assert (
        set(claims).issubset(fixture.owners.leases())
        and fixture.session.claims == claims
    )
    assert fixture.manager._config_generations == {}
    retry = await fixture.client.put(
        "/config/channels/wechat", json=values, headers=auth_headers(fixture)
    )
    assert retry.status_code == 200, retry.text
    saved = config.load_agent_config("A").channels.wechat
    assert (saved.bot_token_file, saved.base_url) == (
        fixture.protected_path,
        fixture.old_url,
    )
    assert fixture.session.status == "consumed" and not fixture.session.claims


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "end",
    ["cancel", "expiry", "revoke", "claim", "unload", "caller", "duplicate"],
)
async def test_actual_atomic_commit_keeps_claims_until_disk_settles(
    protected_save, monkeypatch, end
):
    from wechat_qr_io_support import FileIOGate
    from management_process_suite import eventually

    fixture = protected_save
    config = import_module("qwenpaw.config.config")
    path = fixture.paths["A"] / "agent.json"
    before = path.read_bytes()
    claims = fixture.session.claims
    lease = fixture.session.lease
    gate = FileIOGate(
        monkeypatch,
        config,
        "write_json_atomic",
        lambda target, *a, **kw: Path(target) == path,
    )
    values = {"enabled": True, "bot_token": "confirmed-R1-token"}
    save = asyncio.create_task(
        fixture.client.put(
            "/config/channels/wechat",
            json=values,
            headers=auth_headers(fixture),
        )
    )
    stop = None
    try:
        await gate.wait(save)
        assert path.read_bytes() == before
        assert (
            fixture.session.commit is not None
            and not fixture.session.commit.done()
        )
        if end == "caller":
            save.cancel()
            await asyncio.sleep(0)
            save.cancel()
        elif end == "duplicate":
            stop = asyncio.create_task(
                fixture.client.put(
                    "/config/channels/wechat",
                    json=values,
                    headers=auth_headers(fixture),
                )
            )
        elif end == "cancel":
            stop = asyncio.create_task(
                fixture.sessions.cancel(fixture.scope, fixture.token)
            )
        elif end == "revoke":
            stop = asyncio.create_task(
                fixture.sessions.revoke_channel("A", "wechat")
            )
        elif end == "claim":
            stop = asyncio.create_task(fixture.owners.stop(claims[0]))
        elif end == "unload":
            stop = asyncio.create_task(
                fixture.loader.lifecycle.set_enabled("wechat", False)
            )
        else:
            fixture.session.expires = (
                import_module(
                    "qwenpaw.app.channels.qrcode_sessions"
                ).monotonic()
                - 1
            )
            fixture.sessions._arm_expiry(fixture.session)
            await eventually(lambda: fixture.session.cleanup is not None)
            stop = fixture.session.cleanup
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not save.done()
        if stop:
            assert (
                not stop.done()
            ), "stop returned proof while atomic write remained pending"
        assert lease in fixture.owners.leases()
        assert set(claims).issubset(fixture.owners.leases())
        assert fixture.manager._config_generations == {}
        gate.release.set()
        if end == "caller":
            with pytest.raises(asyncio.CancelledError):
                await save
        else:
            response = await save
            assert response.status_code == 200, response.text
        if stop:
            stopped = await stop
            if end == "duplicate":
                assert stopped.status_code in {400, 409}, stopped.text
            elif end == "claim":
                assert stopped.stopped
        await eventually(
            lambda: fixture.manager._config_generations == {"A": 1}
        )
        saved = config.load_agent_config("A").channels.wechat
        assert saved.bot_token == "confirmed-R1-token"
        assert saved.bot_token_file == fixture.protected_path
        assert fixture.session.status == "consumed"
        assert (
            lease not in fixture.owners.leases() and not fixture.session.claims
        )
        assert set(claims).isdisjoint(fixture.owners.leases())
        assert gate.calls == 1
        again = await fixture.client.put(
            "/config/channels/wechat",
            json=values,
            headers=auth_headers(fixture),
        )
        assert again.status_code == 400 and gate.calls == 1
    finally:
        gate.release.set()
        await asyncio.gather(
            save, *([stop] if stop else []), return_exceptions=True
        )


@pytest.mark.asyncio
async def test_wechat_claim_evidence_resolves_files_off_loop_and_retries_new_owner(
    protected_save, monkeypatch, tmp_path
):
    from qwenpaw.app.channels.wechat_auth_custody import (
        reserve_wechat_auth_async,
    )
    from qwenpaw.plugins.ownership import RuntimeKey, RuntimeStopReceipt
    from wechat_qr_io_support import FileIOGate

    import threading

    fixture = protected_save
    path = Path(fixture.protected_path)
    gate = FileIOGate(
        monkeypatch, Path, "resolve", lambda target, *a, **kw: target == path
    )
    leases = fixture.owners.leases

    def loop_leases():
        assert threading.get_ident() == gate.loop_thread
        return leases()

    monkeypatch.setattr(fixture.owners, "leases", loop_leases)
    task = asyncio.create_task(
        reserve_wechat_auth_async(
            fixture.loader.registry,
            owner=fixture.session.lease,
            workspace_id="A",
            workspace_dir=fixture.scope.workspace_dir,
            config=fixture.sessions._auth_config(
                fixture.session,
                fixture.session.credentials,
                working_dir=fixture.sessions._working_dir,
            ),
            working_dir=fixture.sessions._working_dir,
        )
    )
    new_owner = None
    try:
        await gate.wait(task)
        new_owner = fixture.owners.reserve(
            "wechat",
            RuntimeKey("channel", "wechat", str(tmp_path / "new-runtime")),
            fixture.session.lease.code_root,
            instance_id="new-runtime-during-evidence",
        )
        gate.release.set()
        claims = await task
        assert set(claims) == set(fixture.session.claims)
        assert (
            gate.calls >= 2
        ), "changed runtime custody reused stale file evidence"
        assert fixture.owners.resource(new_owner) is None
    finally:
        gate.release.set()
        await asyncio.gather(task, return_exceptions=True)
        if new_owner:
            fixture.owners.release(
                new_owner, RuntimeStopReceipt(True, "NOT_STARTED")
            )


@pytest.mark.asyncio
async def test_actual_disk_failure_during_revoke_never_restores_authority(
    protected_save, monkeypatch
):
    from wechat_qr_io_support import FileIOGate

    fixture = protected_save
    config = import_module("qwenpaw.config.config")
    path = fixture.paths["A"] / "agent.json"
    before = path.read_bytes()
    claims = fixture.session.claims
    atomic = config.write_json_atomic

    def fail(target, payload, *args, **kwargs):
        if Path(target) == path:
            raise OSError("revoked actual atomic write failed")
        return atomic(target, payload, *args, **kwargs)

    monkeypatch.setattr(config, "write_json_atomic", fail)
    gate = FileIOGate(
        monkeypatch,
        config,
        "write_json_atomic",
        lambda target, *a, **kw: Path(target) == path,
    )
    values = {"enabled": True, "bot_token": "confirmed-R1-token"}
    save = asyncio.create_task(
        fixture.client.put(
            "/config/channels/wechat",
            json=values,
            headers=auth_headers(fixture),
        )
    )
    revoke = None
    try:
        await gate.wait(save)
        revoke = asyncio.create_task(
            fixture.sessions.revoke_channel("A", "wechat")
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not revoke.done() and set(claims).issubset(
            fixture.owners.leases()
        )
        gate.release.set()
        with pytest.raises(
            OSError, match="revoked actual atomic write failed"
        ):
            await save
        await revoke
        assert path.read_bytes() == before
        assert (
            fixture.session.status == "cancelled"
            and not fixture.session.claims
        )
        assert fixture.session.lease not in fixture.owners.leases()
        assert fixture.manager._config_generations == {}
        retry = await fixture.client.put(
            "/config/channels/wechat",
            json=values,
            headers=auth_headers(fixture),
        )
        assert retry.status_code == 400 and gate.calls == 1
    finally:
        gate.release.set()
        await asyncio.gather(
            save, *([revoke] if revoke else []), return_exceptions=True
        )


@pytest.mark.asyncio
async def test_wechat_evidence_retries_published_peer_change_with_stable_owners(
    protected_save, monkeypatch
):
    from qwenpaw.app.channels.management import ManagementError
    from qwenpaw.app.channels.wechat_auth_custody import (
        reserve_wechat_auth_async,
    )
    from qwenpaw.config.config import load_agent_config, save_agent_config
    from wechat_qr_io_support import FileIOGate

    fixture = protected_save
    claims = fixture.session.claims
    leases = fixture.owners.leases()
    # Ordered profiles are A, B, default: B's old evidence is detached when
    # canonicalizing default's runtime path, outside the config reader lock.
    last_workspace = fixture.paths["default"]
    gate = FileIOGate(
        monkeypatch,
        Path,
        "resolve",
        lambda target, *a, **kw: target == last_workspace,
    )
    task = asyncio.create_task(
        reserve_wechat_auth_async(
            fixture.loader.registry,
            owner=fixture.session.lease,
            workspace_id="A",
            workspace_dir=fixture.scope.workspace_dir,
            config=fixture.sessions._auth_config(
                fixture.session,
                fixture.session.credentials,
                working_dir=fixture.sessions._working_dir,
            ),
            working_dir=fixture.sessions._working_dir,
        )
    )
    try:
        await gate.wait(task)
        peer = load_agent_config("B")
        peer.channels.wechat.bot_token_file = fixture.protected_path
        assert not peer.channels.wechat.enabled
        save_agent_config("B", peer)
        assert fixture.owners.leases() == leases
        gate.release.set()
        with pytest.raises(ManagementError) as collision:
            await task
        assert collision.value.reason_code == "ACCOUNT_IN_USE"
        assert gate.calls >= 2, "published peer config reused stale evidence"
        assert (
            fixture.session.claims == claims
            and fixture.owners.leases() == leases
        )
    finally:
        gate.release.set()
        await asyncio.gather(task, return_exceptions=True)
