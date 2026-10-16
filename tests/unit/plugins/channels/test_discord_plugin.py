# -*- coding: utf-8 -*-
"""Run Discord contracts outside repository-wide SDK mocks."""

import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    "suite_name",
    [
        "discord_legacy_suite.py",
        "discord_suite.py",
        "discord_media_suite.py",
        "discord_stream_suite.py",
        "discord_platform_suite.py",
        "discord_reliability_suite.py",
        "discord_actual_agent_suite.py",
    ],
)
def test_discord_plugin(tmp_path, suite_name):
    suite = (
        Path(__file__).resolve().parents[3]
        / "fixtures/channel_plugins"
        / suite_name
    )
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-m",
            "pytest",
            "--confcutdir",
            str(suite.parent),
            str(suite),
            "-q",
        ],
        env={
            **os.environ,
            "QWENPAW_WORKING_DIR": str(tmp_path / "host"),
            "QWENPAW_SECRET_DIR": str(tmp_path / "secrets"),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
