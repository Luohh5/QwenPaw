# -*- coding: utf-8 -*-
"""Source-level deployment command contracts; no native builds or installs."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[3]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_c7_docker_always_installs_full_channel_dependencies() -> None:
    docker = _source("deploy/Dockerfile")
    entrypoint = _source("deploy/entrypoint.sh")
    assert "QWENPAW_CHANNEL_PRESET" not in docker
    assert (
        "uv pip install --python /app/venv/bin/python --no-cache-dir "
        '"$1[channels]"' in docker
    )
    assert "channel-preset.json" not in docker
    assert "channel-preset.json" not in entrypoint


@pytest.mark.parametrize(
    ("inherited_preset", "expected_preset"),
    [
        (None, "light"),
        ("", "light"),
        ("light", "light"),
        ("channels-full", "channels-full"),
    ],
)
def test_c7_docker_init_receives_selected_channel_preset(
    tmp_path: Path,
    inherited_preset: str | None,
    expected_preset: str,
) -> None:
    """Run the real entrypoint through init, recording its child environment."""
    entrypoint = _source("deploy/entrypoint.sh")
    before_server = entrypoint[: entrypoint.index("export QWENPAW_PORT=")]
    cli = tmp_path / "bin/qwenpaw"
    cli.parent.mkdir()
    marker = tmp_path / "init-preset"
    cli.write_text(
        "#!/bin/sh\n"
        '[ "$*" = "init --defaults --accept-security" ] || exit 97\n'
        'printf "%s\\n" "${QWENPAW_CHANNEL_PRESET-unset}" > "$C7_MARKER"\n',
        encoding="utf-8",
    )
    cli.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{cli.parent}{os.pathsep}{os.environ['PATH']}",
        "QWENPAW_WORKING_DIR": str(tmp_path / "working"),
        "C7_MARKER": str(marker),
    }
    env.pop("QWENPAW_CHANNEL_PRESET", None)
    if inherited_preset is not None:
        env["QWENPAW_CHANNEL_PRESET"] = inherited_preset
    result = subprocess.run(
        ["sh", "-c", before_server],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert marker.read_text(encoding="utf-8") == expected_preset + "\n"


def test_c7_installers_apply_exact_extras_offline() -> None:
    for path in (
        "scripts/install.sh",
        "scripts/install.ps1",
        "scripts/install.bat",
    ):
        content = _source(path)
        assert "channels" in content and "full" in content
        assert "plugins preset apply channels-full --offline" in content


@pytest.mark.parametrize(
    ("extras", "selected"),
    [
        ("CHANNELS", True),
        ("Full", True),
        ("dev, ChAnNeLs", True),
        ("whisper,full,dev", True),
        ("dev,whisper", False),
        ("notchannels,fully", False),
    ],
)
def test_c7_bash_installer_selects_exact_extras_case_insensitively(
    tmp_path: Path,
    extras: str,
    selected: bool,
) -> None:
    """Execute only the installer's policy block with a recording CLI."""
    content = _source("scripts/install.sh")
    start = content.index("# Verify the CLI entry point exists")
    end = content.index('info "QwenPaw installed successfully"', start)
    marker = tmp_path / "preset-argv"
    preparation = tmp_path / "prepare-argv"
    cli = tmp_path / "bin/qwenpaw"
    cli.parent.mkdir()
    cli.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        '  "--help") printf "  plugins\\n" ;;\n'
        '  "plugins --help") printf "  prepare-upgrade\\n" ;;\n'
        '  "plugins preset apply channels-full --offline")\n'
        '    printf "%s\\n" "$*" > "$C7_MARKER" ;;\n'
        '  "plugins prepare-upgrade")\n'
        '    printf "%s\\n" "$*" >> "$C7_PREPARATION" ;;\n'
        "  *) exit 97 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    cli.chmod(0o755)
    result = subprocess.run(
        ["bash", "-c", content[start:end]],
        env={
            **os.environ,
            "EXTRAS": extras,
            "QWENPAW_VENV": str(tmp_path),
            "C7_MARKER": str(marker),
            "C7_PREPARATION": str(preparation),
        },
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert marker.exists() is selected
    assert preparation.read_text(encoding="utf-8") == (
        "plugins prepare-upgrade\n"
    )
    if selected:
        assert marker.read_text(encoding="utf-8") == (
            "plugins preset apply channels-full --offline\n"
        )


@pytest.mark.parametrize("failed_help", ["--help", "plugins --help"])
def test_c7_installer_does_not_ignore_cli_help_failure(tmp_path, failed_help):
    content = _source("scripts/install.sh")
    start = content.index("# Verify the CLI entry point exists")
    end = content.index('info "QwenPaw installed successfully"', start)
    cli = tmp_path / "bin/qwenpaw"
    cli.parent.mkdir()
    marker = tmp_path / "prepared"
    cli.write_text(
        "#!/bin/sh\n"
        '[ "$*" != "$C7_FAILED_HELP" ] || exit 37\n'
        'case "$*" in\n'
        '  "--help") printf "  plugins\\n" ;;\n'
        '  "plugins --help") printf "  prepare-upgrade\\n" ;;\n'
        '  "plugins prepare-upgrade") touch "$C7_PREPARED" ;;\n'
        "  *) exit 97 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    cli.chmod(0o755)
    result = subprocess.run(
        ["bash", "-ec", content[start:end]],
        env={
            **os.environ,
            "EXTRAS": "",
            "QWENPAW_VENV": str(tmp_path),
            "C7_FAILED_HELP": failed_help,
            "C7_PREPARED": str(marker),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 37, result.stdout + result.stderr
    assert not marker.exists()


def test_c7_desktop_resource_and_launcher_command_paths() -> None:
    config = json.loads(_source("console/src-tauri/tauri.conf.json"))
    assert "binaries/channel-runtime" in config["bundle"]["resources"]
    launcher = _source("console/src-tauri/src/channel_runtime.rs")
    assert 'root.join("runtime.json")' in launcher
    assert 'root.join("site")' in launcher
    for key in (
        "QWENPAW_CHANNEL_RUNTIME_MANIFEST",
        "QWENPAW_CHANNEL_SUPPORT_ROOT",
        "QWENPAW_CHANNEL_DEPENDENCY_PATHS",
    ):
        assert key in launcher


def test_c7_release_builds_resolve_and_stage_target_child() -> None:
    for path in (
        "scripts/pack-tauri/build_pyinstaller.sh",
        "scripts/pack-tauri/build_pyinstaller.ps1",
    ):
        content = _source(path)
        assert "setuptools.build_meta" in content
        assert "[channels]" in content
        assert "pip download --only-binary=:all:" in content
        assert "stage_channel_runtime.py" in content
        assert "--python" in content
        assert "--wheel" in content
        assert "--resource-dir" in content
        assert "--wheelhouse" in content
