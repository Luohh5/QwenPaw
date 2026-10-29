# -*- coding: utf-8 -*-
import os
from pathlib import Path
import pytest

_WINDOWS = os.name == "nt"


class TestKernelIdentity:
    @pytest.mark.skipif(not _WINDOWS, reason="native Windows path resolution")
    def test_kernel_spelling_needs_no_directory_enumeration(
        self,
        tmp_path,
        monkeypatch,
    ):
        from qwenpaw.plugins import dependency_maintenance as maintenance

        site = tmp_path / "MixedCase" / "CaseSite"
        site.mkdir(parents=True)
        package = site / "Package.py"
        package.write_text("VALUE = 1")
        long_file = site / ("segment" * 25) / "Package.py"
        long_file.parent.mkdir()
        long_file.write_text("VALUE = 2")
        extended = Path("\\\\?\\" + str(site))
        alias = site.parent / "casesite"

        def no_enumeration(*_args, **_kwargs):
            pytest.fail("known Windows paths must not enumerate ancestors")

        with monkeypatch.context() as patcher:
            patcher.setattr(os, "scandir", no_enumeration)
            actual = maintenance.canonical(site)
            assert actual.parts[-2:] == ("MixedCase", "CaseSite")
            assert maintenance.canonical(package).name == "Package.py"
            assert maintenance.canonical(long_file).samefile(long_file)
            assert maintenance.canonical(site / "New" / "Sdk").parts == (
                *actual.parts,
                "New",
                "Sdk",
            )
            resolved = maintenance.canonical(extended)
            assert resolved.samefile(site)
            assert maintenance.canonical(resolved).parts == resolved.parts
            if alias.exists():
                assert maintenance.canonical(alias).parts == actual.parts

    @pytest.mark.skipif(not _WINDOWS, reason="native Windows junction")
    def test_junction_preserves_target_identity(self, tmp_path):
        import subprocess
        from qwenpaw.plugins import dependency_maintenance as maintenance

        target, junction = tmp_path / "CaseTarget", tmp_path / "junction"
        target.mkdir()
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(junction), str(target)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (result.stdout, result.stderr)
        try:
            assert maintenance.canonical(junction).parts == (
                maintenance.canonical(target).parts
            )
        finally:
            junction.rmdir()

    @pytest.mark.skipif(not _WINDOWS, reason="native Windows case sensitivity")
    def test_case_sensitive_directory_keeps_distinct_identities(
        self,
        tmp_path,
    ):
        import subprocess
        from qwenpaw.plugins import dependency_maintenance as maintenance
        from qwenpaw.plugins.install_lock import environment_install_lock_path

        parent = tmp_path / "case-sensitive"
        parent.mkdir()
        result = subprocess.run(
            ["fsutil", "file", "setCaseSensitiveInfo", str(parent), "enable"],
            capture_output=True,
            text=True,
        )
        if result.returncode:
            pytest.skip(
                "runner cannot enable directory case sensitivity: "
                + result.stderr,
            )
        upper, lower = parent / "Dir", parent / "dir"
        upper.mkdir()
        lower.mkdir()
        assert not upper.samefile(lower)
        assert (
            maintenance.canonical(upper).parts
            != maintenance.canonical(lower).parts
        )
        assert environment_install_lock_path(
            upper,
        ) != environment_install_lock_path(lower)
