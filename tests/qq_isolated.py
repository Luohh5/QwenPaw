# -*- coding: utf-8 -*-
"""Run a mapped QQ suite without the parent test process's SDK mocks."""

import os
from pathlib import Path
import subprocess
import sys


def run_qq_suite(suite, tmp_path):
    fixture = Path(__file__).resolve().parent / "fixtures/channel_plugins"
    target = fixture / suite
    assert target.is_file()
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
            str(fixture),
            str(target),
            "-q",
            "--tb=short",
        ],
        env={
            **os.environ,
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "PIP_NO_INDEX": "1",
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
