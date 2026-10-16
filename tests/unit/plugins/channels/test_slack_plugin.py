# -*- coding: utf-8 -*-
"""Run Slack contracts outside repository-wide SDK mocks."""

import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    "suite_name",
    [
        "slack_suite.py",
        "slack_route_suite.py",
        "slack_legacy_suite.py",
        "slack_config_suite.py",
        "slack_policy_suite.py",
        "slack_platform_suite.py",
        "slack_stream_suite.py",
        "slack_media_suite.py",
        "slack_reliability_suite.py",
        "slack_actual_agent_suite.py",
    ],
)
def test_slack_plugin(tmp_path, suite_name):
    suite = (
        Path(__file__).resolve().parents[3]
        / "fixtures/channel_plugins"
        / suite_name
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
