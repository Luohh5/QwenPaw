# -*- coding: utf-8 -*-
"""Retiring frozen host and native executable boundary for local-wheel tests."""

import asyncio
import base64
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

if "QWENPAW_FROZEN_FIXTURE_ROOT" in os.environ:
    launch = json.loads(
        (
            Path(os.environ["QWENPAW_FROZEN_FIXTURE_ROOT"]) / "host-state.json"
        ).read_text()
    )
    os.environ.update(launch["environment"])

import pytest

from tests.unit.plugins.test_frozen_dependency_preparation import (
    desktop as desktop_fixture,
    manifest_desktop as manifest_fixture,
)
from tests.unit.plugins.test_generic_dependency_gate import wheel
from qwenpaw.plugins.registry import PluginRegistry


def configure_host(root):
    """Exclude unrelated host service discovery from this dependency fixture."""
    from qwenpaw.config import config as config_model
    from qwenpaw.app.channels import registry as channel_registry

    config_model._BUILTIN_TOOLS_CACHE = {}
    channel_registry._BUILTIN_CHANNEL_CACHE = {}
    for key in ("WORKING", "SECRET", "BACKUP"):
        os.environ[f"QWENPAW_{key}_DIR"] = str(root / key.lower())


def native_continue(root):
    """Reenter the production frozen command with the genuine host inventory."""
    from qwenpaw.tauri import entry

    state = json.loads((root / "host-state.json").read_text())
    configure_host(root)
    sys.dont_write_bytecode = True
    sys.frozen = True
    sys.executable = state["executable"]
    sys.prefix = state["prefix"]
    sys.path[:] = state["paths"]
    if sys.argv[1:2] == ["--fixture-activate"]:
        asyncio.run(activate(root))
        return

    def refuse_desktop_startup():
        raise AssertionError("dependency command reached desktop startup")

    entry._install_desktop_runtime = refuse_desktop_startup
    entry.main()


async def activate(root):
    """A fresh ordinary host activation consumes the completed installation."""
    from qwenpaw.plugins.loader import PluginLoader
    from qwenpaw.plugins.architecture import PluginManifest
    from qwenpaw.plugins.lifecycle import UnloadMode

    source = root / "plugins/ordinary-plugin"
    loader = PluginLoader([source.parent])
    manifest = PluginManifest.from_dict(
        json.loads((source / "plugin.json").read_text())
    )
    try:
        record = await loader.load_plugin(manifest, source, {"enabled": True})
        assert record.status == "active", record.diagnostics
    finally:
        await loader.lifecycle.unload_all(UnloadMode.SHUTDOWN)


def add_script(payload, version):
    """Give the real wheel a RECORD-owned portable Python script."""
    with zipfile.ZipFile(payload.path) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    info = f"frozen_local_sdk-{version}.dist-info"
    files[
        f"frozen_local_sdk-{version}.data/scripts/frozen-sdk-{version}"
    ] = b"#!python\nimport sys\nprint(sys.executable)\n"
    record = f"{info}/RECORD"
    files[record] = (
        "".join(
            f"{name},sha256={base64.urlsafe_b64encode(hashlib.sha256(content).digest()).decode().rstrip('=')},{len(content)}\n"
            for name, content in files.items()
            if name != record
        )
        + f"{record},,\n"
    ).encode()
    with zipfile.ZipFile(payload.path, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)


async def stage(root):
    from qwenpaw.plugins.channel_bootstrap import (
        resolve_default_channel_runtime,
    )
    from qwenpaw.plugins.dependency_upgrade import DependencyUpgradeManager

    configure_host(root)
    original_python = sys.executable
    patch = pytest.MonkeyPatch()
    fixture = desktop_fixture.__wrapped__(root, patch, PluginRegistry())
    desktop = next(fixture)
    manifest = manifest_fixture.__wrapped__(desktop, patch)
    next(manifest)
    try:
        # First install uses the same real pip / wheel preparation as an
        # ordinary desktop plugin with a previously missing dependency.
        desktop.requirements.write_text("frozen-local-sdk==1.0\n")
        initial = wheel(
            desktop.payload.path.parent, name="frozen-local-sdk", version="1.0"
        )
        add_script(initial, "1.0")
        patch.setattr(sys, "executable", str(desktop.resources / "backend"))
        stale = wheel(
            desktop.payload.path.parent, name="frozen-local-sdk", version="0.9"
        )
        stale_site = desktop.target.parent / "bundle-previous/site"
        with zipfile.ZipFile(stale.path) as archive:
            archive.extractall(stale_site)
        patch.syspath_prepend(str(stale_site))
        target = resolve_default_channel_runtime().dependency_install_target
        await desktop.loader._prepare_python_dependencies(
            desktop.requirements,
            "ordinary-plugin",
        )
        assert (target / "frozen_local_sdk-1.0.dist-info/METADATA").is_file()
        direct_script = (target / "bin/frozen-sdk-1.0").read_bytes()
        assert os.fsencode(desktop.executable) in direct_script
        assert os.fsencode(sys.executable) not in direct_script
        assert str(stale_site) not in sys.path
        assert all(
            stale_site not in spec.dependency_paths
            for spec, _ in desktop.audits
        )
        desktop.requirements.write_text("frozen-local-sdk==2.0\n")
        upgraded = wheel(
            desktop.payload.path.parent,
            name="frozen-local-sdk",
            requires=("frozen-host-leaf>=1",),
        )
        add_script(upgraded, "2.0")
        # Only the actual host sees this metadata, while the standalone
        # worker cannot. Its immutable version constrains the real solver.
        host_site = desktop.resources / "binaries/qwenpaw-backend/_internal"
        for name, version, requires in (("frozen-host-leaf", "1.0", ()),):
            payload = wheel(
                desktop.payload.path.parent,
                name=name,
                version=version,
                requires=requires,
            )
            with zipfile.ZipFile(payload.path) as archive:
                archive.extractall(host_site)
        wheel(
            desktop.payload.path.parent, name="frozen-host-leaf", version="2.0"
        )
        patch.syspath_prepend(str(host_site))
        patch.setattr(sys, "dont_write_bytecode", True)
        importlib.import_module("frozen_local_sdk")
        # Reaudit the real worker after host-only metadata exists. It remains
        # absent from this execution environment, while host audit retains it.
        desktop.requirements.write_text("frozen-local-sdk==1.0\n")
        await desktop.loader._prepare_python_dependencies(
            desktop.requirements,
            "ordinary-plugin",
        )
        worker_packages = sorted(desktop.audits[-1][1].distributions)
        desktop.requirements.write_text("frozen-local-sdk==2.0\n")
        (desktop.upload / "plugin.json").write_text(
            json.dumps(
                {
                    "id": "ordinary-plugin",
                    "version": "1.0",
                    "entry": {"backend": "plugin.py"},
                }
            )
        )
        (desktop.upload / "plugin.py").write_text(
            "import json\nfrom pathlib import Path\n"
            "from importlib.metadata import version\nimport frozen_local_sdk\n"
            "def register(api):\n"
            f"    Path({str(root / 'activation.json')!r}).write_text(json.dumps({{"
            "'version':version('frozen-local-sdk'),"
            "'origin':frozen_local_sdk.__file__}))\n"
            "from types import SimpleNamespace\n"
            "plugin = SimpleNamespace(register=register)\n",
        )
        backend = desktop.resources / "backend"
        backend.write_text(
            f"#!{original_python}\nimport os,runpy\n"
            f"os.environ['QWENPAW_FROZEN_FIXTURE_ROOT']={str(root)!r}\n"
            f"runpy.run_path({str(Path(__file__).resolve())!r},run_name='__main__')\n",
        )
        backend.chmod(0o755)
        patch.setattr(sys, "executable", str(backend))
        patch.setattr(sys, "prefix", str(desktop.resources))
        patch.setattr(
            sys,
            "path",
            [path for path in sys.path if "site-packages" not in path],
        )
        (root / "host-state.json").write_text(
            json.dumps(
                {
                    "executable": sys.executable,
                    "prefix": sys.prefix,
                    "paths": sys.path,
                    "environment": {
                        name: value
                        for name, value in os.environ.items()
                        if name.startswith("QWENPAW_")
                    },
                }
            )
        )
        manager = DependencyUpgradeManager(desktop.loader)
        manager.root = root / "working/dependency_upgrades"
        solver_python = []
        solve = desktop.loader._run_subprocess_with_streaming_log

        def observe_solver(command, **kwargs):
            solver_python.append(command[0])
            return solve(command, **kwargs)

        patch.setattr(
            desktop.loader,
            "_run_subprocess_with_streaming_log",
            observe_solver,
        )
        operation = await manager.stage(desktop.upload)
        assert operation["status"] == "needs_confirmation"
        assert operation["changes"] == [
            {
                "name": "frozen-local-sdk",
                "current_version": "1.0",
                "target_version": "2.0",
            }
        ]
        directory = manager.root / operation["id"]
        assert (target / "frozen_local_sdk-1.0.dist-info/METADATA").is_file()
        assert not (target / "frozen_local_sdk-2.0.dist-info").exists()
        applied = await manager.apply(operation["id"])
        assert applied["status"] == "waiting_restart", applied
        assert (target / "frozen_local_sdk-1.0.dist-info/METADATA").is_file()
        (root / "report.json").write_text(
            json.dumps(
                {
                    "directory": str(directory),
                    "python": str(desktop.executable),
                    "target": str(target),
                    "legacy": str(stale_site),
                    "resources": str(desktop.resources),
                    "host_site": str(host_site),
                    "worker_packages": worker_packages,
                    "constraints": (directory / "constraints.txt")
                    .read_text()
                    .splitlines(),
                    "solver_python": solver_python[0],
                }
            )
        )
    finally:
        for generator in (manifest, fixture):
            try:
                next(generator)
            except StopIteration:
                pass
        patch.undo()


if __name__ == "__main__":
    if "QWENPAW_FROZEN_FIXTURE_ROOT" in os.environ:
        native_continue(Path(os.environ["QWENPAW_FROZEN_FIXTURE_ROOT"]))
    else:
        asyncio.run(stage(Path(sys.argv[1])))
