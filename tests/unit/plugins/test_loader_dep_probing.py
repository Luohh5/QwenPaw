# -*- coding: utf-8 -*-
"""Tests for PluginLoader dependency probing and uv discovery.

Covers the static helpers that decide whether a requirement is already
satisfied (metadata probe + import probe) and locate the ``uv`` binary,
which previously had no unit coverage.
"""

# pylint: disable=protected-access,redefined-outer-name,unused-argument,use-implicit-booleaness-not-comparison  # noqa: E501
from __future__ import annotations

from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import subprocess

import pytest

from qwenpaw.plugins.loader import PluginLoader

# ---------------------------------------------------------------------------
# _is_requirement_satisfied
# ---------------------------------------------------------------------------


class TestIsRequirementSatisfied:
    def test_installed_package_no_specifier(self):
        from packaging.requirements import Requirement

        # ``packaging`` is a hard dependency, always present.
        req = Requirement("packaging")
        assert PluginLoader._is_requirement_satisfied(req) is True

    def test_installed_package_satisfying_specifier(self):
        from packaging.requirements import Requirement

        req = Requirement("packaging>=1.0")
        assert PluginLoader._is_requirement_satisfied(req) is True

    def test_installed_package_violating_specifier(self):
        from packaging.requirements import Requirement

        # Require an impossibly high version of a real package.
        req = Requirement("packaging>=9999.0")
        assert PluginLoader._is_requirement_satisfied(req) is False

    def test_missing_package_no_specifier(self):
        from packaging.requirements import Requirement

        req = Requirement("this-package-does-not-exist-xyz")
        assert PluginLoader._is_requirement_satisfied(req) is False

    def test_missing_package_with_specifier(self):
        from packaging.requirements import Requirement

        req = Requirement("this-package-does-not-exist-xyz>=1.0")
        assert PluginLoader._is_requirement_satisfied(req) is False

    def test_frozen_bundled_via_import_probe(self):
        """A package present in the env but lacking .dist-info is found.

        The import probe covers frozen desktop builds where dist-info is
        stripped (issue #5209). We simulate by checking a module that is
        importable in this test process.
        """
        from packaging.requirements import Requirement

        # ``json`` is a stdlib module with no dist-info.
        req = Requirement("json")
        assert PluginLoader._is_requirement_satisfied(req) is True


# ---------------------------------------------------------------------------
# _find_unsatisfied_dependencies
# ---------------------------------------------------------------------------


class TestFindUnsatisfiedDependencies:
    def test_missing_requirements_file_returns_empty(self, tmp_path):
        result = PluginLoader._find_unsatisfied_dependencies(
            tmp_path / "requirements.txt",
        )
        assert result == []

    def test_all_satisfied_returns_empty(self, tmp_path):
        req_file = tmp_path / "requirements.txt"
        req_file.write_text("packaging\n", encoding="utf-8")
        result = PluginLoader._find_unsatisfied_dependencies(req_file)
        assert result == []

    def test_missing_package_reported(self, tmp_path):
        req_file = tmp_path / "requirements.txt"
        req_file.write_text(
            "this-package-does-not-exist-xyz>=1.0\n",
            encoding="utf-8",
        )
        result = PluginLoader._find_unsatisfied_dependencies(req_file)
        assert result == ["this-package-does-not-exist-xyz>=1.0"]

    def test_comments_and_blanks_skipped(self, tmp_path):
        req_file = tmp_path / "requirements.txt"
        req_file.write_text(
            "# a comment\n\npackaging\n   \n-r extra.txt\n",
            encoding="utf-8",
        )
        result = PluginLoader._find_unsatisfied_dependencies(req_file)
        assert result == []

    def test_option_lines_skipped(self, tmp_path):
        req_file = tmp_path / "requirements.txt"
        req_file.write_text(
            "--index-url https://example.com\npackaging\n",
            encoding="utf-8",
        )
        result = PluginLoader._find_unsatisfied_dependencies(req_file)
        assert result == []

    def test_malformed_line_skipped_not_raised(self, tmp_path):
        req_file = tmp_path / "requirements.txt"
        req_file.write_text(
            "not a valid requirement !!!\npackaging\n",
            encoding="utf-8",
        )
        result = PluginLoader._find_unsatisfied_dependencies(req_file)
        assert result == []

    def test_mixed_satisfied_and_missing(self, tmp_path):
        req_file = tmp_path / "requirements.txt"
        req_file.write_text(
            "packaging\nthis-missing-one-abc\n",
            encoding="utf-8",
        )
        result = PluginLoader._find_unsatisfied_dependencies(req_file)
        assert result == ["this-missing-one-abc"]


# ---------------------------------------------------------------------------
# _find_uv
# ---------------------------------------------------------------------------


class TestFindUv:
    def test_found_on_path(self, monkeypatch):
        monkeypatch.setattr(
            "qwenpaw.plugins.loader.shutil.which",
            lambda name: "/usr/local/bin/uv" if name == "uv" else None,
        )
        assert PluginLoader._find_uv() == "/usr/local/bin/uv"

    def test_not_on_path_falls_back_to_home(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "qwenpaw.plugins.loader.shutil.which",
            lambda name: None,
        )
        home = tmp_path / "home"
        (home / ".local" / "bin").mkdir(parents=True)
        uv_bin = home / ".local" / "bin" / "uv"
        uv_bin.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
        monkeypatch.delenv("LOCALAPPDATA", raising=False)
        assert PluginLoader._find_uv() == str(uv_bin)

    def test_not_found_anywhere_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "qwenpaw.plugins.loader.shutil.which",
            lambda name: None,
        )
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
        monkeypatch.delenv("LOCALAPPDATA", raising=False)
        assert PluginLoader._find_uv() is None

    def test_windows_localappdata_candidate(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "qwenpaw.plugins.loader.shutil.which",
            lambda name: None,
        )
        home = tmp_path / "home"
        home.mkdir()
        local_app = tmp_path / "localapp"
        uv_exe = local_app / "Programs" / "uv" / "uv.exe"
        uv_exe.parent.mkdir(parents=True)
        uv_exe.write_text("MZ", encoding="utf-8")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
        monkeypatch.setenv("LOCALAPPDATA", str(local_app))
        assert PluginLoader._find_uv() == str(uv_exe)


@pytest.mark.parametrize(
    ("tool", "version", "safe"),
    [
        ("pip", "26.2.1", False),
        ("pip", "26.2.2", True),
        ("uv", "0.13.3", False),
        ("uv", "0.13.4", True),
    ],
)
def test_dependency_installer_security_versions(
    monkeypatch,
    tool,
    version,
    safe,
):
    from qwenpaw.utils import runtime_environment

    calls = []

    def version_probe(command, **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            f"{tool} {version}\n",
            "",
        )

    monkeypatch.setattr(runtime_environment.subprocess, "run", version_probe)
    if tool == "pip":
        check = partial(
            runtime_environment.require_pip,
            "chosen-python",
            isolated=True,
        )
        expected = ["chosen-python", "-I", "-B", "-m", "pip", "--version"]
    else:
        check = partial(runtime_environment.require_uv, "chosen-uv")
        expected = ["chosen-uv", "--version"]
    if safe:
        check()
    else:
        with pytest.raises(RuntimeError):
            check()
    assert calls == [expected]


@pytest.mark.asyncio
async def test_old_installer_cannot_acquire_dependency_wheels(
    tmp_path,
    monkeypatch,
):
    """The controlled wheel path must reject before resolver execution."""
    loader = PluginLoader([tmp_path])
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("sample-sdk==1\n", encoding="utf-8")
    resolver = Mock()
    guard = Mock(side_effect=RuntimeError("Unsafe pip 26.2.1"))
    monkeypatch.setattr(loader, "_run_subprocess_with_streaming_log", resolver)
    monkeypatch.setattr("qwenpaw.plugins.loader.require_pip", guard)

    with pytest.raises(RuntimeError, match="Unsafe pip"):
        await loader._acquire_dependency_wheels(
            SimpleNamespace(),
            SimpleNamespace(python_executable="chosen-python"),
            SimpleNamespace(distributions={}, marker_environment={}),
            tmp_path / "site",
            "sample-plugin",
            requirements,
        )
    guard.assert_called_once_with("chosen-python", isolated=True)
    resolver.assert_not_called()
    assert requirements.read_text(encoding="utf-8") == "sample-sdk==1\n"


@pytest.mark.asyncio
async def test_controlled_wheels_use_certifi_floor_without_editing_source(
    tmp_path,
    monkeypatch,
):
    from qwenpaw.utils.runtime_environment import CERTIFI_REQUIREMENT

    loader = PluginLoader([tmp_path])
    requirements = tmp_path / "requirements.txt"
    source_text = "--no-index\nsample-sdk==1\n"
    requirements.write_text(source_text, encoding="utf-8")
    events = []

    def safe_installer(python, *, isolated):
        assert python == "chosen-python" and isolated is True
        events.append("checked")
        return True

    def inspect_resolution(command, **_kwargs):
        events.append("wheel")
        assert events == ["checked", "wheel"]
        assert command[:6] == [
            "chosen-python",
            "-I",
            "-B",
            "-m",
            "pip",
            "wheel",
        ]
        assert "install" not in command
        effective = Path(command[command.index("-r") + 1])
        assert effective != requirements
        assert effective.read_text(encoding="utf-8").splitlines() == [
            "--no-index",
            "sample-sdk==1",
            "",
            CERTIFI_REQUIREMENT,
        ]
        assert requirements.read_text(encoding="utf-8") == source_text
        return subprocess.CompletedProcess(command, 0, "", "")

    async def inspect_gate(_process, _spec, **options):
        events.append("gate")
        assert options["requirements_file"] != requirements
        assert (
            CERTIFI_REQUIREMENT
            in options["requirements_file"]
            .read_text(
                encoding="utf-8",
            )
            .splitlines()
        )
        assert options["expected_snapshot"].distributions == {}
        assert options["wheels"] == ()

    monkeypatch.setattr("qwenpaw.plugins.loader.require_pip", safe_installer)
    monkeypatch.setattr(
        loader,
        "_run_subprocess_with_streaming_log",
        inspect_resolution,
    )
    monkeypatch.setattr(
        "qwenpaw.plugins.dependency_gate.prepare_shared_dependencies",
        inspect_gate,
    )
    await loader._acquire_dependency_wheels(
        SimpleNamespace(),
        SimpleNamespace(python_executable="chosen-python", code_root=tmp_path),
        SimpleNamespace(distributions={}, marker_environment={}),
        tmp_path / "site",
        "sample-plugin",
        requirements,
    )
    assert events == ["checked", "wheel", "gate"]
    assert requirements.read_text(encoding="utf-8") == source_text


@pytest.mark.asyncio
async def test_certifi_floor_refuses_old_immutable_provider_before_download(
    tmp_path,
    monkeypatch,
):
    loader = PluginLoader([tmp_path])
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("sample-sdk==1\n", encoding="utf-8")
    certifi = {"version": "2026.7.22", "requires_dist": ()}
    refuse = Mock(
        side_effect=RuntimeError("protected certifi needs desktop update"),
    )
    context = SimpleNamespace(
        immutable_providers=lambda: {"certifi": certifi},
        refuse_immutable_provider=refuse,
    )
    resolver, guard = Mock(), Mock()
    monkeypatch.setattr(loader, "_run_subprocess_with_streaming_log", resolver)
    monkeypatch.setattr("qwenpaw.plugins.loader.require_pip", guard)

    with pytest.raises(RuntimeError, match="protected certifi"):
        await loader._acquire_dependency_wheels(
            SimpleNamespace(),
            SimpleNamespace(python_executable="chosen-python"),
            SimpleNamespace(
                distributions={"certifi": certifi},
                marker_environment={},
            ),
            tmp_path / "site",
            "sample-plugin",
            requirements,
            dependency_context=context,
        )
    refuse.assert_called_once_with("certifi", certifi)
    guard.assert_not_called()
    resolver.assert_not_called()
    assert requirements.read_text(encoding="utf-8") == "sample-sdk==1\n"


@pytest.mark.asyncio
async def test_immutable_installer_without_pip_never_runs_ensurepip(
    tmp_path,
    monkeypatch,
):
    loader = PluginLoader([tmp_path])
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("sample-sdk==1\n", encoding="utf-8")
    resolver = Mock()
    guard = Mock(return_value=False)
    context = SimpleNamespace(
        immutable_providers=lambda: {},
        runtime_identity={"protocol": 1},
    )
    monkeypatch.setattr(loader, "_run_subprocess_with_streaming_log", resolver)
    monkeypatch.setattr("qwenpaw.plugins.loader.require_pip", guard)

    with pytest.raises(RuntimeError, match="bundled pip is missing"):
        await loader._acquire_dependency_wheels(
            SimpleNamespace(),
            SimpleNamespace(python_executable="chosen-python"),
            SimpleNamespace(distributions={}, marker_environment={}),
            tmp_path / "site",
            "sample-plugin",
            requirements,
            dependency_context=context,
        )
    guard.assert_called_once_with("chosen-python", isolated=True)
    resolver.assert_not_called()


@pytest.mark.asyncio
async def test_mutable_missing_pip_is_rechecked_and_reaudited_after_bootstrap(
    tmp_path,
    monkeypatch,
):
    loader = PluginLoader([tmp_path])
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("sample-sdk==1\n", encoding="utf-8")
    original = SimpleNamespace(distributions={}, marker_environment={})
    refreshed = SimpleNamespace(
        distributions={"pip": {"version": "26.2.2"}},
        marker_environment={},
    )
    events = []

    def check(python, *, isolated):
        assert python == "chosen-python" and isolated is True
        events.append("check")
        return events.count("check") > 1

    def run(command, **_kwargs):
        if command[-1] == "ensurepip":
            events.append("bootstrap")
            assert command == ["chosen-python", "-I", "-m", "ensurepip"]
        else:
            events.append("wheel")
            assert "wheel" in command and "install" not in command
            assert (
                Path(command[command.index("--constraint") + 1]).read_text(
                    encoding="utf-8",
                )
                == "pip==26.2.2\n"
            )
        return subprocess.CompletedProcess(command, 0, "", "")

    async def audit(_spec):
        events.append("audit")
        return SimpleNamespace(snapshot=refreshed)

    async def gate(_process, _spec, **options):
        events.append("gate")
        assert options["expected_snapshot"] is refreshed

    monkeypatch.setattr("qwenpaw.plugins.loader.require_pip", check)
    monkeypatch.setattr(loader, "_run_subprocess_with_streaming_log", run)
    monkeypatch.setattr(
        "qwenpaw.plugins.dependency_gate.prepare_shared_dependencies",
        gate,
    )
    await loader._acquire_dependency_wheels(
        SimpleNamespace(audit=audit),
        SimpleNamespace(python_executable="chosen-python", code_root=tmp_path),
        original,
        tmp_path / "site",
        "sample-plugin",
        requirements,
    )
    assert events == ["check", "bootstrap", "audit", "check", "wheel", "gate"]
    assert requirements.read_text(encoding="utf-8") == "sample-sdk==1\n"


@pytest.mark.asyncio
async def test_async_loader_leases_before_loop_registry_publication(
    tmp_path,
    monkeypatch,
):
    import asyncio
    import threading
    from qwenpaw.plugins import dependency_maintenance, loader as loader_mod

    loop_thread = threading.get_ident()
    events = []
    started, release = threading.Event(), threading.Event()
    runtime = SimpleNamespace(
        python_executable="chosen-python",
        support_root=tmp_path,
        dependency_paths=(tmp_path / "site",),
        dependency_install_target=tmp_path / "site",
        runtime_identity={"protocol": 1},
    )
    original_registry = loader_mod.PluginRegistry

    def lease(paths):
        assert threading.get_ident() != loop_thread
        assert paths == runtime.dependency_paths
        events.append("lease")
        started.set()
        assert release.wait(3)

    class Registry(original_registry):
        _instance = None

        def __init__(self):
            assert threading.get_ident() == loop_thread
            assert events == ["lease"]
            events.append("registry")
            super().__init__()

        def configure_channel_runtime(self, settings):
            assert threading.get_ident() == loop_thread
            assert settings is runtime
            events.append("publish")

    monkeypatch.setattr(dependency_maintenance, "lease_environment", lease)
    monkeypatch.setattr(loader_mod, "PluginRegistry", Registry)
    creation = asyncio.create_task(
        loader_mod.PluginLoader.create([tmp_path], channel_runtime=runtime),
    )
    try:
        assert await asyncio.to_thread(started.wait, 2)
        assert events == ["lease"]
        assert not creation.done()
    finally:
        release.set()
    result = await asyncio.wait_for(creation, 5)
    assert isinstance(result.registry, Registry)
    assert events == ["lease", "registry", "publish"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["identity", "origin"])
async def test_dependency_import_resolution_rejects_changed_module(
    tmp_path,
    monkeypatch,
    change,
):
    import asyncio
    import sys
    import threading
    from types import ModuleType
    from qwenpaw.plugins import dependency_transaction as transaction

    name = "_qwenpaw_stale_dependency_test"
    source = str(tmp_path / "sdk.py")
    module = ModuleType(name)
    module.__file__ = source
    monkeypatch.setitem(sys.modules, name, module)
    original = transaction._imported_changes
    started, release = threading.Event(), threading.Event()
    loop_thread = threading.get_ident()

    def resolve(payload, modules):
        assert threading.get_ident() != loop_thread
        started.set()
        assert release.wait(3)
        return original(payload, modules)

    monkeypatch.setattr(transaction, "_imported_changes", resolve)
    probe = asyncio.create_task(
        transaction.imported_changes_async({"old": {source: {}}}),
    )
    try:
        assert await asyncio.to_thread(started.wait, 2)
        if change == "origin":
            module.__file__ = str(tmp_path / "replacement.py")
        else:
            replacement = ModuleType(name)
            replacement.__file__ = source
            sys.modules[name] = replacement
    finally:
        release.set()
    with pytest.raises(RuntimeError, match="DEPENDENCY_PLAN_STALE"):
        await asyncio.wait_for(probe, 5)


@pytest.mark.asyncio
async def test_async_dependency_targets_reject_changed_host_paths(
    tmp_path,
    monkeypatch,
):
    import sys
    from qwenpaw.plugins.python_backend import PythonBackend

    backend = PythonBackend(SimpleNamespace())
    admission = await backend.dependency_targets_async(None)
    monkeypatch.setattr(sys, "path", [*sys.path, str(tmp_path / "new-site")])
    with pytest.raises(RuntimeError, match="IDENTITY_CHANGED"):
        admission.validate()


@pytest.mark.asyncio
@pytest.mark.parametrize("stale", [False, True])
async def test_frozen_dependency_path_preparation_and_loop_publication(
    tmp_path,
    monkeypatch,
    stale,
):
    import asyncio
    import os
    import sys
    import threading
    from qwenpaw.plugins import loader as loader_mod

    root = tmp_path / "runtime"
    bucket = root / "python"
    target = bucket / "bundle-new" / "site"
    old = bucket / "bundle-old" / "site"
    paths = [str(old), str(bucket / "site"), str(tmp_path / "host")]
    monkeypatch.setattr(sys, "path", paths[:])
    monkeypatch.setenv("QWENPAW_PLUGIN_SITE", "before")
    monkeypatch.setattr(loader_mod, "_is_frozen", lambda: True)
    monkeypatch.setattr(loader_mod, "_plugin_runtime_dir", lambda: root)
    loop_thread = threading.get_ident()
    started, release = threading.Event(), threading.Event()
    prepare = loader_mod._prepare_plugin_site_path
    publish = loader_mod._publish_plugin_site_path
    publications = []

    def resolve(site, snapshot):
        assert threading.get_ident() != loop_thread
        assert tuple(snapshot) == tuple(paths)
        result = prepare(site, snapshot)
        assert sys.path == paths
        assert os.environ["QWENPAW_PLUGIN_SITE"] == "before"
        started.set()
        assert release.wait(3)
        return result

    def commit(prepared):
        assert threading.get_ident() == loop_thread
        publications.append(prepared)
        publish(prepared)

    monkeypatch.setattr(loader_mod, "_prepare_plugin_site_path", resolve)
    monkeypatch.setattr(loader_mod, "_publish_plugin_site_path", commit)
    pending = asyncio.create_task(
        loader_mod._ensure_plugin_site_on_path_async(target),
    )
    try:
        assert await asyncio.to_thread(started.wait, 2)
        if stale:
            sys.path.append(str(tmp_path / "concurrent-site"))
    finally:
        release.set()
    if stale:
        with pytest.raises(RuntimeError, match="IDENTITY_CHANGED"):
            await asyncio.wait_for(pending, 5)
        assert not publications
        assert os.environ["QWENPAW_PLUGIN_SITE"] == "before"
    else:
        await asyncio.wait_for(pending, 5)
        assert len(publications) == 1
        assert sys.path == [str(target), str(tmp_path / "host")]
        assert os.environ["QWENPAW_PLUGIN_SITE"] == str(target)
