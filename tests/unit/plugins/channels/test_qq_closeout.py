# -*- coding: utf-8 -*-
"""Official QQ inherited and actual Agent/native session contracts."""

import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    "suite_name",
    [
        "qq_closeout_contract_suite.py",
        "qq_closeout_formal_suite.py",
        "qq_closeout_context_suite.py",
        "qq_closeout_media_suite.py",
        "qq_closeout_config_suite.py",
        "qq_closeout_lifecycle_suite.py",
        "qq_closeout_cards_pure_suite.py",
        "qq_closeout_scalar_suite.py",
        "qq_closeout_card_host_suite.py",
        "qq_closeout_remaining_suite.py",
        "qq_retired_helpers_suite.py",
    ],
)
def test_qq_official_closeout_groups(tmp_path, suite_name):
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
            "-p",
            "pytest_asyncio.plugin",
            "--confcutdir",
            str(suite.parent),
            str(suite),
            "-q",
        ],
        env={
            **os.environ,
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "PIP_NO_INDEX": "1",
            "QWENPAW_WORKING_DIR": str(tmp_path / "work"),
            "QWENPAW_SECRET_DIR": str(tmp_path / "secret"),
            "QWENPAW_BACKUP_DIR": str(tmp_path / "backup"),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
