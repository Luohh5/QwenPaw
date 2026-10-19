# -*- coding: utf-8 -*-
"""Run host-state filesystem regressions outside repository SDK mocks."""

import os
from pathlib import Path
import subprocess
import sys


def test_windows_host_state_filesystem_contract(tmp_path):
    suite = (
        Path(__file__).resolve().parents[3]
        / "fixtures/channel_plugins/windows_host_state_suite.py"
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
            "QWENPAW_BACKUP_DIR": str(tmp_path / "backups"),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
