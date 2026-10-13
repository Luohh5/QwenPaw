# -*- coding: utf-8 -*-
"""Run independent C4 evidence outside repository-wide SDK substitutions."""

import os
from pathlib import Path
import subprocess
import sys


def test_c4_shared_dependencies(tmp_path):
    suite = (
        Path(__file__).resolve().parents[3]
        / "fixtures/channel_plugins/c4_dependencies_suite.py"
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
        env={**os.environ, "QWENPAW_WORKING_DIR": str(tmp_path / "c4")},
        capture_output=True,
        text=True,
        check=False,
        timeout=150,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_c4_loader_and_registry(tmp_path):
    suite = (
        Path(__file__).resolve().parents[3]
        / "fixtures/channel_plugins/c4_framework_suite.py"
    )
    for channel in ("feishu", "dingtalk"):
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
                "QWENPAW_WORKING_DIR": str(tmp_path / channel),
            },
            capture_output=True,
            text=True,
            check=False,
            timeout=90,
        )
        assert result.returncode == 0, result.stdout + result.stderr
