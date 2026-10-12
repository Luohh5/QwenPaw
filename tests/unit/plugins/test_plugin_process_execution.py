# -*- coding: utf-8 -*-
"""Run generic process evidence without repository-wide SDK replacements."""

import os
from pathlib import Path
import subprocess
import sys


def test_plugin_process_execution(tmp_path):
    suite = (
        Path(__file__).resolve().parents[2]
        / "fixtures/plugin_execution/process_suite.py"
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
            "QWENPAW_WORKING_DIR": str(tmp_path / "generic-execution"),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_existing_plugin_exports_keep_object_identity(tmp_path):
    script = """
import qwenpaw.plugins as plugins
from qwenpaw.plugins import (
    PluginLoader, PluginRegistry, PluginApi,
    PluginManifest, PluginRecord, get_tool_config,
)
from qwenpaw.plugins.loader import PluginLoader as DirectLoader
from qwenpaw.plugins.registry import PluginRegistry as DirectRegistry
from qwenpaw.plugins.api import (
    PluginApi as DirectApi, get_tool_config as direct_config,
)
from qwenpaw.plugins.architecture import (
    PluginManifest as DirectManifest, PluginRecord as DirectRecord,
)
for name, public, direct in (
    ('PluginLoader', PluginLoader, DirectLoader),
    ('PluginRegistry', PluginRegistry, DirectRegistry),
    ('PluginApi', PluginApi, DirectApi),
    ('PluginManifest', PluginManifest, DirectManifest),
    ('PluginRecord', PluginRecord, DirectRecord),
    ('get_tool_config', get_tool_config, direct_config),
):
    assert public is direct, name
    assert getattr(plugins, name) is public, name
    assert getattr(plugins, name) is public, name
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", script],
        env={
            **os.environ,
            "QWENPAW_WORKING_DIR": str(tmp_path / "plugin-exports"),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
