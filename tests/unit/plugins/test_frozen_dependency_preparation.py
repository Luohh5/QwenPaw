# -*- coding: utf-8 -*-
"""Ordinary desktop plugins use readable support and a writable overlay."""

# pylint: disable=protected-access,redefined-outer-name

from dataclasses import replace
import ast
import hashlib
import importlib
from importlib import metadata
import json
import os
from pathlib import Path
import platform
import runpy
import shutil
import subprocess
import sys
import sysconfig
from types import SimpleNamespace
import venv
import zipfile

import pytest
from packaging.tags import sys_tags

from qwenpaw.plugins import loader as loader_module
from qwenpaw.plugins.architecture import PluginManifest
from qwenpaw.plugins.channel_adapter import ChannelRuntimeSettings
from qwenpaw.plugins.execution import PluginProcess
from qwenpaw.plugins.execution_backend import BackendBinding

from .test_generic_dependency_gate import declare, evidence, wheel

pytestmark = pytest.mark.skipif(
    os.name == "nt",
    reason="Immutable Resources fixture requires POSIX directory permissions",
)


def _contents(root):
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


@pytest.fixture
def desktop(tmp_path, monkeypatch, fresh_registry):
    """Two desktop trees: opaque frozen imports and real readable workers."""
    source = Path(loader_module.__file__).resolve().parents[2]
    resources = tmp_path / "Resources"
    support = resources / "binaries/channel-runtime/site"
    plugins = support / "qwenpaw/plugins"
    shutil.copytree(
        source / "qwenpaw/plugins/execution",
        plugins / "execution",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    shutil.copy2(
        source / "qwenpaw/plugins/dependency_maintenance.py",
        plugins / "dependency_maintenance.py",
    )
    frozen_loader = (
        resources
        / "binaries/qwenpaw-backend/_internal/qwenpaw/plugins/loader.py"
    )
    frozen_loader.parent.mkdir(parents=True)
    frozen_loader.write_text("# worker support is absent from this tree\n")
    work = tmp_path / "working"
    bucket = (
        f"py{sys.version_info.major}.{sys.version_info.minor}-"
        f"{platform.system().lower()}-{platform.machine().lower()}"
    )
    target = work / "plugin_runtime" / bucket / "site"
    target.mkdir(parents=True)
    settings = ChannelRuntimeSettings(
        python_executable=sys.executable,
        support_root=support,
        qwenpaw_version="test",
        dependency_paths=(support, target),
        dependency_install_target=target,
        runtime_identity={
            "version": list(sys.version_info[:2]),
            "implementation": sys.implementation.name,
            "cache_tag": sys.implementation.cache_tag,
            "sys_platform": sys.platform,
            "system": platform.system(),
            "machine": platform.machine(),
            "supported_tags": [str(tag) for tag in sys_tags()],
        },
    )
    upload = tmp_path / "ordinary-plugin"
    upload.mkdir()
    requirements = upload / "requirements.txt"
    requirements.write_text("frozen-local-sdk==2.0\n")
    index = tmp_path / "wheel-index"
    index.mkdir()
    payload = wheel(index, name="frozen-local-sdk")
    monkeypatch.setattr(loader_module, "_is_frozen", lambda: True)
    monkeypatch.setattr(loader_module, "__file__", str(frozen_loader))
    monkeypatch.setattr("qwenpaw.constant.WORKING_DIR", work)
    monkeypatch.setenv("QWENPAW_DESKTOP_PY_RUNTIME", sys.executable)
    monkeypatch.setenv("PIP_NO_INDEX", "1")
    monkeypatch.setenv("PIP_FIND_LINKS", str(index))
    monkeypatch.setenv("PIP_NO_CACHE_DIR", "1")
    monkeypatch.setenv("PIP_CONFIG_FILE", os.devnull)
    monkeypatch.delenv("QWENPAW_RUNTIME_PROVISIONER", raising=False)
    monkeypatch.setattr(sys, "path", sys.path.copy())
    audits = []
    audit = PluginProcess.audit

    async def observe(process, spec):
        result = await audit(process, spec)
        audits.append((spec, result.snapshot))
        return result

    monkeypatch.setattr(PluginProcess, "audit", observe)
    # Desktop Resources are not writable. In particular, reader custody must
    # not create mutable lease sidecars beside packaged dependency sites.
    support.parent.chmod(0o555)
    frozen_loader.parents[3].chmod(0o555)
    loader = loader_module.PluginLoader(
        [tmp_path / "plugins"],
        channel_runtime=settings,
    )
    host_audits = []
    host_audit = loader._python_backend.audit_dependencies

    async def observe_host(description):
        snapshot = await host_audit(description)
        host_audits.append(snapshot)
        return snapshot

    monkeypatch.setattr(
        loader._python_backend,
        "audit_dependencies",
        observe_host,
    )
    yield SimpleNamespace(
        loader=loader,
        settings=settings,
        resources=resources,
        support=support,
        target=target,
        upload=upload,
        requirements=requirements,
        payload=payload,
        audits=audits,
        host_audits=host_audits,
        registry=fresh_registry,
    )
    support.parent.chmod(0o755)
    frozen_loader.parents[3].chmod(0o755)
    assert not fresh_registry.runtime_owners.leases("ordinary-plugin")
    sys.modules.pop("frozen_local_sdk", None)
    sys.modules.pop("frozen_common_dependency", None)
    sys.path_importer_cache.pop(str(target), None)
    importlib.invalidate_caches()


@pytest.fixture
def manifest_desktop(desktop, monkeypatch):
    """A launcher manifest with an actual executable inside Resources."""
    from qwenpaw.__version__ import __version__
    from qwenpaw.config import utils as config_utils

    runtime = desktop.resources / "binaries/python-runtime/python"
    executable = runtime / (
        "Scripts/python.exe" if os.name == "nt" else "bin/python"
    )
    # Native venv creation supplies the platform's correct executable layout.
    # Reuse the test environment's offline pip/packaging via a trusted .pth;
    # neither worker auditing nor pip writes bytecode inside Resources.
    venv.EnvBuilder(with_pip=False, symlinks=False).create(runtime)
    local_site = runtime / (
        "Lib/site-packages"
        if os.name == "nt"
        else f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
    )
    (local_site / "test-runtime.pth").write_text(
        sysconfig.get_path("purelib") + "\n",
    )
    shared_worker = (
        desktop.support / "qwenpaw/channel_protocol/shared_worker.py"
    )
    shared_worker.parent.mkdir(parents=True)
    shared_worker.write_text(
        "# resolver requires the packaged channel worker\n",
    )
    index = desktop.support / "qwenpaw/official_channels/index.json"
    index.parent.mkdir(parents=True)
    index.write_text('{"schema_version":1}\n')
    manifest_path = desktop.resources / "binaries/channel-runtime/runtime.json"
    manifest = {
        "schema_version": 1,
        "host_version": __version__,
        "preset": "light",
        "python_executable": executable.relative_to(
            desktop.resources,
        ).as_posix(),
        "support_root": desktop.support.relative_to(
            desktop.resources,
        ).as_posix(),
        "dependency_paths": [
            desktop.support.relative_to(desktop.resources).as_posix(),
        ],
        "python": desktop.settings.runtime_identity,
        "python_runtime_release": "local-test-runtime",
        "host_wheel_sha256": "0" * 64,
        "official_index_sha256": hashlib.sha256(
            index.read_bytes(),
        ).hexdigest(),
    }
    desktop.support.parent.chmod(0o755)
    manifest_path.write_text(json.dumps(manifest))
    desktop.support.parent.chmod(0o555)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(config_utils, "WORKING_DIR", desktop.target.parents[2])
    monkeypatch.setattr(desktop.registry, "channel_runtime", None)
    monkeypatch.setenv("QWENPAW_TAURI_RESOURCE_DIR", str(desktop.resources))
    monkeypatch.setenv("QWENPAW_CHANNEL_RUNTIME_MANIFEST", str(manifest_path))
    monkeypatch.setenv("QWENPAW_DESKTOP_PY_RUNTIME", str(executable))
    monkeypatch.setenv("QWENPAW_CHANNEL_SUPPORT_ROOT", str(desktop.support))
    monkeypatch.setenv(
        "QWENPAW_CHANNEL_DEPENDENCY_PATHS",
        json.dumps([str(desktop.support)]),
    )
    monkeypatch.setenv("QWENPAW_CHANNEL_PRESET", "light")
    desktop.manifest = manifest
    desktop.manifest_path = manifest_path
    desktop.executable = executable
    local_site.parent.chmod(0o555)
    yield desktop
    local_site.parent.chmod(0o755)


@pytest.mark.asyncio
async def test_frozen_missing_dependency_uses_real_worker_and_host_overlay(
    desktop,
):
    before = _contents(desktop.resources)

    decision = await desktop.loader._ensure_dependencies_installed(
        desktop.upload,
        "ordinary-plugin",
        allow_install=True,
    )

    assert decision.already_satisfied and not decision.missing
    assert not decision.allow_install
    assert desktop.audits
    assert "frozen-local-sdk" not in desktop.audits[0][1].distributions
    assert len(desktop.host_audits) >= 2
    assert "frozen-local-sdk" not in desktop.host_audits[0].distributions
    assert (
        desktop.host_audits[-1].distributions["frozen-local-sdk"]["version"]
        == "2.0"
    )
    assert all(
        spec.support_root == desktop.support for spec, _ in desktop.audits
    )
    assert all(
        desktop.support in spec.dependency_paths for spec, _ in desktop.audits
    )
    assert os.environ["QWENPAW_PLUGIN_SITE"] == str(desktop.target)
    assert str(desktop.support) not in sys.path
    installed = desktop.target / "frozen_local_sdk.py"
    assert installed.is_file()
    assert metadata.version("frozen-local-sdk") == "2.0"
    module = importlib.import_module("frozen_local_sdk")
    assert Path(module.__file__) == installed
    assert module.VALUE == "safe-local-addition"
    assert _contents(desktop.resources) == before


@pytest.mark.asyncio
async def test_frozen_packaged_dependency_is_reused_without_acquisition(
    desktop,
    monkeypatch,
):
    host_site = desktop.resources / "binaries/qwenpaw-backend/_internal"
    with zipfile.ZipFile(desktop.payload.path) as archive:
        archive.extractall(desktop.support)
        archive.extractall(host_site)
    monkeypatch.syspath_prepend(str(host_site))
    before = _contents(desktop.resources)

    async def refuse_acquisition(*_args, **_kwargs):
        pytest.fail("A packaged satisfying dependency must not be downloaded")

    monkeypatch.setattr(
        desktop.loader,
        "_acquire_dependency_wheels",
        refuse_acquisition,
    )
    # Explicit preparation must still audit the runtime bundle even when the
    # frozen host already has its own copy of the distribution.
    await desktop.loader._prepare_python_dependencies(
        desktop.requirements,
        "ordinary-plugin",
    )
    decision = await desktop.loader._ensure_dependencies_installed(
        desktop.upload,
        "ordinary-plugin",
        allow_install=True,
    )

    assert decision.already_satisfied and not decision.missing
    assert (
        desktop.audits[-1][1].distributions["frozen-local-sdk"]["version"]
        == "2.0"
    )
    assert not list(desktop.target.iterdir())
    # Frozen imports do not need writable bytecode caches beside packaged code.
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    module = importlib.import_module("frozen_local_sdk")
    assert Path(module.__file__) == host_site / "frozen_local_sdk.py"
    assert str(desktop.support) not in sys.path
    assert _contents(desktop.resources) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("worker_has_common", [True, False])
async def test_frozen_sdk_install_retains_host_transitive_provider(
    desktop,
    monkeypatch,
    worker_has_common,
):
    host_site = desktop.resources / "binaries/qwenpaw-backend/_internal"
    common = wheel(
        desktop.payload.path.parent,
        name="frozen-common-dependency",
        version="1.0",
    )
    with zipfile.ZipFile(common.path) as archive:
        archive.extractall(host_site)
        if worker_has_common:
            archive.extractall(desktop.support)
    wheel(
        desktop.payload.path.parent,
        name="frozen-local-sdk",
        requires=("frozen-common-dependency==1.0",),
    )
    monkeypatch.syspath_prepend(str(host_site))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    original_provider = importlib.import_module("frozen_common_dependency")
    before = _contents(desktop.resources)

    decision = await desktop.loader._ensure_dependencies_installed(
        desktop.upload,
        "ordinary-plugin",
        allow_install=True,
    )
    assert decision.already_satisfied
    assert (
        "frozen-common-dependency" in desktop.audits[-1][1].distributions
    ) is worker_has_common
    backend = desktop.loader._python_backend
    description = backend.describe(
        desktop.upload,
        PluginManifest(id="ordinary-plugin", version="1.0"),
    )
    snapshot = await backend.audit_dependencies(description)
    # Duplicate runtime metadata must never make the frozen host's genuine
    # transitive dependency disappear from the evidence used for admission.
    assert "frozen-common-dependency" in snapshot.distributions
    assert (
        Path(
            snapshot.distributions["frozen-common-dependency"][
                "metadata_path"
            ],
        ).parent
        == host_site
    )
    usage = desktop.registry.dependency_usage
    token = usage.declare(description.dependency_declaration(), pending=True)
    try:
        await desktop.loader._admit_dependencies(
            BackendBinding(backend, description),
            token,
            snapshot,
        )
    finally:
        usage.withdraw(token)

    sdk = importlib.import_module("frozen_local_sdk")
    assert Path(sdk.__file__) == desktop.target / "frozen_local_sdk.py"
    assert (
        importlib.import_module("frozen_common_dependency")
        is original_provider
    )
    assert (
        Path(original_provider.__file__)
        == host_site / "frozen_common_dependency.py"
    )
    assert str(desktop.support) not in sys.path
    assert not (desktop.target / "frozen_common_dependency.py").exists()
    assert _contents(desktop.resources) == before


@pytest.mark.asyncio
async def test_frozen_wrong_native_identity_rejects_before_writes(
    desktop,
    monkeypatch,
):
    identity = {
        **desktop.settings.runtime_identity,
        "machine": "wrong-machine",
    }
    monkeypatch.setattr(
        desktop.registry,
        "channel_runtime",
        replace(desktop.settings, runtime_identity=identity),
    )
    before = _contents(desktop.resources)

    with pytest.raises(ValueError, match="CHANNEL_RUNTIME_IDENTITY_MISMATCH"):
        await desktop.loader._prepare_python_dependencies(
            desktop.requirements,
            "ordinary-plugin",
        )

    assert not list(desktop.target.iterdir())
    assert _contents(desktop.resources) == before


@pytest.mark.asyncio
async def test_frozen_missing_readable_worker_rejects_before_writes(desktop):
    from qwenpaw.plugins.execution import ProcessError

    (desktop.support / "qwenpaw/plugins/execution/worker.py").unlink()
    before = _contents(desktop.resources)

    with pytest.raises(ProcessError) as error:
        await desktop.loader._prepare_python_dependencies(
            desktop.requirements,
            "ordinary-plugin",
        )

    assert error.value.reason_code == "BOOTSTRAP_UNAVAILABLE"
    assert not list(desktop.target.iterdir())
    assert _contents(desktop.resources) == before


@pytest.mark.asyncio
async def test_frozen_without_registered_settings_uses_launcher_manifest(
    manifest_desktop,
    monkeypatch,
):
    from qwenpaw.plugins.channel_bootstrap import (
        resolve_default_channel_runtime,
    )

    desktop = manifest_desktop
    legacy = desktop.target
    common = wheel(
        desktop.payload.path.parent,
        name="frozen-common-dependency",
    )
    host_site = desktop.resources / "binaries/qwenpaw-backend/_internal"
    with zipfile.ZipFile(common.path) as archive:
        archive.extractall(desktop.support)
        archive.extractall(legacy)
        archive.extractall(host_site)
    monkeypatch.syspath_prepend(str(host_site))
    desktop.requirements.write_text(
        "frozen-local-sdk==2.0\nfrozen-common-dependency==2.0\n",
    )
    legacy_before = _contents(legacy)
    before = _contents(desktop.resources)
    settings = resolve_default_channel_runtime()
    target = settings.dependency_install_target

    decision = await desktop.loader._ensure_dependencies_installed(
        desktop.upload,
        "ordinary-plugin",
        allow_install=True,
    )

    assert decision.already_satisfied and not decision.missing
    assert all(
        Path(spec.python_executable) == desktop.executable
        for spec, _ in desktop.audits
    )
    assert all(
        spec.support_root == desktop.support for spec, _ in desktop.audits
    )
    assert target != legacy
    assert loader_module._plugin_site_dir() == target
    assert all(
        spec.dependency_paths == settings.dependency_paths
        for spec, _ in desktop.audits
    )
    assert all(
        legacy not in spec.dependency_paths for spec, _ in desktop.audits
    )
    assert os.environ["QWENPAW_PLUGIN_SITE"] == str(target)
    assert str(target) in sys.path
    assert str(legacy) not in sys.path
    module = importlib.import_module("frozen_local_sdk")
    assert Path(module.__file__) == target / "frozen_local_sdk.py"
    assert _contents(legacy) == legacy_before
    assert _contents(desktop.resources) == before


@pytest.mark.asyncio
async def test_frozen_invalid_launcher_manifest_rejects_before_writes(
    manifest_desktop,
):
    desktop = manifest_desktop
    desktop.manifest["support_root"] = "../untrusted-support"
    desktop.manifest_path.write_text(json.dumps(desktop.manifest))
    before = _contents(desktop.resources)

    with pytest.raises(ValueError, match="CHANNEL_RUNTIME_MANIFEST_INVALID"):
        await desktop.loader._prepare_python_dependencies(
            desktop.requirements,
            "ordinary-plugin",
        )

    assert not desktop.audits
    assert not list(desktop.target.iterdir())
    assert _contents(desktop.resources) == before


@pytest.mark.asyncio
async def test_source_dependency_audit_keeps_shared_interpreter_and_support(
    desktop,
    monkeypatch,
):
    from qwenpaw.plugins.execution.environment import shared_dependency_paths

    source_support = Path(__file__).resolve().parents[3] / "src"
    monkeypatch.setattr(loader_module, "_is_frozen", lambda: False)
    monkeypatch.setattr(
        loader_module,
        "__file__",
        str(source_support / "qwenpaw/plugins/loader.py"),
    )
    desktop.requirements.write_text("packaging\n")
    before = _contents(desktop.resources)

    await desktop.loader._prepare_python_dependencies(
        desktop.requirements,
        "ordinary-plugin",
    )

    assert desktop.audits
    assert all(
        spec.python_executable == sys.executable for spec, _ in desktop.audits
    )
    assert all(
        spec.support_root == source_support for spec, _ in desktop.audits
    )
    assert all(
        spec.dependency_paths == shared_dependency_paths()
        for spec, _ in desktop.audits
    )
    assert str(desktop.target) not in sys.path
    assert not list(desktop.target.iterdir())
    assert _contents(desktop.resources) == before


@pytest.mark.asyncio
async def test_frozen_metadata_closure_satisfies_hub_host_without_pip(
    desktop,
    monkeypatch,
):
    from qwenpaw.plugins.execution.dependencies import _installed_metadata

    build_site = desktop.upload.parent / "build-site"
    host_site = desktop.resources / "binaries/qwenpaw-backend/_internal"
    for name, requires in (
        ("qwenpaw", ("frozen-local-sdk==2.0", "httpx==2.0")),
        ("httpx", ("frozen-common-dependency==2.0",)),
        ("frozen-local-sdk", ("frozen-common-dependency==2.0",)),
        ("frozen-common-dependency", ()),
    ):
        payload = wheel(
            desktop.payload.path.parent,
            name=name,
            requires=requires,
        )
        with zipfile.ZipFile(payload.path) as archive:
            archive.extractall(build_site)
            if name != "qwenpaw":
                archive.extractall(desktop.support)
    # Execute the actual metadata collection section of the desktop spec,
    # substituting only PyInstaller's data-copy descriptor boundary.
    spec = (
        Path(__file__).resolve().parents[3] / "scripts/pack-tauri/qwenpaw.spec"
    )
    nodes = ast.parse(spec.read_text()).body
    selected = [
        node
        for node in nodes
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.For))
        and any(
            isinstance(item, ast.Name)
            and item.id in {"copy_metadata", "_metadata_pkgs"}
            for item in ast.walk(node)
        )
    ]
    inventory = {
        dist.metadata["Name"]: dist
        for dist in metadata.distributions(path=[str(build_site)])
    }

    def installed(name):
        if name not in inventory:
            raise metadata.PackageNotFoundError(name)
        return inventory[name]

    namespace = {
        "Path": Path,
        "runpy": runpy,
        "SPECPATH": str(spec.parent),
        "PackageNotFoundError": metadata.PackageNotFoundError,
        "copy_metadata": lambda name: [
            (str(metadata.distribution(name)._path), "."),
        ],
        "datas": [],
    }
    with monkeypatch.context() as build:
        build.setattr(metadata, "distribution", installed)
        exec(
            compile(ast.Module(body=selected, type_ignores=[]), spec, "exec"),
            namespace,
        )
    copied = set(namespace["datas"])
    for source, _ in copied:
        shutil.copytree(source, host_site / Path(source).name)
    host_rows, invalid, _ = _installed_metadata((str(host_site),))
    assert not invalid
    host = replace(
        evidence(host_site, package_sites=(host_site, desktop.target)),
        distributions={"httpx": host_rows["httpx"]},
    )
    usage = desktop.registry.dependency_usage
    token = declare(usage, "aliyun-hub", host, ())
    usage.observe_host(token, host)
    usage.withdraw(token)
    before = _contents(desktop.resources)

    async def no_pip(*args, **kwargs):
        pytest.fail("A complete packaged environment must not acquire wheels")

    monkeypatch.setattr(desktop.loader, "_acquire_dependency_wheels", no_pip)
    monkeypatch.syspath_prepend(str(host_site))
    await desktop.loader._prepare_python_dependencies(
        desktop.requirements,
        "ordinary-plugin",
    )
    result = await desktop.loader._ensure_dependencies_installed(
        desktop.upload,
        "ordinary-plugin",
        allow_install=True,
    )

    assert result.already_satisfied and not result.missing
    assert "frozen-common-dependency" in desktop.audits[-1][1].distributions
    assert _contents(desktop.resources) == before
    assert not list(desktop.target.iterdir())


def test_frozen_ordinary_upgrade_uses_the_prepared_managed_site(tmp_path):
    """Direct install, review and offline continuation share a frozen target."""
    root = Path(__file__).resolve().parents[3]
    helper = root / "tests/fixtures/shared_dependencies/frozen_upgrade_host.py"
    result = subprocess.run(
        [sys.executable, "-I", str(helper), str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((tmp_path / "report.json").read_text())
    directory = Path(report["directory"])
    stored = json.loads((directory / "operation.json").read_text())
    assert stored["approved"] and stored["status"] == "waiting_restart"
    assert stored["worker_python"] == report["python"]
    assert (
        stored["environment"]["executable"]
        == stored["continuation_command"][0]
    )
    assert stored["environment"]["executable"] != report["python"]
    context = stored["dependency_context"]
    assert context["python_executable"] == report["python"]
    assert context["install_target"] == report["target"]
    assert str(Path(report["host_site"])) in stored["environment"]["sites"]
    assert "frozen-host-leaf" not in report["worker_packages"]
    assert "frozen-host-leaf==1.0" in report["constraints"]
    assert stored["proposed"]["frozen-host-leaf"]["version"] == "1.0"
    assert report["solver_python"] == report["python"]

    from qwenpaw.plugins.dependency_transaction import authenticate_plan

    stored["dependency_context"]["install_target"] = str(tmp_path / "other")
    with pytest.raises(RuntimeError, match="DEPENDENCY_PLAN_STALE"):
        authenticate_plan(directory, stored)

    before = _contents(Path(report["resources"]))
    legacy = _contents(Path(report["legacy"]))
    unauthorized = subprocess.run(
        [*stored["continuation_command"], "1"],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
        env={
            name: value
            for name, value in os.environ.items()
            if name != "QWENPAW_DEPENDENCY_MAINTENANCE"
        },
    )
    assert unauthorized.returncode != 0
    assert "DEPENDENCY_APPROVAL_REQUIRED" in unauthorized.stderr
    assert (
        "dependency command reached desktop startup" not in unauthorized.stderr
    )
    assert _contents(Path(report["resources"])) == before
    result = subprocess.run(
        [
            report["python"],
            "-I",
            "-S",
            str(directory / "worker/dependency_worker.py"),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    operation = json.loads((directory / "operation.json").read_text())
    assert result.returncode == 0, (
        result.stdout + result.stderr + str(operation.get("error"))
    )
    assert operation["status"] == "succeeded", operation.get("error")
    assert operation["result"]["id"] == "ordinary-plugin"
    assert operation["result"]["status"] == "inactive"
    assert not (tmp_path / "activation.json").exists()
    activated = subprocess.run(
        [stored["continuation_command"][0], "--fixture-activate"],
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert activated.returncode == 0, activated.stdout + activated.stderr
    target = Path(report["target"])
    assert not (target / "frozen_local_sdk-1.0.dist-info").exists()
    assert (target / "frozen_local_sdk-2.0.dist-info/METADATA").is_file()
    assert not (target / "frozen_host_leaf.py").exists()
    script = target / "bin/frozen-sdk-2.0"
    assert os.fsencode(report["python"]) in script.read_bytes()
    launched = subprocess.run(
        [str(script)],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert launched.returncode == 0, launched.stdout + launched.stderr
    assert launched.stdout.strip() == report["python"]
    activation = json.loads((tmp_path / "activation.json").read_text())
    assert activation == {
        "version": "2.0",
        "origin": str(target / "frozen_local_sdk.py"),
    }
    assert _contents(Path(report["resources"])) == before
    assert _contents(Path(report["legacy"])) == legacy


@pytest.mark.asyncio
async def test_frozen_ordinary_packaged_upgrade_refuses_before_resolution(
    manifest_desktop,
    monkeypatch,
):
    from qwenpaw.plugins.dependency_gate import SharedDependencyError
    from qwenpaw.plugins.dependency_upgrade import DependencyUpgradeManager

    desktop = manifest_desktop
    host_site = desktop.resources / "binaries/qwenpaw-backend/_internal"
    old = wheel(
        desktop.payload.path.parent,
        name="frozen-local-sdk",
        version="1.0",
    )
    with zipfile.ZipFile(old.path) as archive:
        archive.extractall(host_site)
        archive.extractall(desktop.support)
    monkeypatch.syspath_prepend(str(host_site))
    monkeypatch.setattr(sys, "prefix", str(desktop.resources))
    monkeypatch.setattr(sys, "executable", str(desktop.resources / "backend"))
    monkeypatch.setattr(
        sys,
        "path",
        [path for path in sys.path if "site-packages" not in path],
    )
    (desktop.upload / "plugin.json").write_text(
        json.dumps(
            {
                "id": "ordinary-plugin",
                "version": "1.0",
                "entry": {},
            },
        ),
    )

    def refuse_solver(*_args, **_kwargs):
        pytest.fail("A conflicting packaged provider must refuse before pip")

    monkeypatch.setattr(
        desktop.loader,
        "_run_subprocess_with_streaming_log",
        refuse_solver,
    )
    manager = DependencyUpgradeManager(desktop.loader)
    manager.root = desktop.upload.parent / "working/dependency_upgrades"
    before = _contents(desktop.resources)

    with pytest.raises(SharedDependencyError) as refused:
        await manager.stage(desktop.upload)

    assert refused.value.reason_code == "DEPENDENCY_ENVIRONMENT_PROTECTED"
    assert "frozen-local-sdk" in str(refused.value)
    assert _contents(desktop.resources) == before
    assert not list(manager.root.glob("*/operation.json"))
    assert not list(desktop.target.iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["direct", "upgrade"])
async def test_frozen_worker_only_packaged_provider_cannot_be_overlaid(
    desktop,
    monkeypatch,
    operation,
):
    from qwenpaw.plugins.dependency_gate import SharedDependencyError
    from qwenpaw.plugins.dependency_upgrade import DependencyUpgradeManager

    old = wheel(
        desktop.payload.path.parent,
        name="frozen-local-sdk",
        version="1.0",
    )
    with zipfile.ZipFile(old.path) as archive:
        archive.extractall(desktop.support)
    (desktop.upload / "plugin.json").write_text(
        json.dumps(
            {
                "id": "ordinary-plugin",
                "version": "1.0",
                "entry": {},
            },
        ),
    )
    monkeypatch.setattr(sys, "prefix", str(desktop.resources))
    monkeypatch.setattr(sys, "executable", str(desktop.resources / "backend"))
    monkeypatch.setattr(
        sys,
        "path",
        [path for path in sys.path if "site-packages" not in path],
    )
    before = _contents(desktop.resources)
    manager = DependencyUpgradeManager(desktop.loader)
    manager.root = desktop.upload.parent / "working/dependency_upgrades"

    with pytest.raises(SharedDependencyError) as refused:
        if operation == "direct":
            await desktop.loader._prepare_python_dependencies(
                desktop.requirements,
                "ordinary-plugin",
            )
        else:
            await manager.stage(desktop.upload)

    assert (
        refused.value.reason_code == "DEPENDENCY_ENVIRONMENT_PROTECTED"
    ), str(refused.value)
    assert _contents(desktop.resources) == before
    assert not list(desktop.target.iterdir())
    assert not list(manager.root.glob("*/operation.json"))


@pytest.mark.asyncio
async def test_frozen_immutable_metadata_change_invalidates_reviewed_upgrade(
    desktop,
    monkeypatch,
):
    from qwenpaw.plugins.dependency_upgrade import DependencyUpgradeManager

    for name, version, site in (
        ("frozen-local-sdk", "1.0", desktop.target),
        ("frozen-policy", "1.0", desktop.support),
    ):
        payload = wheel(
            desktop.payload.path.parent,
            name=name,
            version=version,
        )
        with zipfile.ZipFile(payload.path) as archive:
            archive.extractall(site)
    (desktop.upload / "plugin.json").write_text(
        json.dumps(
            {
                "id": "ordinary-plugin",
                "version": "1.0",
                "entry": {},
            },
        ),
    )
    monkeypatch.setattr(sys, "prefix", str(desktop.resources))
    monkeypatch.setattr(sys, "executable", str(desktop.resources / "backend"))
    stdlib = Path(sysconfig.get_path("stdlib"))
    monkeypatch.setattr(
        sys,
        "path",
        [str(stdlib), str(stdlib / "lib-dynload")],
    )
    manager = DependencyUpgradeManager(desktop.loader)
    manager.root = desktop.upload.parent / "working/dependency_upgrades"
    operation = await manager.stage(desktop.upload)
    stored = manager._read(operation["id"])
    assert stored["dependency_context"]["immutable_identity"]
    policy = desktop.support / "frozen_policy-1.0.dist-info/METADATA"
    policy.write_bytes(
        policy.read_bytes() + b"Summary: same-version policy changed\n",
    )
    before = _contents(desktop.target)
    resources = _contents(desktop.resources)
    binding = await manager._binding(desktop.upload)
    actual = await binding.backend.audit_dependencies(binding.description)
    assert actual.distributions["frozen-local-sdk"]["version"] == "1.0"
    assert "frozen-policy" not in actual.distributions
    assert actual.to_mapping() == stored["environment"]

    applied = await manager.apply(operation["id"])

    assert applied["status"] == "failed"
    assert applied["error"]["code"] == "DEPENDENCY_PLAN_STALE"
    assert _contents(desktop.target) == before
    assert _contents(desktop.resources) == resources
    assert not (desktop.target / "frozen_local_sdk-2.0.dist-info").exists()


@pytest.mark.asyncio
async def test_frozen_worker_only_module_alias_cannot_be_overlaid(
    desktop,
    monkeypatch,
):
    from qwenpaw.plugins.dependency_gate import SharedDependencyError

    bundled = wheel(
        desktop.payload.path.parent,
        name="frozen-private-sdk",
        version="1.0",
        module="frozen_local_sdk",
    )
    with zipfile.ZipFile(bundled.path) as archive:
        archive.extractall(desktop.support)
    monkeypatch.setattr(sys, "prefix", str(desktop.resources))
    monkeypatch.setattr(sys, "executable", str(desktop.resources / "backend"))
    stdlib = Path(sysconfig.get_path("stdlib"))
    monkeypatch.setattr(
        sys,
        "path",
        [str(stdlib), str(stdlib / "lib-dynload")],
    )
    before = _contents(desktop.resources)

    with pytest.raises(SharedDependencyError) as refused:
        await desktop.loader._prepare_python_dependencies(
            desktop.requirements,
            "ordinary-plugin",
        )

    assert refused.value.reason_code == "DEPENDENCY_RESTART_REQUIRED"
    assert not list(desktop.target.iterdir())
    assert _contents(desktop.resources) == before
    assert "frozen-private-sdk" not in desktop.host_audits[-1].distributions
    assert "frozen_local_sdk" not in sys.modules
