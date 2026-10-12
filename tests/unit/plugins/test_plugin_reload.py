# -*- coding: utf-8 -*-
# pylint: disable=protected-access,redefined-outer-name
"""Probe import, reload rollback, and provision migration."""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from qwenpaw.plugins.architecture import PluginManifest
from qwenpaw.plugins.lifecycle import PluginInstance, UnloadMode
from qwenpaw.plugins.loader import PluginLoader
from qwenpaw.plugins.module_isolation import (
    finish_probe_import,
    get_namespace_finder,
    plugin_module_name,
    probe_plugin_source,
)
from qwenpaw.plugins.provision import (
    apply_tool_factory,
    commit_migrations,
    load_inventory,
    provision_files,
    recover_migrating_inventory,
)
from qwenpaw.plugins.updates import (
    marker_path,
    recover_interrupted_updates,
    update_marker_status,
    write_updating_marker,
)
from qwenpaw.plugins.workspace_projector import WorkspaceProjector
from qwenpaw.runtime.slash_command_registry import SlashCommandRegistry


def _write_plugin(
    root: Path,
    plugin_id: str,
    *,
    body: str = "pass",
    extra_py: str | None = None,
    version: str = "1.0.0",
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "plugin.json").write_text(
        json.dumps(
            {
                "id": plugin_id,
                "version": version,
                "name": plugin_id,
                "entry": {"backend": "main.py"},
            },
        ),
        encoding="utf-8",
    )
    (root / "main.py").write_text(
        "class _P:\n"
        f"    def register(self, api):\n"
        f"        {body}\n"
        "plugin = _P()\n",
        encoding="utf-8",
    )
    if extra_py is not None:
        (root / "helper.py").write_text(extra_py, encoding="utf-8")
    return root


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "broken_code",
    ["def (\n", "raise RuntimeError('module boom')\n"],
    ids=["syntax", "module-level"],
)
async def test_probe_failure_keeps_old(
    tmp_path: Path,
    fresh_registry,
    monkeypatch,
    broken_code,
):
    monkeypatch.setattr(
        "qwenpaw.constant.WORKING_DIR",
        tmp_path / "work",
    )
    installed = _write_plugin(
        tmp_path / "plugins" / "keep-old",
        "keep-old",
        body="api.register_slash_command('ping', lambda c, a: None)",
    )
    workspace = SimpleNamespace(
        agent_id="talk",
        plugins=SimpleNamespace(
            slash_command_registry=SlashCommandRegistry(),
        ),
    )
    fresh_registry.projector = WorkspaceProjector(
        live_workspaces=lambda: [workspace],
    )
    fresh_registry.set_workspace_manager(
        SimpleNamespace(agents={"talk": workspace}),
    )
    loader = PluginLoader(plugin_dirs=[tmp_path / "plugins"])
    loader.registry = fresh_registry
    manifest = PluginManifest.from_dict(
        json.loads((installed / "plugin.json").read_text(encoding="utf-8")),
    )
    await loader.load_plugin(manifest, installed)
    await fresh_registry.projector.project(
        "slash_command",
        "ping",
        "keep-old",
    )
    assert "ping" in workspace.plugins.slash_command_registry.names()

    broken = tmp_path / "staging-broken"
    _write_plugin(broken, "keep-old", body="pass")
    (broken / "main.py").write_text(broken_code, encoding="utf-8")

    report = await loader.lifecycle.reload(
        "keep-old",
        new_source=broken,
    )
    assert report.unchanged
    assert not report.ok
    assert "keep-old" in loader.get_all_loaded_plugins()
    assert "keep-old" in fresh_registry.get_all_plugin_manifests()
    assert "ping" in workspace.plugins.slash_command_registry.names()
    assert (
        (installed / "main.py")
        .read_text(encoding="utf-8")
        .count(
            "register_slash_command",
        )
    )


def test_probe_cleans_finder_and_modules(tmp_path: Path):
    root = _write_plugin(tmp_path / "probe-me", "probe-me")
    probe_plugin_source("probe-me", root, root / "main.py")
    probe_name = plugin_module_name("probe-me", probe=True)
    assert not get_namespace_finder().is_registered(probe_name)
    assert probe_name not in sys.modules
    finish_probe_import("probe-me", root)
    assert not get_namespace_finder().is_registered(probe_name)


def test_updating_marker_restores_old_directory(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "qwenpaw.constant.WORKING_DIR",
        tmp_path / "work",
    )
    target = tmp_path / "plugins" / "mid"
    target.mkdir(parents=True)
    (target / "main.py").write_text("NEW\n", encoding="utf-8")
    backup = tmp_path / "plugins" / "mid.bak"
    backup.mkdir()
    (backup / "main.py").write_text("OLD\n", encoding="utf-8")
    write_updating_marker(
        "mid",
        backup_path=backup,
        target_path=target,
    )
    assert recover_interrupted_updates() == ["mid"]
    assert (target / "main.py").read_text(encoding="utf-8") == "OLD\n"
    assert not backup.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupted_at", ["copy", "move"])
async def test_boot_recovers_only_before_swap_starts(
    tmp_path: Path,
    fresh_registry,
    monkeypatch,
    interrupted_at: str,
):
    monkeypatch.setattr("qwenpaw.constant.WORKING_DIR", tmp_path / "work")
    plugin_id = f"interrupted-staging-{interrupted_at}"
    installed = _write_plugin(
        tmp_path / "plugins" / plugin_id,
        plugin_id,
        body="self.version = 'old'",
    )
    incoming = _write_plugin(
        tmp_path / "incoming",
        plugin_id,
        body="self.version = 'new'",
        version="2.0.0",
    )
    loader = PluginLoader([installed.parent])
    loader.registry = fresh_registry

    class InterruptedProcess(BaseException):
        pass

    def interrupt_copy(_source, destination):
        Path(destination).mkdir()
        (Path(destination) / "partial").write_text("incomplete")
        raise InterruptedProcess

    def interrupt_move(_source, _destination):
        raise InterruptedProcess

    with monkeypatch.context() as patch:
        patch.setattr(
            shutil,
            "copytree" if interrupted_at == "copy" else "move",
            interrupt_copy if interrupted_at == "copy" else interrupt_move,
        )
        with pytest.raises(InterruptedProcess):
            await loader._swap_plugin_dir(plugin_id, installed, incoming)

    marker = json.loads(marker_path(plugin_id).read_text())
    assert not Path(marker["backup_path"]).exists()
    boot_loader = PluginLoader([installed.parent])
    boot_loader.registry = fresh_registry
    await boot_loader.load_all_plugins(activate=True)
    record = boot_loader.get_loaded_plugin(plugin_id)
    if interrupted_at == "copy":
        assert record.status == "active"
        assert record.manifest.version == "1.0.0"
        assert sys.modules[plugin_module_name(plugin_id)].plugin.version == (
            "old"
        )
        assert not Path(marker["staging_path"]).exists()
        assert not marker_path(plugin_id).exists()
    else:
        assert record.status == "failed"
        assert "needs_restart" in record.diagnostics
        assert plugin_module_name(plugin_id) not in sys.modules
        assert marker_path(plugin_id).exists()
    assert marker["phase"] == (
        "staging" if interrupted_at == "copy" else "swapping"
    )


def test_tool_fields_follow_plugin_keep_user(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "qwenpaw.constant.WORKING_DIR",
        tmp_path / "work",
    )
    apply_tool_factory(
        "tid",
        "demo",
        {
            "description": "old",
            "icon": "a",
            "display_to_user": True,
            "enabled": False,
            "async_execution": False,
            "config": {},
        },
        None,
    )
    merged = apply_tool_factory(
        "tid",
        "demo",
        {
            "description": "new",
            "icon": "b",
            "display_to_user": True,
            "enabled": False,
            "async_execution": False,
            "config": {},
        },
        {
            "description": "old",
            "icon": "a",
            "display_to_user": True,
            "enabled": True,
            "async_execution": True,
            "config": {"k": 1},
        },
    )
    assert merged["description"] == "new"
    assert merged["icon"] == "b"
    assert merged["enabled"] is True
    assert merged["async_execution"] is True
    assert merged["config"] == {"k": 1}


def test_provision_register_failure_keeps_user_edits(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setattr(
        "qwenpaw.constant.WORKING_DIR",
        tmp_path / "work",
    )
    src = tmp_path / "src"
    src.mkdir()
    (src / "note.md").write_text("v1\n", encoding="utf-8")
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "note.md").write_text("v1\n", encoding="utf-8")
    provision_files("pid", src, dest, "1.0.0")
    (dest / "note.md").write_text("user\n", encoding="utf-8")
    (src / "note.md").write_text("v2\n", encoding="utf-8")
    assert provision_files("pid", src, dest, "2.0.0") == "migrate"
    assert (dest / "note.md").read_text(encoding="utf-8") == "user\n"
    recover_migrating_inventory("pid")
    assert (dest / "note.md").read_text(encoding="utf-8") == "user\n"


@pytest.mark.asyncio
async def test_reload_rollback_leaves_old_on_disk(
    tmp_path: Path,
    fresh_registry,
    monkeypatch,
):
    monkeypatch.setattr(
        "qwenpaw.constant.WORKING_DIR",
        tmp_path / "work",
    )
    installed = _write_plugin(
        tmp_path / "plugins" / "roll",
        "roll",
        body="pass",
    )
    old_text = (installed / "main.py").read_text(encoding="utf-8")
    loader = PluginLoader(plugin_dirs=[tmp_path / "plugins"])
    loader.registry = fresh_registry
    manifest = PluginManifest.from_dict(
        json.loads((installed / "plugin.json").read_text(encoding="utf-8")),
    )
    await loader.load_plugin(manifest, installed)

    staging = _write_plugin(
        tmp_path / "staging-roll",
        "roll",
        body="raise RuntimeError('register boom')",
    )
    report = await loader.lifecycle.reload("roll", new_source=staging)
    assert not report.ok
    assert not report.unchanged
    assert (installed / "main.py").read_text(encoding="utf-8") == old_text
    assert "roll" in loader.get_all_loaded_plugins()


def test_commit_migrations_clears_marker(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "qwenpaw.constant.WORKING_DIR",
        tmp_path / "work",
    )
    src = tmp_path / "s"
    src.mkdir()
    (src / "a.txt").write_text("1\n", encoding="utf-8")
    dest = tmp_path / "d"
    dest.mkdir()
    (dest / "a.txt").write_text("1\n", encoding="utf-8")
    provision_files("cid", src, dest, "1.0.0")
    (src / "a.txt").write_text("2\n", encoding="utf-8")
    provision_files("cid", src, dest, "2.0.0")
    loc = load_inventory("cid")["locations"][str(dest)]
    assert loc.get("migrating")
    commit_migrations("cid")
    loc = load_inventory("cid")["locations"][str(dest)]
    assert loc.get("migrating") is None


@pytest.mark.asyncio
async def test_reload_restore_failure_preserves_backups_and_blocks_activation(
    tmp_path: Path,
    fresh_registry,
    monkeypatch,
):
    from qwenpaw.plugins import provision as provision_module

    monkeypatch.setattr("qwenpaw.constant.WORKING_DIR", tmp_path / "work")
    plugin_id = "restore-blocked"
    dest = tmp_path / "data"
    trace = tmp_path / "startup"
    for version in ("1.0.0", "2.0.0"):
        factory = tmp_path / version
        factory.mkdir()
        (factory / "version.txt").write_text(version, encoding="utf-8")
    installed = _write_plugin(
        tmp_path / "plugins" / plugin_id,
        plugin_id,
        body=(
            "from pathlib import Path\n"
            f"        if not Path({str(dest)!r}).exists():\n"
            f"            api.provision_files({str(tmp_path / '1.0.0')!r}, "
            f"{str(dest)!r}, '1.0.0')\n"
            "        api.register_startup_hook('note', "
            f"lambda: Path({str(trace)!r}).write_text('old started'))"
        ),
    )
    loader = PluginLoader(plugin_dirs=[installed.parent])
    loader.registry = fresh_registry
    manifest = PluginManifest.from_dict(
        json.loads((installed / "plugin.json").read_text(encoding="utf-8")),
    )
    assert (await loader.load_plugin(manifest, installed)).status == "active"
    trace.unlink()
    staging = _write_plugin(
        tmp_path / "staging",
        plugin_id,
        body=(
            f"api.provision_files({str(tmp_path / '2.0.0')!r}, "
            f"{str(dest)!r}, '2.0.0')\n"
            "        raise RuntimeError('register boom')"
        ),
    )
    incoming = json.loads(
        (staging / "plugin.json").read_text(encoding="utf-8"),
    )
    incoming["version"] = "2.0.0"
    (staging / "plugin.json").write_text(
        json.dumps(incoming),
        encoding="utf-8",
    )

    def deny_restore(_backup, _dest):
        raise PermissionError("provision restore occupied")

    restore_backup = provision_module._restore_backup
    monkeypatch.setattr(provision_module, "_restore_backup", deny_restore)
    report = await loader.lifecycle.reload(plugin_id, new_source=staging)
    assert not report.ok
    assert report.needs_restart
    assert any("register boom" in error for error in report.errors)
    assert any(
        "provision restore occupied" in error for error in report.errors
    )
    assert not trace.exists(), "old service must wait for disk recovery"
    record = loader.get_loaded_plugin(plugin_id)
    assert record is not None and record.status == "failed"
    assert any(
        "provision restore occupied" in item for item in record.diagnostics
    )
    location = load_inventory(plugin_id)["locations"][str(dest)]
    assert location["migrating"]["status"] == "prepared"
    backup = Path(location["migrating"]["backup_path"])
    assert (backup / "version.txt").read_text(encoding="utf-8") == "1.0.0"
    marker = json.loads(marker_path(plugin_id).read_text(encoding="utf-8"))
    assert marker["status"] == "prepared"
    assert Path(marker["backup_path"]).is_dir()
    with pytest.raises(RuntimeError):
        await loader.lifecycle.activate(plugin_id)
    assert not trace.exists()

    monkeypatch.setattr(provision_module, "_restore_backup", restore_backup)
    restarted = PluginLoader(plugin_dirs=[installed.parent])
    restarted.registry = fresh_registry
    await restarted.load_all_plugins(activate=True)
    assert restarted.get_loaded_plugin(plugin_id).status == "active"
    assert trace.read_text(encoding="utf-8") == "old started"
    assert (dest / "version.txt").read_text(encoding="utf-8") == "1.0.0"
    assert not backup.exists()
    assert not marker_path(plugin_id).exists()


@pytest.fixture()
def pending_migrations(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("qwenpaw.constant.WORKING_DIR", tmp_path / "work")
    plugin_id = "cleanup"
    dests = []
    backups = []
    for name in ("occupied", "available"):
        src = tmp_path / f"src-{name}"
        src.mkdir()
        (src / "version.txt").write_text("old", encoding="utf-8")
        dest = tmp_path / name
        provision_files(plugin_id, src, dest, "1")
        (src / "version.txt").write_text("new", encoding="utf-8")
        provision_files(plugin_id, src, dest, "2")
        dests.append(dest)
        marker = load_inventory(plugin_id)["locations"][str(dest)]["migrating"]
        backups.append(Path(marker["backup_path"]))
    return plugin_id, dests, backups


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["activate", "provision", "plugin"])
async def test_committed_cleanup_failure_keeps_retry_locations(
    tmp_path: Path,
    monkeypatch,
    fresh_registry,
    pending_migrations,
    entry: str,
):
    from qwenpaw.plugins import loader as loader_module
    from qwenpaw.plugins import provision as provision_module

    plugin_id, dests, backups = pending_migrations
    loader = PluginLoader(plugin_dirs=[])
    loader.registry = fresh_registry
    plugin_backup = tmp_path / "plugin.bak"
    plugin_backup.mkdir()
    target = _write_plugin(tmp_path / "plugin", plugin_id)
    if entry != "activate":
        write_updating_marker(
            plugin_id,
            backup_path=plugin_backup,
            target_path=target,
        )
    blocked = plugin_backup if entry == "plugin" else backups[0]
    module = loader_module if entry == "plugin" else provision_module
    attribute = "_remove_dir" if entry == "plugin" else "_remove_path"
    remove = getattr(module, attribute)

    def deny_occupied(path):
        if path == blocked:
            raise PermissionError("backup occupied")
        return remove(path)

    monkeypatch.setattr(module, attribute, deny_occupied)
    committed = [None]
    if entry == "activate":
        errors = await loader._commit_plugin_transaction(
            plugin_id,
            committed=committed,
        )
        assert committed[0] == errors
        assert not marker_path(plugin_id).exists()
    else:
        errors = await loader._finish_committed_reload(
            plugin_id,
            plugin_backup,
        )
        assert update_marker_status(plugin_id) == "committed"
        assert plugin_backup.is_dir()
    assert any("backup occupied" in error for error in errors)
    locations = load_inventory(plugin_id)["locations"]
    assert locations[str(dests[1])]["migrating"] is None
    assert not backups[1].exists()
    if entry != "plugin":
        assert locations[str(dests[0])]["migrating"]["status"] == "committed"
        assert backups[0].is_dir()

    monkeypatch.setattr(module, attribute, remove)
    if entry == "activate":
        recovery = recover_migrating_inventory(plugin_id)
        assert not recovery.failures
    else:
        assert (
            await loader._finish_committed_reload(plugin_id, plugin_backup)
            == []
        )
        assert not marker_path(plugin_id).exists()
        assert not plugin_backup.exists()
    for dest, backup in zip(dests, backups):
        assert (dest / "version.txt").read_text(encoding="utf-8") == "new"
        assert not backup.exists()
        assert (
            load_inventory(plugin_id)["locations"][str(dest)]["migrating"]
            is None
        )


@pytest.mark.asyncio
async def test_effect_is_torn_down_on_unload():
    inst = PluginInstance("fx")
    state = {"on": False}

    def setup():
        state["on"] = True

    def teardown():
        state["on"] = False

    inst.record_runtime(
        "patch",
        teardown,
        shutdown_critical=False,
        kind="effect",
    )
    setup()
    assert state["on"]
    await inst.dispose(UnloadMode.UNLOAD)
    assert state["on"] is False


def _write_provision_plugin(
    root: Path,
    plugin_id: str,
    dest: Path,
    version: str,
    content: str,
    *,
    boom: bool = False,
) -> Path:
    factory = root / "factory"
    factory.mkdir(parents=True, exist_ok=True)
    (factory / "note.md").write_text(content, encoding="utf-8")
    boom_line = "        raise RuntimeError('register boom')\n" if boom else ""
    body = (
        "from pathlib import Path\n"
        f"        dest = Path({str(dest)!r})\n"
        "        api.provision_files(\n"
        "            Path(__file__).parent / 'factory', dest, "
        f"{version!r},\n"
        "        )\n"
        f"{boom_line}"
    )
    return _write_plugin(root, plugin_id, body=body)


@pytest.mark.asyncio
async def test_reload_register_failure_keeps_user_provision(
    tmp_path: Path,
    fresh_registry,
    monkeypatch,
):
    monkeypatch.setattr(
        "qwenpaw.constant.WORKING_DIR",
        tmp_path / "work",
    )
    dest = tmp_path / "notes"
    installed = _write_provision_plugin(
        tmp_path / "plugins" / "prov",
        "prov",
        dest,
        "1.0.0",
        "v1\n",
    )
    loader = PluginLoader(plugin_dirs=[tmp_path / "plugins"])
    loader.registry = fresh_registry
    manifest = PluginManifest.from_dict(
        json.loads((installed / "plugin.json").read_text(encoding="utf-8")),
    )
    await loader.load_plugin(manifest, installed)
    assert (dest / "note.md").read_text(encoding="utf-8") == "v1\n"
    (dest / "note.md").write_text("user\n", encoding="utf-8")

    staging = _write_provision_plugin(
        tmp_path / "staging-prov",
        "prov",
        dest,
        "2.0.0",
        "v2\n",
        boom=True,
    )
    report = await loader.lifecycle.reload("prov", new_source=staging)
    assert not report.ok
    assert (dest / "note.md").read_text(encoding="utf-8") == "user\n"


@pytest.mark.asyncio
async def test_successful_reload_replaces_source(
    tmp_path: Path,
    fresh_registry,
    monkeypatch,
):
    monkeypatch.setattr(
        "qwenpaw.constant.WORKING_DIR",
        tmp_path / "work",
    )
    installed = _write_plugin(tmp_path / "plugins" / "swap", "swap")
    loader = PluginLoader(plugin_dirs=[tmp_path / "plugins"])
    loader.registry = fresh_registry
    manifest = PluginManifest.from_dict(
        json.loads((installed / "plugin.json").read_text(encoding="utf-8")),
    )
    await loader.load_plugin(manifest, installed)
    staging = _write_plugin(
        tmp_path / "staging-swap",
        "swap",
        body="api.register_slash_command('ok', lambda c, a: None)",
    )
    report = await loader.lifecycle.reload("swap", new_source=staging)
    assert report.ok
    assert not report.unchanged
    assert "register_slash_command" in (installed / "main.py").read_text(
        encoding="utf-8",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entry,restore_blocked",
    [("reload", False), ("reload", True), ("force", False)],
)
async def test_cancel_during_completed_swap_restores_old_disk(
    tmp_path: Path,
    fresh_registry,
    monkeypatch,
    entry: str,
    restore_blocked: bool,
):
    monkeypatch.setattr("qwenpaw.constant.WORKING_DIR", tmp_path / "work")
    plugin_id = "cancel-swap"
    installed = _write_plugin(
        tmp_path / "plugins" / plugin_id,
        plugin_id,
        body="self.version = 'old'",
    )
    staging = _write_plugin(
        tmp_path / "incoming",
        plugin_id,
        body="self.version = 'new'",
        version="2.0.0",
    )
    loader = PluginLoader(plugin_dirs=[installed.parent])
    loader.registry = fresh_registry
    if entry == "reload":
        await loader.load_plugin(
            PluginManifest.from_dict(
                json.loads((installed / "plugin.json").read_text()),
            ),
            installed,
        )
    loop = asyncio.get_running_loop()
    cancelled = threading.Event()
    move = shutil.move
    task = None

    def cancel_reload():
        task.cancel()
        cancelled.set()

    def move_then_cancel(src, dest, *args, **kwargs):
        source = Path(src)
        if restore_blocked and ".bak" in source.name:
            raise PermissionError("plugin restore occupied")
        result = move(src, dest, *args, **kwargs)
        if source.name.endswith(".staging") and Path(dest) == installed:
            loop.call_soon_threadsafe(cancel_reload)
            assert cancelled.wait(5), "reload cancellation was not delivered"
        return result

    monkeypatch.setattr(shutil, "move", move_then_cancel)
    operation = (
        loader.lifecycle.reload(plugin_id, new_source=staging)
        if entry == "reload"
        else loader.load_plugin_from_path(staging, force=True)
    )
    task = asyncio.create_task(operation)
    with pytest.raises(asyncio.CancelledError):
        await task
    record = loader.get_loaded_plugin(plugin_id)
    if restore_blocked:
        marker = json.loads(marker_path(plugin_id).read_text())
        assert marker["status"] == "prepared"
        backup = Path(marker["backup_path"])
        assert json.loads((backup / "plugin.json").read_text())["version"] == (
            "1.0.0"
        )
        assert record is None or record.status == "failed"
        instance = loader.lifecycle.get_instance(plugin_id)
        assert "needs_restart" in instance.diagnostics
        assert any(
            "plugin restore occupied" in error
            for error in instance.diagnostics
        )
        with pytest.raises(RuntimeError, match="plugin restore occupied"):
            await loader.lifecycle.activate(plugin_id)
    else:
        assert json.loads((installed / "plugin.json").read_text())[
            "version"
        ] == ("1.0.0")
        if entry == "reload":
            assert record.status == "active"
            assert record.manifest.version == "1.0.0"
            module = sys.modules[plugin_module_name(plugin_id)]
            assert module.plugin.version == "old"
        else:
            assert record is None
        assert not marker_path(plugin_id).exists()
        assert not list(installed.parent.glob(f"{plugin_id}*.bak*"))


@pytest.mark.asyncio
async def test_force_install_keeps_old_when_probe_fails(
    tmp_path: Path,
    fresh_registry,
    monkeypatch,
):
    monkeypatch.setattr(
        "qwenpaw.constant.WORKING_DIR",
        tmp_path / "work",
    )
    installed = _write_plugin(tmp_path / "plugins" / "facade", "facade")
    loader = PluginLoader(plugin_dirs=[tmp_path / "plugins"])
    loader.registry = fresh_registry
    manifest = PluginManifest.from_dict(
        json.loads((installed / "plugin.json").read_text(encoding="utf-8")),
    )
    await loader.load_plugin(manifest, installed)
    staging = tmp_path / "staging-facade"
    _write_plugin(staging, "facade")
    (staging / "main.py").write_text("def (\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="probe failed"):
        await loader.load_plugin_from_path(staging, force=True)
    assert "facade" in loader.get_all_loaded_plugins()
    assert "class _P" in (installed / "main.py").read_text(encoding="utf-8")
