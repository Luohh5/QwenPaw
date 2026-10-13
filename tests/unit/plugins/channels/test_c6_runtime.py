# -*- coding: utf-8 -*-
"""Run finite C6 runtime acceptance without global SDK substitutes."""

import os
from pathlib import Path
import subprocess
import sys


def test_c6_runtime_acceptance(tmp_path):
    suite = (
        Path(__file__).resolve().parents[3]
        / "fixtures/channel_plugins/c6_runtime_suite.py"
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
            "QWENPAW_RUNNING_IN_CONTAINER": "true",
            "QWENPAW_SECRET_DIR": str(tmp_path / "secrets"),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
