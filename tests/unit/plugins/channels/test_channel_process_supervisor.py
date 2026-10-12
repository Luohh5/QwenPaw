# -*- coding: utf-8 -*-
"""Run B2 subprocess tests outside repository-wide SDK replacements."""

import os
from pathlib import Path
import subprocess
import sys


def test_channel_process_supervisor(tmp_path):
    """Collect supervisor tests in a new isolated interpreter."""
    suite = (
        Path(__file__).resolve().parents[3]
        / "fixtures/channel_plugins/process_supervisor_suite.py"
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
            "QWENPAW_WORKING_DIR": str(tmp_path / "qwenpaw-b2"),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
