# -*- coding: utf-8 -*-
"""Run B1 shared contracts outside tests/conftest.py SDK mocks."""

import os
from pathlib import Path
import subprocess
import sys


def test_channel_process_contract(tmp_path):
    """A new interpreter collects the import guard before production APIs."""
    suite = (
        Path(__file__).resolve().parents[3]
        / "fixtures/channel_plugins/process_contract_suite.py"
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
            "QWENPAW_WORKING_DIR": str(tmp_path / "qwenpaw-b1"),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
