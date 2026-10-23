# -*- coding: utf-8 -*-
# pylint: disable=protected-access,redefined-outer-name,unused-argument
"""C6 host ownership and passive boot contracts without vendor imports."""

import importlib.abc
from pathlib import Path
import sys
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from catalog_suite import candidate

pytest_plugins = ["catalog_suite"]

ROOT = Path(__file__).resolve().parents[3]


def test_c6_default_registry_never_attempts_legacy_platform_imports(registry):
    from qwenpaw.app.channels.registry import (
        clear_builtin_channel_cache,
        get_channel_registry,
    )

    attempts = []

    class RecordLegacyImports(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname.startswith(
                (
                    "qwenpaw.app.channels.feishu",
                    "qwenpaw.app.channels.dingtalk",
                ),
            ):
                attempts.append(fullname)
                raise ImportError("Legacy channel must never be resolved")

    guard = RecordLegacyImports()
    sys.meta_path.insert(0, guard)
    try:
        clear_builtin_channel_cache()
        channels = get_channel_registry()
    finally:
        sys.meta_path.remove(guard)
        clear_builtin_channel_cache()
    assert "console" in channels
    assert not attempts
    assert not {"feishu", "dingtalk"}.intersection(channels)


def test_c6_official_ownership_is_permanent_without_runtime(registry):
    from qwenpaw.app.channels.registry import (
        BUILTIN_CHANNEL_KEYS,
        selected_process_channels,
    )

    assert {"feishu", "dingtalk"} <= selected_process_channels()
    assert not {"feishu", "dingtalk"}.intersection(BUILTIN_CHANNEL_KEYS)
    assert "console" in BUILTIN_CHANNEL_KEYS


def c6_loader(tmp_path, workspaces=None):
    from qwenpaw.plugins.channel_adapter import ChannelRuntimeSettings
    from qwenpaw.plugins.loader import PluginLoader
    from qwenpaw.plugins.workspace_projector import WorkspaceProjector

    source, manifest = candidate(tmp_path / "installed")
    loader = PluginLoader(
        [source.parent],
        channel_runtime=ChannelRuntimeSettings(
            python_executable=sys.executable,
            support_root=ROOT / "src",
            qwenpaw_version="2.2.2b4",
            selected_channels=frozenset({"feishu", "dingtalk"}),
        ),
    )
    loader.registry.projector = WorkspaceProjector(
        live_workspaces=lambda: workspaces or [],
    )
    return loader, source, manifest


@pytest.mark.asyncio
@pytest.mark.parametrize("disabled_workspace", [False, True])
async def test_c6_passive_boot_arms_hooks_without_unused_sdk_probe(
    tmp_path,
    registry,
    monkeypatch,
    disabled_workspace,
):
    manager = NS(start_one=AsyncMock(return_value=NS()))
    workspace = NS(
        agent_id="c6-later",
        channel_manager=manager,
        _config=NS(channels=NS(feishu=NS(enabled=False))),
    )
    workspaces = [workspace] if disabled_workspace else []
    loader, source, manifest = c6_loader(tmp_path, workspaces)
    probes = []

    async def observe_probe(*args, **kwargs):
        probes.append((args, kwargs))

    monkeypatch.setattr(loader, "_prepare_channel_candidate", observe_probe)
    record = await loader.load_plugin(
        manifest,
        source,
        {"enabled": True},
        activate=False,
    )
    await loader.activate_all_loaded()
    assert not probes
    instance = loader.lifecycle.get_instance("feishu")
    assert instance.activated, "Later workspace enables still need armed hooks"
    assert record.status not in {"running", "validated"}
    assert loader.registry.get_channel_catalog_entry("feishu") is not None
    manager.start_one.assert_not_awaited()
    if not disabled_workspace:
        workspaces.append(workspace)
    workspace._config.channels.feishu.enabled = True
    await loader.registry.projector.project("channel", "feishu", "feishu")
    manager.start_one.assert_awaited_once()


@pytest.mark.asyncio
async def test_c6_explicit_validation_may_probe_without_workspace(
    tmp_path,
    registry,
    monkeypatch,
):
    loader, source, manifest = c6_loader(tmp_path)
    probes = []

    async def observe_probe(*args, **kwargs):
        probes.append((args, kwargs))

    monkeypatch.setattr(loader, "_prepare_channel_candidate", observe_probe)
    await loader.load_plugin(
        manifest,
        source,
        {"enabled": True},
        activate=True,
    )
    assert probes, "Explicit activation must validate before commit"


@pytest.mark.parametrize("key", ["feishu", "dingtalk"])
def test_c6_descriptor_fields_are_frontend_widgets(tmp_path, registry, key):
    from catalog_suite import describe

    source, manifest = candidate(tmp_path, key)
    row = registry.register_channel_descriptor(key, describe(source, manifest))
    fields = {field["name"]: field for field in row.config_fields}
    assert fields["streaming_enabled"]["type"] == "switch"
    secret = "app_secret" if key == "feishu" else "client_secret"
    assert fields[secret]["type"] == "password"
    assert fields["media_dir"]["type"] == "text"
    assert fields["media_dir"]["default"] is None


@pytest.mark.asyncio
async def test_c6_disabled_discovery_retains_static_catalog(
    tmp_path,
    registry,
    monkeypatch,
):
    loader, _source, _manifest = c6_loader(tmp_path)

    async def forbidden_probe(*args, **kwargs):
        raise AssertionError("Disabled discovery probed a vendor")

    monkeypatch.setattr(loader, "_prepare_channel_candidate", forbidden_probe)
    await loader.load_all_plugins(
        configs={"feishu": {"enabled": False}},
        types=["channel"],
        activate=False,
    )
    row = loader.registry.get_channel_catalog_entry("feishu")
    assert row is not None
    assert row.global_enabled is False
    assert loader.get_loaded_plugin("feishu").enabled is False


@pytest.mark.parametrize(
    "name,kind,default",
    [("access_tokens", "array", []), ("secret_map", "object", {})],
)
def test_c6_structured_secret_names_keep_json_widget_and_type(
    name,
    kind,
    default,
):
    from qwenpaw.app.channels.catalog import config_fields

    fields = config_fields(
        {
            "type": "object",
            "required": [name],
            "properties": {
                name: {"type": kind, "default": default},
                "api_token": {"type": "string"},
            },
        },
    )
    structured, scalar = fields
    assert structured["type"] == "json"
    assert structured["json_type"] == kind
    assert structured["default"] == default and structured["required"] is True
    assert scalar["type"] == "password"


@pytest.fixture
def passive_catalog(tmp_path, registry, monkeypatch):
    """Use real authenticated release resources and loop-owned runtime state."""
    import shutil

    from qwenpaw.config import utils as config_utils
    from qwenpaw.config.config import ChannelConfig
    from qwenpaw.plugins import official_channels

    resources = tmp_path / "resources"
    shutil.copytree(ROOT / "src/qwenpaw/official_channels", resources)
    monkeypatch.setattr(official_channels, "_resource_root", lambda: resources)
    settings = NS(plugins={})
    monkeypatch.setattr(config_utils, "load_config", lambda: settings)
    monkeypatch.setattr(
        config_utils,
        "get_plugins_dir",
        lambda: tmp_path / "installed",
    )
    agent = NS(config=NS(channels=ChannelConfig()), channel_manager=None)

    async def get_agent(_request):
        return agent

    monkeypatch.setattr(
        "qwenpaw.app.agent_context.get_agent_for_request",
        get_agent,
    )
    from starlette.requests import Request

    request = Request({"type": "http", "app": NS(state=NS())})
    return NS(
        resources=resources,
        settings=settings,
        request=request,
        agent=agent,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["list_channels", "list_channel_schemas"])
async def test_c6_catalog_request_reads_real_resources_off_loop(
    passive_catalog,
    monkeypatch,
    endpoint,
):
    import asyncio
    import threading

    from qwenpaw.app.routers import config as routes

    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    entered = asyncio.Event()
    release = threading.Event()
    original_open = Path.open
    observed = []

    def guarded_open(path, *args, **kwargs):
        stream = original_open(path, *args, **kwargs)
        if path.parent == passive_catalog.resources:
            observed.append(path.name)
            if threading.get_ident() == loop_thread:
                stream.close()
                raise AssertionError("Catalog resource file opened on loop")
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(5):
                stream.close()
                raise AssertionError("Loop did not release resource read")
        return stream

    monkeypatch.setattr(Path, "open", guarded_open)
    task = asyncio.create_task(
        getattr(routes, endpoint)(passive_catalog.request),
    )
    try:
        barrier = asyncio.create_task(entered.wait())
        done, _ = await asyncio.wait(
            {task, barrier},
            timeout=5,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if task in done:
            await task
        assert entered.is_set(), "No real resource read reached the barrier"
        loop.call_soon(release.set)
        result = await task
        assert "index.json" in observed and any(
            name.endswith(".zip") for name in observed
        )
        assert "feishu" in result and "azure_bot" in result
        warm = await getattr(routes, endpoint)(passive_catalog.request)
        assert "feishu" in warm and "azure_bot" in warm
    finally:
        release.set()
        barrier.cancel()
        await asyncio.gather(task, barrier, return_exceptions=True)


@pytest.mark.asyncio
async def test_c6_catalog_single_flight_settles_cancel_and_keeps_runtime_fresh(
    passive_catalog,
    tmp_path,
    registry,
    monkeypatch,
):
    import asyncio
    import threading

    from catalog_suite import describe
    from qwenpaw.app.channels.catalog import (
        get_configurable_channel_catalog_async,
    )
    from qwenpaw.plugins import archive, official_channels
    from qwenpaw.plugins.official_channel_catalog import OFFICIAL_CHANNELS

    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    entered = asyncio.Event()
    release = threading.Event()
    extracted = []
    original_extract = archive.safe_extract_zip
    original_catalog = registry.get_channel_catalog

    def extract(archive_file, target, *args, **kwargs):
        assert threading.get_ident() != loop_thread
        result = original_extract(archive_file, target, *args, **kwargs)
        extracted.append(target)
        if len(extracted) == 1:
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(5), "Loop did not release extraction"
        return result

    def runtime_catalog():
        assert threading.get_ident() == loop_thread
        return original_catalog()

    monkeypatch.setattr(archive, "safe_extract_zip", extract)
    monkeypatch.setattr(registry, "get_channel_catalog", runtime_catalog)
    first = asyncio.create_task(get_configurable_channel_catalog_async())
    second = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert extracted[0].is_dir()
        second = asyncio.create_task(get_configurable_channel_catalog_async())
        first.cancel()
        await asyncio.sleep(0)
        first.cancel()
        await asyncio.sleep(0)
        assert not first.done(), "Cancellation escaped active file work"
        assert extracted[0].is_dir(), "Cancelled reader removed active source"
        source, manifest = candidate(tmp_path / "runtime")
        row = registry.register_channel_descriptor(
            "feishu",
            describe(source, manifest),
            global_enabled=False,
        )
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        cold = await second
    finally:
        release.set()
        await asyncio.gather(
            *(task for task in (first, second) if task is not None),
            return_exceptions=True,
        )
    assert len(extracted) == len(OFFICIAL_CHANNELS)
    assert all(not path.exists() for path in extracted)
    assert cold["feishu"]["availability"] == "disabled"
    cold["telegram"]["config_schema"]["properties"].clear()
    row.global_enabled = True
    row.runtime_settings = NS()
    passive_catalog.settings.plugins["slack"] = {"enabled": False}
    warm = await get_configurable_channel_catalog_async(["saved_unknown"])
    assert len(extracted) == len(OFFICIAL_CHANNELS), "Warm cache re-extracted"
    assert warm["telegram"]["config_schema"]["properties"]
    assert warm["feishu"]["availability"] == "validation_pending"
    assert warm["slack"]["availability"] == "missing_code"
    assert warm["slack"]["global_enabled"] is False
    assert warm["saved_unknown"]["availability"] == "missing_code"
    metadata = official_channels.bundled_channel_description("telegram")
    assert not any(
        hasattr(metadata, name)
        for name in (
            "descriptor_path",
            "requirements_path",
            "code_root",
            "create_proxy",
        )
    ), "Passive cache retained executable or temporary source metadata"
    assert not hasattr(metadata.descriptor, "entrypoint")

    from qwenpaw.app.routers.config import list_channel_schemas

    health = {
        "state": "ready",
        "platform_state": "connected",
        "last_error": "",
    }

    def health_snapshot():
        assert threading.get_ident() == loop_thread
        return dict(health)

    async def get_channel(key):
        if key == "feishu":
            return NS(channel_process=NS(health_snapshot=health_snapshot))
        return None

    passive_catalog.agent.channel_manager = NS(get_channel=get_channel)
    running = await list_channel_schemas(passive_catalog.request)
    assert running["feishu"]["availability"] == "running"
    assert "repair" not in running["feishu"]
    health.update(state="failed", platform_state="failed", last_error="lost")
    failed = await list_channel_schemas(passive_catalog.request)
    assert failed["feishu"]["availability"] == "failed"
    assert failed["feishu"]["diagnostic"] == "lost"
    assert failed["feishu"]["repair"]["action"] == "repair"


@pytest.mark.asyncio
async def test_c6_catalog_cache_reauthenticates_changed_resources(
    passive_catalog,
    monkeypatch,
):
    import hashlib
    import io
    import json
    import shutil
    import zipfile

    from qwenpaw.app.channels.catalog import (
        get_configurable_channel_catalog_async,
    )
    from qwenpaw.plugins import official_channels

    initial = await get_configurable_channel_catalog_async()
    assert initial["feishu"]["config_schema"]
    root = passive_catalog.resources
    index_path = root / "index.json"
    index = json.loads(index_path.read_text())
    entry = next(
        row for row in index["plugins"] if row["plugin_id"] == "feishu"
    )
    archive_path = root / entry["filename"]
    original = archive_path.read_bytes()
    archive_path.write_bytes(original + b"tampered")
    damaged = await get_configurable_channel_catalog_async()
    assert "config_schema" not in damaged["feishu"]
    assert "digest" in damaged["feishu"]["diagnostic"].lower()
    archive_path.write_bytes(original)
    restored = await get_configurable_channel_catalog_async()
    assert restored["feishu"]["config_schema"]

    replacement = root.with_name("replacement-resources")
    shutil.copytree(root, replacement)
    payload = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(original)) as before:
        with zipfile.ZipFile(payload, "w") as after:
            for name in before.namelist():
                data = before.read(name)
                if name == "feishu/channel.json":
                    descriptor = json.loads(data)
                    descriptor["label"]["en"] = "Replacement Feishu"
                    data = json.dumps(descriptor).encode()
                    entry["descriptor_sha256"] = hashlib.sha256(
                        data
                    ).hexdigest()
                after.writestr(name, data)
    data = payload.getvalue()
    (replacement / entry["filename"]).write_bytes(data)
    entry["sha256"] = hashlib.sha256(data).hexdigest()
    (replacement / "index.json").write_text(json.dumps(index))
    monkeypatch.setattr(
        official_channels, "_resource_root", lambda: replacement
    )
    changed = await get_configurable_channel_catalog_async()
    assert changed["feishu"]["label"] == "Replacement Feishu"
    index["host_version"] = "0.0.0"
    (replacement / "index.json").write_text(json.dumps(index))
    rejected = await get_configurable_channel_catalog_async()
    assert "config_schema" not in rejected["feishu"]
    assert "identity" in rejected["feishu"]["diagnostic"].lower()
