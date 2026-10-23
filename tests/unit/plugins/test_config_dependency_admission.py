# -*- coding: utf-8 -*-
"""Config rebuild admission must preserve the current runtime until admitted."""

# pylint: disable=protected-access
import asyncio
import importlib
import sys
import threading
from pathlib import Path

import pytest

from qwenpaw.plugins.lifecycle import PluginState, UnloadMode
from qwenpaw.plugins.settings import persist_plugin_settings

from .test_plugin_config_update import _load_with_workspace
from .test_reload_dependency_admission import declaration_tokens, distribution


BODY = (
    "self.calls = getattr(self, 'calls', []) + [api.config['cmd']]\n"
    "        api.register_slash_command(api.config['cmd'], lambda c, a: None)\n"
    "        if api.config.get('fail') == 'register':\n"
    "            raise RuntimeError('candidate register failed')\n"
    "        def start():\n"
    "            if api.config.get('fail') == 'activate':\n"
    "                raise RuntimeError('candidate activation failed')\n"
    "        api.register_startup_hook('start', start)"
)


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setattr("qwenpaw.constant.WORKING_DIR", tmp_path / "work")
    monkeypatch.setattr("qwenpaw.config.utils.get_config_path", lambda: path)
    persist_plugin_settings("config-admission", config={"cmd": "old"})
    return path


@pytest.mark.parametrize("conflict_at", ["candidate", "rollback"])
async def test_config_host_dependency_refusal_preserves_exact_runtime(
    tmp_path,
    fresh_registry,
    monkeypatch,
    config_file,
    conflict_at,
):
    usage = fresh_registry.dependency_usage
    tokens = declaration_tokens(monkeypatch, usage)
    loader, workspace = await _load_with_workspace(
        tmp_path,
        fresh_registry,
        "config-admission",
        BODY,
        {"cmd": "old"},
    )
    old_tokens = tuple(
        token for token in tokens if usage.has_declaration(token)
    )
    assert len(old_tokens) == 1
    tokens.clear()
    record = loader.get_loaded_plugin("config-admission")
    instance = loader.lifecycle.get_instance("config-admission")
    definition = record.instance
    ledger = tuple(instance._runtime)
    declarations = tuple(usage.snapshot().uses)
    disk_before = config_file.read_bytes()
    site = tmp_path / "host-site"
    distribution(site, "config-admission-peer", "1.0")
    distribution(
        site,
        "config-admission-owner",
        "1.0",
        requires=("config-admission-peer>=2",),
    )
    monkeypatch.syspath_prepend(str(site))
    teardown_calls = []
    original_teardown = loader.teardown_config_runtime
    original_admit = loader._admit_dependencies
    admit_calls = 0

    async def teardown(*args, **kwargs):
        teardown_calls.append(args)
        return await original_teardown(*args, **kwargs)

    async def admit(*args, **kwargs):
        nonlocal admit_calls
        admit_calls += 1
        result = await original_admit(*args, **kwargs)
        if conflict_at == "rollback" and admit_calls == 1:
            importlib.import_module("config_admission_owner")
        return result

    monkeypatch.setattr(loader, "teardown_config_runtime", teardown)
    monkeypatch.setattr(loader, "_admit_dependencies", admit)
    if conflict_at == "candidate":
        importlib.import_module("config_admission_owner")
    try:
        report = await loader.lifecycle.update_config(
            "config-admission",
            {"cmd": "new"},
        )
        assert not report.ok, report.errors
        assert report.unchanged, report.errors
        assert any("config-admission-peer" in error for error in report.errors)
        assert not teardown_calls
        assert loader.get_loaded_plugin("config-admission") is record
        assert loader.lifecycle.get_instance("config-admission") is instance
        assert record.instance is definition
        assert (
            record.status == "active" and instance.state is PluginState.ACTIVE
        )
        assert instance.activated and instance.config == {"cmd": "old"}
        assert tuple(instance._runtime) == ledger
        assert definition.calls == ["old"]
        assert workspace.plugins.slash_command_registry.names() == ["old"]
        assert config_file.read_bytes() == disk_before
        assert len(tokens) == (1 if conflict_at == "candidate" else 2)
        assert not any(usage.has_declaration(token) for token in tokens)
        assert all(usage.has_declaration(token) for token in old_tokens)
        assert tuple(
            row for row in usage.snapshot().uses if row.origin == "declaration"
        ) == tuple(row for row in declarations if row.origin == "declaration")
    finally:
        sys.modules.pop("config_admission_owner", None)
        await loader.lifecycle.unload("config-admission", UnloadMode.UNLOAD)


@pytest.mark.parametrize("failure", [None, "register", "activate"])
async def test_config_admission_transfers_exact_candidate_or_recovery_token(
    tmp_path,
    fresh_registry,
    monkeypatch,
    config_file,
    failure,
):
    usage = fresh_registry.dependency_usage
    tokens = declaration_tokens(monkeypatch, usage)
    loader, workspace = await _load_with_workspace(
        tmp_path,
        fresh_registry,
        "config-admission",
        BODY,
        {"cmd": "old"},
    )
    (old_token,) = (token for token in tokens if usage.has_declaration(token))
    tokens.clear()
    instance = loader.lifecycle.get_instance("config-admission")
    record = loader.get_loaded_plugin("config-admission")
    definition = record.instance
    disk_before = config_file.read_bytes()
    incoming = {"cmd": "new", "fail": failure}
    try:
        report = await loader.lifecycle.update_config(
            "config-admission",
            incoming,
        )
        assert report.ok is (failure is None), report.errors
        assert not report.needs_restart, report.errors
        if failure:
            assert any("candidate" in error for error in report.errors)
            assert config_file.read_bytes() == disk_before
        assert loader.lifecycle.get_instance("config-admission") is instance
        assert loader.get_loaded_plugin("config-admission") is record
        assert record.instance is definition
        assert record.status == "active" and instance.activated
        assert instance.state is PluginState.ACTIVE
        assert instance.config == ({"cmd": "old"} if failure else incoming)
        assert workspace.plugins.slash_command_registry.names() == [
            "old" if failure else "new",
        ]
        assert definition.calls == (
            ["old", "new", "old"] if failure else ["old", "new"]
        )
        assert len(tokens) == 2
        candidate, recovery = tokens
        assert candidate is not recovery and old_token not in tokens
        assert not usage.has_declaration(old_token)
        assert usage.has_declaration(candidate) is (failure is None)
        assert usage.has_declaration(recovery) is (failure is not None)
        assert (
            len(
                [
                    row
                    for row in usage.snapshot().uses
                    if row.origin == "declaration"
                ],
            )
            == 1
        )
    finally:
        unloaded = await loader.lifecycle.unload(
            "config-admission",
            UnloadMode.UNLOAD,
        )
        assert unloaded.clean and unloaded.quiescent
    assert not any(
        usage.has_declaration(token) for token in (old_token, *tokens)
    )


async def test_cancel_config_preflight_settles_real_scanner_before_unlocking(
    tmp_path,
    fresh_registry,
    monkeypatch,
    config_file,
):
    usage = fresh_registry.dependency_usage
    tokens = declaration_tokens(monkeypatch, usage)
    loader, workspace = await _load_with_workspace(
        tmp_path,
        fresh_registry,
        "config-admission",
        BODY,
        {"cmd": "old"},
    )
    old_tokens = tuple(
        token for token in tokens if usage.has_declaration(token)
    )
    tokens.clear()
    instance = loader.lifecycle.get_instance("config-admission")
    record = loader.get_loaded_plugin("config-admission")
    ledger = tuple(instance._runtime)
    disk_before = config_file.read_bytes()
    site = tmp_path / "host-site"
    info = distribution(site, "config-cancellation-owner", "1.0")
    monkeypatch.syspath_prepend(str(site))
    importlib.import_module("config_cancellation_owner")
    entered, acquired = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original_open = Path.open

    def slow_record(path, *args, **kwargs):
        if path == info / "RECORD":
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(5), "config preflight scanner was not released"
        return original_open(path, *args, **kwargs)

    async def competing_lifecycle():
        async with loader.plugin_lifecycle("config-admission"):
            acquired.set()

    monkeypatch.setattr(Path, "open", slow_record)
    updating = asyncio.create_task(
        loader.lifecycle.update_config(
            "config-admission",
            {"cmd": "new"},
        ),
    )
    competing = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert tuple(instance._runtime) == ledger
        assert workspace.plugins.slash_command_registry.names() == ["old"]
        competing = asyncio.create_task(competing_lifecycle())
        for _ in range(3):
            updating.cancel()
            await asyncio.sleep(0)
            assert not updating.done()
            assert not acquired.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await updating
        await asyncio.wait_for(competing, 5)
        assert loader.lifecycle.get_instance("config-admission") is instance
        assert loader.get_loaded_plugin("config-admission") is record
        assert instance.state is PluginState.ACTIVE and instance.activated
        assert instance.config == {"cmd": "old"}
        assert tuple(instance._runtime) == ledger
        assert record.instance.calls == ["old"]
        assert workspace.plugins.slash_command_registry.names() == ["old"]
        assert config_file.read_bytes() == disk_before
        assert tokens and not any(
            usage.has_declaration(token) for token in tokens
        )
        assert all(usage.has_declaration(token) for token in old_tokens)
    finally:
        release.set()
        await asyncio.gather(
            *(task for task in (updating, competing) if task is not None),
            return_exceptions=True,
        )
        sys.modules.pop("config_cancellation_owner", None)
        await loader.lifecycle.unload("config-admission", UnloadMode.UNLOAD)


async def test_inactive_config_rejection_does_not_create_enabled_admission(
    tmp_path,
    fresh_registry,
    monkeypatch,
    config_file,
):
    usage = fresh_registry.dependency_usage
    tokens = declaration_tokens(monkeypatch, usage)
    loader, workspace = await _load_with_workspace(
        tmp_path,
        fresh_registry,
        "config-admission",
        BODY,
        {"cmd": "old", "enabled": False},
    )
    record = loader.get_loaded_plugin("config-admission")
    instance = loader.lifecycle.get_instance("config-admission")
    disk_before = config_file.read_bytes()
    assert record.instance is None and not record.enabled
    assert not tokens
    try:
        report = await loader.lifecycle.update_config(
            "config-admission",
            {"cmd": "new"},
        )
        assert not report.ok and report.unchanged, report.errors
        assert loader.get_loaded_plugin("config-admission") is record
        assert loader.lifecycle.get_instance("config-admission") is instance
        assert instance.config == {"cmd": "old"}
        assert not tokens
        assert not workspace.plugins.slash_command_registry.names()
        assert config_file.read_bytes() == disk_before
    finally:
        await loader.lifecycle.unload("config-admission", UnloadMode.UNLOAD)
