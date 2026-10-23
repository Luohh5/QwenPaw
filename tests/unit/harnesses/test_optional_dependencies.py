# -*- coding: utf-8 -*-
"""Tests for optional third-party agent dependencies."""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path


def test_codex_and_qoder_sdks_are_optional_and_in_full() -> None:
    pyproject_path = Path(__file__).parents[3] / "pyproject.toml"
    data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    project = data["project"]
    dependencies = project["dependencies"]
    extras = project["optional-dependencies"]

    assert not any("openai-codex" in item for item in dependencies)
    assert not any("qoder-agent-sdk" in item for item in dependencies)
    assert extras["codex"] == ["openai-codex==0.144.4"]
    assert extras["qoder"] == ["qoder-agent-sdk==1.0.9"]
    assert extras["full"] == [
        "qwenpaw[qwenpaw-data,hub,local,whisper,codex,qoder]",
    ]


def test_core_app_imports_without_optional_vendor_sdks() -> None:
    code = (
        "import sys; "
        'sys.modules["qoder_agent_sdk"] = None; '
        'sys.modules["codex_cli_bin"] = None; '
        "import qwenpaw.app.workspace.workspace; "
        'assert not any(name.startswith("qwenpaw.harnesses.qoder") '
        'or name.startswith("qwenpaw.harnesses.codex") '
        "for name in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
