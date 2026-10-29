# -*- coding: utf-8 -*-
"""Temporary host-only phase counters; no audit or transaction replacement."""

from collections import defaultdict
from functools import wraps
import json
import os
from pathlib import Path
import sys
from threading import Lock
from time import perf_counter

import pytest

_COUNTS = defaultdict(lambda: [0, 0.0])
_LOCK = Lock()
_INSTALLED = False


def _timed(name, function):
    @wraps(function)
    def measured(*args, **kwargs):
        start = perf_counter()
        try:
            return function(*args, **kwargs)
        finally:
            with _LOCK:
                row = _COUNTS[name]
                row[0] += 1
                row[1] += perf_counter() - start

    return measured


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    global _INSTALLED
    if _INSTALLED:
        return
    from qwenpaw.plugins import dependency_maintenance as maintenance
    from qwenpaw.plugins import dependency_transaction as transaction
    from qwenpaw.plugins.dependency_upgrade import DependencyUpgradeManager
    from qwenpaw.plugins.execution import dependencies

    replacements = {}
    for module, names in (
        (maintenance, ("canonical", "_existing_path", "_enumerated_path")),
        (transaction, ("environment_fingerprint", "prepare_payload")),
        (dependencies, ("_read_installed_metadata", "_record_entries")),
    ):
        for name in names:
            function = getattr(module, name)
            replacements[id(function)] = _timed(name, function)
    for module in tuple(sys.modules.values()):
        if module is None or not getattr(module, "__name__", "").startswith(
            ("qwenpaw.", "tests."),
        ):
            continue
        for name, value in tuple(vars(module).items()):
            replacement = replacements.get(id(value))
            if replacement is not None:
                setattr(module, name, replacement)
    method = DependencyUpgradeManager._prepare_resolution
    DependencyUpgradeManager._prepare_resolution = staticmethod(
        _timed("_prepare_resolution", method),
    )
    os.scandir = _timed("scandir", os.scandir)
    _INSTALLED = True


def pytest_terminal_summary(terminalreporter):
    report = {
        name: {"calls": count, "seconds": round(seconds, 6)}
        for name, (count, seconds) in sorted(_COUNTS.items())
    }
    terminalreporter.write_line("HOST_PHASE_COUNTERS=" + json.dumps(report))
    destination = os.environ.get("QWENPAW_TIMING_REPORT")
    if destination:
        Path(destination).write_text(json.dumps(report, indent=2))
