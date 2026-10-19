# -*- coding: utf-8 -*-
"""Isolated entry for official WeCom config and inherited contracts."""

import os
from pathlib import Path
import subprocess
import sys


def test_wecom_official_config_and_contract(tmp_path):
    suite = (
        Path(__file__).resolve().parents[3]
        / "fixtures/channel_plugins/wecom_config_target_closeout_suite.py"
    )
    for name in ("home", "work", "secret", "backup", "tmp"):
        (tmp_path / name).mkdir()
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
            str(suite.with_name("wecom_contract_suite.py")),
            "-q",
            "--tb=short",
        ],
        env={
            **os.environ,
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "HOME": str(tmp_path / "home"),
            "TMPDIR": str(tmp_path / "tmp"),
            "TMP": str(tmp_path / "tmp"),
            "TEMP": str(tmp_path / "tmp"),
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
