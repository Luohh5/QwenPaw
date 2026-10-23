# -*- coding: utf-8 -*-
"""Published core and Harness artifacts keep vendor runtimes optional."""

from __future__ import annotations

import os
import subprocess
import sys
import shutil
import zipfile
from pathlib import Path

import pytest
from packaging.requirements import Requirement


REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session", name="core_wheel")
def _build_core_wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build the actual wheel once with the vendor-free core interpreter."""
    root = tmp_path_factory.mktemp("harness-core-wheel")
    source = root / "source"
    source.mkdir()
    for name in ("pyproject.toml", "setup.py", "README.md", "LICENSE"):
        shutil.copy2(REPO_ROOT / name, source / name)
    ignored = shutil.ignore_patterns(
        "build",
        "dist",
        "*.egg-info",
        "__pycache__",
        "*.pyc",
        "node_modules",
    )
    for relative in (
        "src",
        "packages/qwenpawmail-mcp/src",
        "plugins/hub",
    ):
        shutil.copytree(
            REPO_ROOT / relative,
            source / relative,
            ignore=ignored,
        )
    dist = root / "dist"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--no-isolation",
            "--outdir",
            str(dist),
            str(source),
        ],
        cwd=source,
        check=True,
        capture_output=True,
        text=True,
    )
    wheels = list(dist.glob("qwenpaw-*.whl"))
    assert len(wheels) == 1
    return wheels[0]


def test_core_wheel_has_no_vendor_runtime(core_wheel: Path) -> None:
    """The release wheel must omit old adapters and vendor payloads."""
    with zipfile.ZipFile(core_wheel) as archive:
        names = set(archive.namelist())
        metadata_name = next(
            name for name in names if name.endswith(".dist-info/METADATA")
        )
        metadata = archive.read(metadata_name).decode()

    assert not any(name.startswith("qwenpaw/harnesses/") for name in names)
    assert not any(
        name.startswith(("qoder_agent_sdk/", "codex_cli_bin/"))
        for name in names
    )
    assert "qwenpaw/plugins/harnesses.py" in names
    assert "qwenpaw/plugins/frontend/index.js" in names
    assert "qwenpaw/plugins/templates/hello/plugin.json" in names
    assert "qwenpawmail_mcp/__init__.py" in names
    for hub in ("qwenpaw-hub", "clawhub-hub", "modelscope-hub", "aliyun-hub"):
        assert any(name.endswith(f"/{hub}/plugin.json") for name in names)
        assert any(name.endswith(f"/{hub}/plugin.py") for name in names)

    for line in metadata.splitlines():
        if not line.startswith("Requires-Dist: "):
            continue
        requirement = Requirement(line.removeprefix("Requires-Dist: "))
        if requirement.marker and not requirement.marker.evaluate(
            {"extra": "full"},
        ):
            continue
        assert requirement.name.lower().replace("_", "-") not in {
            "openai-codex",
            "qoder-agent-sdk",
        }
        if requirement.name.lower() == "qwenpaw":
            assert not {"codex", "qoder"} & requirement.extras


def test_minimal_core_wheel_imports_without_vendor_sdks(
    core_wheel: Path,
    tmp_path: Path,
) -> None:
    """Install the produced wheel without deps and import the real app."""
    target = tmp_path / "wheel-site"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-index",
            "--no-deps",
            "--target",
            str(target),
            str(core_wheel),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    script = "\n".join(
        [
            "import importlib.util, pathlib, qwenpaw, sys",
            "from qwenpaw.app._app import app",
            "assert pathlib.Path(qwenpaw.__file__).resolve().is_relative_to("
            "pathlib.Path(sys.argv[1]))",
            "assert app is not None",
            "assert importlib.util.find_spec('qoder_agent_sdk') is None",
            "assert importlib.util.find_spec('codex_cli_bin') is None",
        ],
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(target)
    subprocess.run(
        [sys.executable, "-c", script, str(target)],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
