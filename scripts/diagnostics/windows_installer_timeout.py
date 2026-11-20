# -*- coding: utf-8 -*-
"""Observe process termination around the original installer timeout test."""

import ctypes
from ctypes import wintypes
import json
import sys

import pytest


@pytest.fixture(autouse=True)
def observe_installer_exit(tmp_path, monkeypatch):
    """Read a stable child handle without changing the test or waiting on it."""
    if sys.platform != "win32":
        pytest.skip("Requires a native Windows process handle")

    from qwenpaw.plugins import dependency_process

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    )
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL

    stop = dependency_process._stop_process_tree
    install = dependency_process.run_install_command
    observation = {}
    handle = None

    def handle_status():
        if not handle:
            return "no handle"
        result = kernel.WaitForSingleObject(handle, 0)
        if result == 0:
            return "signaled"
        if result == 258:
            return "unsignaled"
        return f"wait error {ctypes.get_last_error()}"

    def observe_cleanup(proc, tree=None):
        nonlocal handle
        try:
            child_pid = int(
                (tmp_path / "child.pid").read_text(encoding="utf-8"),
            )
            handle = kernel.OpenProcess(0x00100000, False, child_pid)
            if handle:
                observation["before_cleanup"] = handle_status()
            else:
                observation["open_process_error"] = ctypes.get_last_error()
        except (OSError, ValueError) as exc:
            observation["open_process_error"] = type(exc).__name__
        try:
            return stop(proc, tree)
        finally:
            observation["cleanup_return"] = handle_status()

    def observe_return(*args, **kwargs):
        nonlocal handle
        try:
            return install(*args, **kwargs)
        finally:
            observation["installer_return"] = handle_status()
            if handle:
                kernel.CloseHandle(handle)
                handle = None

    monkeypatch.setattr(
        dependency_process,
        "_stop_process_tree",
        observe_cleanup,
    )
    monkeypatch.setattr(
        dependency_process,
        "run_install_command",
        observe_return,
    )
    try:
        yield
    finally:
        if handle:
            kernel.CloseHandle(handle)
        print("PROCESS_EXIT " + json.dumps(observation, sort_keys=True))
        assert observation.get("cleanup_return") == "signaled", observation
        assert observation.get("installer_return") == "signaled", observation
