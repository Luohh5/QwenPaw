# -*- coding: utf-8 -*-
"""Run standard Channel contracts outside repository-wide SDK mocks."""

import os
from pathlib import Path
import subprocess
import sys


def test_channel_extension_contract(tmp_path):
    suite = (
        Path(__file__).resolve().parents[3]
        / "fixtures/channel_plugins/extension_contract_suite.py"
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
