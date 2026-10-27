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
        expected = ["chosen-python", "-I", "-m", "pip", "--version"]
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
        assert command[:5] == ["chosen-python", "-I", "-m", "pip", "wheel"]
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
