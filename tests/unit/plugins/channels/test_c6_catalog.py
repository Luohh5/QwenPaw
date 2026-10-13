# -*- coding: utf-8 -*-
"""Run independent C6 static host contracts outside global SDK mocks."""

import os
from pathlib import Path
import subprocess
import sys


def test_c6_static_host_contracts(tmp_path):
    suite = Path(__file__).resolve().parents[3] / (
        "fixtures/channel_plugins/c6_catalog_suite.py"
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
