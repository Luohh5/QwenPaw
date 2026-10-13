# -*- coding: utf-8 -*-
"""Run independent C5 transaction evidence without global SDK substitutions."""

import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("channel", ["feishu", "dingtalk"])
def test_c5_transactions(tmp_path, channel):
    suite = (
        Path(__file__).resolve().parents[3]
        / "fixtures/channel_plugins/c5_transactions_suite.py"
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
            "QWENPAW_C5_CHANNEL": channel,
            "QWENPAW_WORKING_DIR": str(tmp_path / channel),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
