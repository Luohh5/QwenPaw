# -*- coding: utf-8 -*-
# pylint: disable=missing-function-docstring,duplicate-code
"""Installed candidates run in isolated hosts with actual installed SDKs."""

import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("channel", ["feishu", "dingtalk"])
def test_installed_channel_process(channel, tmp_path):
    suite = (
        Path(__file__).resolve().parents[3]
        / "fixtures/channel_plugins/installation_process_suite.py"
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
            "QWENPAW_B5_CHANNEL": channel,
            "QWENPAW_WORKING_DIR": str(tmp_path / "host"),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
