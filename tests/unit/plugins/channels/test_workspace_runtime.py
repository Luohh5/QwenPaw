# -*- coding: utf-8 -*-
"""C2 runtime contracts use isolated hosts without global SDK mocks."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("channel", ["feishu", "dingtalk"])
def test_real_workspace_channel_process(channel, tmp_path):
    suite = (
        Path(__file__).resolve().parents[3]
        / "fixtures/channel_plugins/workspace_runtime_suite.py"
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
            "QWENPAW_C2_CHANNEL": channel,
            "QWENPAW_WORKING_DIR": str(tmp_path / "host"),
        },
        capture_output=True,
        text=True,
        check=False,
        # C4 adds actual metadata audit and reaped Driver probe per fixture.
        # Feishu full suite takes 128s; individual runtime limits unchanged.
        timeout=300,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_workspace_queue_admission(tmp_path):
    suite = (
        Path(__file__).resolve().parents[3]
        / "fixtures/channel_plugins/workspace_admission_suite.py"
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
        env={**os.environ, "QWENPAW_WORKING_DIR": str(tmp_path / "host")},
        capture_output=True,
        text=True,
        check=False,
        timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr
