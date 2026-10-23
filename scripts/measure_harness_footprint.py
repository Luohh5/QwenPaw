#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Measure an isolated import footprint without claiming lifecycle readiness.

Each invocation uses a fresh process and disposable home/config directories.
Process creation and network access are denied before importing the app. The
scenario labels reserve lifecycle measurements; import_ms is never ready_ms.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import time

SCENARIOS = (
    "native-startup",
    "native-agent-list",
    "native-chat",
    "frontend-native",
)
ROOT = Path(__file__).resolve().parents[1]
_VENDORS = {"codex", "qoder", "qodercli", "claude"}
_SOURCE_PATHS = (
    "src",
    "packages",
    "pyproject.toml",
    "scripts/measure_harness_footprint.py",
)


class VendorProbeGuard:
    """Deny launches (including wrappers) and record redacted probe attempts."""

    def __init__(self):
        self.vendor_process_count = 0
        self.vendor_probe_count = 0
        self.blocked_process_attempts = []
        self.blocked_network_attempts = 0
        self._active = False
        self._which = None

    @staticmethod
    def _basename(value):
        if isinstance(value, bytes):
            value = os.fsdecode(value)
        return Path(str(value)).name

    def _audit(self, event, arguments):
        if not self._active:
            return
        if event in {
            "subprocess.Popen",
            "os.system",
            "os.posix_spawn",
            "os.exec",
            "os.spawn",
            "os.fork",
            "os.forkpty",
        }:
            basename = self._basename(arguments[0]) if arguments else ""
            vendor = basename.lower().removesuffix(".exe")
            executable = (
                "<shell>"
                if event == "os.system"
                else (
                    "<fork>"
                    if event in {"os.fork", "os.forkpty"}
                    else vendor
                    if vendor in _VENDORS
                    else "<process>"
                )
            )
            argv = arguments[1] if len(arguments) > 1 else ()
            names = {basename}
            if isinstance(argv, (list, tuple)):
                names.update(self._basename(part) for part in argv)
            if any(
                name.lower().removesuffix(".exe") in _VENDORS for name in names
            ):
                self.vendor_probe_count += 1
            self.blocked_process_attempts.append(
                {"event": event, "executable": executable},
            )
            raise RuntimeError("Footprint probe blocked process execution")
        if event in {
            "socket.connect",
            "socket.sendto",
            "socket.sendmsg",
            "socket.getaddrinfo",
            "socket.gethostbyname",
            "socket.gethostbyaddr",
            "socket.getnameinfo",
        }:
            self.blocked_network_attempts += 1
            raise RuntimeError("Footprint probe blocked network access")

    def __enter__(self):
        self._which = shutil.which

        def which(command, *args, **kwargs):
            if (
                self._basename(command).lower().removesuffix(".exe")
                in _VENDORS
            ):
                self.vendor_probe_count += 1
            return self._which(command, *args, **kwargs)

        sys.addaudithook(self._audit)
        shutil.which = which
        self._active = True
        return self

    def __exit__(self, *_):
        self._active = False
        shutil.which = self._which


def _rss():
    try:
        import psutil

        process = psutil.Process()
        rss = process.memory_info().rss
        try:
            children_rss = sum(
                child.memory_info().rss
                for child in process.children(recursive=True)
            )
            source = "psutil-current-rss"
        except (PermissionError, psutil.AccessDenied):
            # The sandbox can deny process enumeration. All Python launch
            # boundaries are blocked; this process has created no children.
            children_rss = 0
            source = "psutil-current-rss; children-zero-by-launch-denial"
        return (
            rss,
            children_rss,
            source,
        )
    except ImportError:
        try:
            import resource
        except ImportError:
            return (
                None,
                None,
                "rss-unavailable: psutil and resource unavailable",
            )

        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return (
            int(rss if sys.platform == "darwin" else rss * 1024),
            0,
            "resource-peak-rss",
        )


def _probe(scenario, commit):
    # Keep stdout reserved for the protocol; imported libraries may print.
    from contextlib import redirect_stdout, redirect_stderr

    started = time.perf_counter()
    error = None
    missing_module = None
    with VendorProbeGuard() as guard:
        with open(os.devnull, "w", encoding="utf-8") as quiet:
            with redirect_stdout(quiet), redirect_stderr(quiet):
                try:
                    importlib.import_module("qwenpaw.app._app")
                except Exception as exc:  # report partial imports honestly
                    error = type(exc).__name__
                    if isinstance(exc, ModuleNotFoundError):
                        missing_module = exc.name
        elapsed = (time.perf_counter() - started) * 1000
        rss, children_rss, rss_source = _rss()
    return {
        "commit": commit,
        "python": platform.python_version(),
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "scenario": scenario,
        "milestone": "app-import-failed" if error else "app-imported",
        "ready_ms": None,
        "import_ms": elapsed,
        "import_error": error,
        "missing_module": missing_module,
        "unavailable_reason": (
            "Import-only probe; ASGI lifespan, native agent listing/chat, "
            "and browser interaction were not executed."
            + (f" App import stopped with {error}." if error else "")
        ),
        "process_rss_bytes": rss,
        "children_rss_bytes": children_rss,
        "rss_source": rss_source,
        "modules": sorted(sys.modules),
        "vendor_process_count": guard.vendor_process_count,
        "vendor_probe_count": guard.vendor_probe_count,
        "blocked_process_attempts": guard.blocked_process_attempts,
        "blocked_network_attempts": guard.blocked_network_attempts,
        "isolation": (
            "fresh process; temporary home, working, secret, cache and "
            "config directories; launches/network denied"
        ),
    }


def _source_state(root):
    """Identify actual git-listed source bytes, including untracked edits."""
    names = subprocess.check_output(
        [
            "git",
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
            "--",
            *_SOURCE_PATHS,
        ],
        cwd=root,
    ).split(b"\0")
    digest = hashlib.sha256()
    for name in sorted(set(names) - {b""}):
        path = Path(root) / os.fsdecode(name)
        digest.update(name + b"\0")
        digest.update(
            b"file\0" + path.read_bytes() if path.is_file() else b"missing\0",
        )
        digest.update(b"\0")
    dirty = subprocess.check_output(
        [
            "git",
            "status",
            "--porcelain",
            "--untracked-files=all",
            "--",
            *_SOURCE_PATHS,
        ],
        cwd=root,
    )
    return {"dirty": bool(dirty), "fingerprint": digest.hexdigest()}


def _isolated_env(temporary):
    temporary = Path(temporary)
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(
            ("QWENPAW_", "CODEX_", "QODER_", "CLAUDE_", "PYTHON"),
        )
        and not any(
            part in key.upper()
            for part in ("TOKEN", "SECRET", "API_KEY", "PASSWORD")
        )
    }
    env.update(
        HOME=str(temporary / "home"),
        USERPROFILE=str(temporary / "home"),
        APPDATA=str(temporary / "config"),
        LOCALAPPDATA=str(temporary / "cache"),
        XDG_CACHE_HOME=str(temporary / "cache"),
        XDG_CONFIG_HOME=str(temporary / "config"),
        XDG_DATA_HOME=str(temporary / "data"),
        QWENPAW_WORKING_DIR=str(temporary / "work"),
        QWENPAW_SECRET_DIR=str(temporary / "secrets"),
        QWENPAW_RUNTIME_ID="harness-footprint-probe",
        QWENPAW_LOG_LEVEL="error",
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONPATH=os.pathsep.join(
            (str(ROOT / "src"), str(ROOT / "packages/qwenpawmail-mcp/src")),
        ),
    )
    return env


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", required=True, choices=SCENARIOS)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--_child",
        action="store_true",
        dest="child",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--_commit",
        dest="commit",
        default="",
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()
    if args.child:
        print(json.dumps(_probe(args.scenario, args.commit), sort_keys=True))
        return
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        text=True,
    ).strip()
    source_state = _source_state(ROOT)
    with tempfile.TemporaryDirectory(prefix="qwenpaw-footprint-") as directory:
        temporary = Path(directory)
        # Avoid inherited product overrides, credentials, user Python hooks,
        # config paths, or provider homes reaching the isolated import.
        env = _isolated_env(temporary)
        for name in ("home", "cache", "config", "data", "work", "secrets"):
            (temporary / name).mkdir()
        child = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--scenario",
                args.scenario,
                "--output",
                str(args.output),
                "--_child",
                "--_commit",
                commit,
            ],
            cwd=temporary,
            env=env,
            text=True,
            capture_output=True,
            timeout=75,
            check=True,
        )
        report = json.loads(child.stdout)
        report["source_state"] = source_state
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
