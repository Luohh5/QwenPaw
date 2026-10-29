# -*- coding: utf-8 -*-
"""Temporary host-only cost sampler; load with pytest -p remaining_cost_probe.

No production decisions change. QWENPAW_COST_REPORT is the output JSON path.
Counters include setup/call/teardown, never child-process internals.
"""
from __future__ import annotations

import asyncio
import contextvars
import functools
import importlib
import inspect
import json
import os
import platform
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

_NODE = contextvars.ContextVar("cost_probe_node", default=None)
_STAGE = contextvars.ContextVar("cost_probe_stage", default="test")
_LOCAL = threading.local()
_LOCK = threading.RLock()
_NODES = {}
_PATCHES = []
_TARGETS = []
_MISSING = []
_EPOCH = time.perf_counter()
_EVENT_LIMIT = 2000
_ORIGINAL_STAT = os.stat
_ENABLED = os.environ.get("QWENPAW_COST_PROFILE", "1") != "0"


def _row():
    return {
        "started": 0,
        "completed": 0,
        "exceptions": 0,
        "inclusive_wall_s": 0.0,
        "sync_self_wall_s": 0.0,
        "returned_bytes": 0,
        "returned_chars": 0,
        "returned_true": 0,
        "returned_false": 0,
    }


def _node_data(node):
    data = _NODES.get(node)
    if data is not None:
        return data
    metrics = {}
    for target in _TARGETS:
        key = target["name"]
        if key in {"pathlib.Path.read_text", "pathlib.Path.read_bytes"}:
            for category in ("METADATA", "RECORD", "top_level.txt", "other"):
                metrics[key + ":" + category] = _row()
        else:
            metrics[key] = _row()
    data = {
        "metrics": metrics,
        "metrics_by_stage": {},
        "raw_paths": {},
        "events": [],
        "events_dropped": 0,
        "pytest_phases": {},
    }
    _NODES[node] = data
    return data


def _start(node, key, stage, raw_path=None):
    with _LOCK:
        data = _node_data(node)
        total = data["metrics"].get(key)
        if total is None:
            total = data["metrics"][key] = _row()
        scope = data["metrics_by_stage"].get(stage)
        if scope is None:
            scope = data["metrics_by_stage"][stage] = {}
        scoped = scope.get(key)
        if scoped is None:
            scoped = scope[key] = _row()
        total["started"] += 1
        scoped["started"] += 1
        if isinstance(raw_path, (str, Path)):
            data["raw_paths"].setdefault(key, set()).add(str(raw_path))
        return total, scoped


def _finish(rows, elapsed, self_time, failed, result=None, read=False):
    with _LOCK:
        for row in rows:
            row["completed"] += 1
            row["exceptions"] += int(failed)
            row["inclusive_wall_s"] += elapsed
            if not failed and result is True:
                row["returned_true"] += 1
            elif not failed and result is False:
                row["returned_false"] += 1
            if self_time is not None:
                row["sync_self_wall_s"] += self_time
            if read and not failed:
                if isinstance(result, bytes):
                    row["returned_bytes"] += len(result)
                elif isinstance(result, str):
                    row["returned_chars"] += len(result)


def _task_id():
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    return id(task) if task is not None else None


def _event(node, key, stage, start, elapsed, failed, task, pending):
    with _LOCK:
        data = _node_data(node)
        if len(data["events"]) >= _EVENT_LIMIT:
            data["events_dropped"] += 1
            return
        event = {
            "function": key,
            "stage": stage,
            "start_s": start - _EPOCH,
            "wall_s": elapsed,
            "thread_id": threading.get_ident(),
            "task_id": task,
            "exception": failed,
        }
        if pending is not None:
            event["loaded_origins_count"] = pending
        data["events"].append(event)


def _wrap(
    function,
    key,
    *,
    stage=None,
    coarse=False,
    raw=False,
    pending_index=None,
    read_kind=None,
):
    """Sync self subtracts only wrapped sync children on this OS thread."""
    is_async = inspect.iscoroutinefunction(function)

    def enter(args, kwargs):
        node = (
            _NODE.get()
        )  # Capture ownership at entry, including late workers.
        if node is None:
            return None
        label = stage or _STAGE.get()
        if key.endswith("._observe_host_dependencies"):
            if kwargs.get("_snapshot") is not None:
                label = "host_delta_subset"
            elif _STAGE.get() == "host_delta":
                label = "host_fallback_full"
        actual_key = key
        if read_kind:
            name = args[0].name
            category = (
                name
                if name in {"METADATA", "RECORD", "top_level.txt"}
                else "other"
            )
            actual_key = f"{key}:{category}"
        rows = _start(
            node,
            actual_key,
            label,
            args[0] if raw and args else None,
        )
        pending = None
        if pending_index is not None:
            value = (
                args[pending_index]
                if len(args) > pending_index
                else kwargs.get("loaded_origins")
            )
            if value is not None and hasattr(value, "__len__"):
                pending = len(
                    value,
                )  # No iteration/materialization of generators.
        token = _STAGE.set(label) if stage or coarse else None
        return node, actual_key, label, rows, pending, token

    if is_async:

        @functools.wraps(function)
        async def async_wrapper(*args, **kwargs):
            state = enter(args, kwargs)
            if state is None:
                return await function(*args, **kwargs)
            node, actual_key, label, rows, pending, token = state
            task = _task_id() if coarse else None
            failed = True
            start = time.perf_counter()
            try:
                result = await function(*args, **kwargs)
                failed = False
                return result
            finally:
                elapsed = time.perf_counter() - start
                _finish(rows, elapsed, None, failed)
                if coarse:
                    _event(
                        node,
                        actual_key,
                        label,
                        start,
                        elapsed,
                        failed,
                        task,
                        pending,
                    )
                if token is not None:
                    _STAGE.reset(token)

        return async_wrapper

    @functools.wraps(function)
    def sync_wrapper(*args, **kwargs):
        state = enter(args, kwargs)
        if state is None:
            return function(*args, **kwargs)
        node, actual_key, label, rows, pending, token = state
        stack = getattr(_LOCAL, "stack", None)
        if stack is None:
            stack = _LOCAL.stack = []
        frame = [0.0]
        stack.append(frame)
        task = _task_id() if coarse else None
        failed, result = True, None
        start = time.perf_counter()
        try:
            result = function(*args, **kwargs)
            failed = False
            return result
        finally:
            elapsed = time.perf_counter() - start
            stack.pop()
            if stack:
                stack[-1][0] += elapsed
            _finish(
                rows,
                elapsed,
                max(0.0, elapsed - frame[0]),
                failed,
                result,
                bool(read_kind),
            )
            if coarse:
                _event(
                    node,
                    actual_key,
                    label,
                    start,
                    elapsed,
                    failed,
                    task,
                    pending,
                )
            if token is not None:
                _STAGE.reset(token)

    return sync_wrapper


def _patch(owner, name, key, **options):
    try:
        descriptor = inspect.getattr_static(owner, name)
    except AttributeError:
        _MISSING.append(key)
        return
    function = (
        descriptor.__func__
        if isinstance(descriptor, (staticmethod, classmethod))
        else getattr(owner, name)
    )
    if inspect.isasyncgenfunction(inspect.unwrap(function)):
        _MISSING.append(
            key
            + " (async contextmanager factory: skipped to preserve context-manager "
            + "result identity; constructor time is not body time)",
        )
        return
    if not callable(function):
        _MISSING.append(key + " (not callable)")
        return
    wrapper = _wrap(function, key, **options)
    replacement = (
        type(descriptor)(wrapper)
        if isinstance(descriptor, (staticmethod, classmethod))
        else wrapper
    )
    setattr(owner, name, replacement)
    _PATCHES.append((owner, name, descriptor))
    _TARGETS.append(
        {
            "name": key,
            "async": inspect.iscoroutinefunction(function),
            "coarse": bool(options.get("coarse")),
            "self_time_valid": not inspect.iscoroutinefunction(function),
        },
    )
    # Production imports aliases, e.g. `from ... import canonical`. Preserve
    # their identity-based binding and restore each original on finish.
    for module_name, module in tuple(sys.modules.items()):
        if module_name.startswith("qwenpaw.") and module is not None:
            for alias, value in tuple(vars(module).items()):
                if value is function and (
                    module is not owner or alias != name
                ):
                    setattr(module, alias, wrapper)
                    _PATCHES.append((module, alias, value))


def _install():
    modules = {}
    for short in (
        "dependency_maintenance",
        "execution.dependencies",
        "python_backend",
        "dependency_gate",
        "dependency_transaction",
        "execution.process",
        "loader",
        "install_lock",
    ):
        name = "qwenpaw.plugins." + short
        try:
            modules[short] = importlib.import_module(name)
        except Exception as exc:
            _MISSING.append(name + " (import " + type(exc).__name__ + ")")
    if os.name == "nt":
        import ntpath
    if not _ENABLED:
        return  # Same target imports, lightweight pytest hooks, no wrappers.
    specs = {
        "dependency_maintenance": [
            (name, None)
            for name in (
                "canonical",
                "_existing_path",
                "_enumerated_path",
                "_try_lock",
                "check_admission",
                "live_readers",
            )
        ],
        "install_lock": [
            ("_acquire_os_lock", None),
            ("environment_install_lock_path", None),
        ],
        "execution.dependencies": [
            (name, None)
            for name in (
                "_read_installed_metadata",
                "_installed_metadata",
                "read_dependency_inventory",
                "observe_dependencies",
                "_provides_loaded_modules",
                "_read_record",
                "_record_entries",
                "_parse_record_entries",
                "_record_names",
            )
        ],
        "python_backend": [
            ("_check_read_history", "read_history"),
            ("_observe_host_dependencies", "host_full"),
            ("_observe_host_delta", "host_delta"),
            ("_files_for_loaded_origins", None),
        ],
        "dependency_gate": [
            ("prepare_shared_dependencies", "prepare"),
            ("admit_shared_dependencies", "read_admission"),
            ("evaluate_shared_dependencies", "read_admission"),
            ("_evaluate_shared_dependencies_async", "read_admission"),
            ("complete_read_uses", "read_admission"),
        ],
        "dependency_transaction": [
            ("prepare_payload", "payload"),
            ("environment_fingerprint", "environment_fingerprint"),
            ("publish", "publish"),
            ("rollback", "rollback"),
        ],
    }
    for module_name, entries in specs.items():
        module = modules.get(module_name)
        if module is None:
            continue
        for name, stage in entries:
            opts = {
                "stage": stage,
                "coarse": stage is not None,
                "raw": name in {"canonical", "_existing_path"},
            }
            if name in {"_observe_host_dependencies", "_observe_host_delta"}:
                opts["pending_index"] = 2
            elif name == "_files_for_loaded_origins":
                opts["pending_index"] = 1
            _patch(module, name, module.__name__ + "." + name, **opts)
    classes = [
        (
            "python_backend",
            "PythonBackend",
            {
                "prepare": "prepare",
                "admit_existing": "read_admission",
                "audit_dependencies": "full_audit",
                "observe_host_dependencies": "host_observation",
            },
        ),
        (
            "execution.process",
            "PluginProcess",
            {
                "start": "worker_start",
                "_start": "worker_start",
                "_launch": "worker_launch",
                "_open": "worker_open_handshake",
                "_hello": "worker_handshake",
                "call": "worker_rpc",
            },
        ),
        (
            "loader",
            "PluginLoader",
            {
                "_prepare_python_dependencies": "prepare",
                "_dependency_registration": "prepare",
                "_admit_dependencies": "read_admission",
                "_observe_host_dependencies": "host_observation",
            },
        ),
    ]
    for module_name, class_name, names in classes:
        module = modules.get(module_name)
        cls = getattr(module, class_name, None) if module else None
        if cls is None:
            _MISSING.append(
                "qwenpaw.plugins." + module_name + "." + class_name,
            )
            continue
        for name, stage in names.items():
            _patch(
                cls,
                name,
                cls.__module__ + "." + class_name + "." + name,
                stage=stage,
                coarse=True,
            )
    for name in ("stat", "lstat", "fsync"):
        _patch(os, name, "os." + name)
    _patch(os.path, "realpath", "os.path.realpath")
    if os.name == "nt":
        _patch(ntpath, "_getfinalpathname", "ntpath._getfinalpathname")
    else:
        _MISSING.append("ntpath._getfinalpathname (non-Windows host)")
    for name in ("read_text", "read_bytes"):
        _patch(Path, name, "pathlib.Path." + name, read_kind=name)
    # Keep Popen a class: callers and isinstance checks remain valid.
    _patch(
        subprocess.Popen,
        "__init__",
        "subprocess.Popen.__init__",
        stage="worker_spawn",
        coarse=True,
    )


def _restore():
    for owner, name, original in reversed(_PATCHES):
        setattr(owner, name, original)
    _PATCHES.clear()


def _environment_inventory():
    """Post-timing enumeration only; stat actual RECORD files, no content read."""
    start = time.perf_counter()
    from importlib import metadata
    import stat
    from qwenpaw.plugins.dependency_maintenance import canonical

    distributions = list(metadata.distributions())
    records, errors = {}, {}
    unsupported = 0
    for distribution in distributions:
        origin = getattr(distribution, "_path", None)
        if not isinstance(origin, Path):
            unsupported += 1
            continue
        record = origin / "RECORD"
        try:
            value = _ORIGINAL_STAT(record)
        except FileNotFoundError:
            continue
        except OSError as exc:
            key = type(exc).__name__
            errors[key] = errors.get(key, 0) + 1
            continue
        if stat.S_ISREG(value.st_mode):
            try:
                # Preserve actual Windows case-sensitive directory spelling.
                key = str(canonical(record))
                records[key] = value.st_size
            except (OSError, RuntimeError, ValueError) as exc:
                key = type(exc).__name__
                errors[key] = errors.get(key, 0) + 1
    return {
        "visible_distributions": len(distributions),
        "unique_record_files": len(records),
        "record_file_bytes": sum(records.values()),
        "unsupported_nonfilesystem_distributions": unsupported,
        "record_stat_errors": errors,
        "loaded_modules": len(sys.modules),
        "collection_wall_s": time.perf_counter() - start,
        "timing": "after measured tests and wrapper restoration",
    }


def _snapshot():
    with _LOCK:
        nodes = {}
        for node, data in _NODES.items():
            nodes[node] = {
                key: value
                for key, value in data.items()
                if key
                not in {
                    "raw_paths",
                    "metrics",
                    "metrics_by_stage",
                    "events",
                    "pytest_phases",
                }
            }
            nodes[node]["metrics"] = {
                key: dict(value) for key, value in data["metrics"].items()
            }
            nodes[node]["metrics_by_stage"] = {
                stage: {key: dict(value) for key, value in metrics.items()}
                for stage, metrics in data["metrics_by_stage"].items()
            }
            nodes[node]["events"] = [dict(value) for value in data["events"]]
            nodes[node]["pytest_phases"] = {
                key: dict(value)
                for key, value in data["pytest_phases"].items()
            }
            nodes[node]["unique_raw_path_strings"] = {
                key: len(values) for key, values in data["raw_paths"].items()
            }
            nodes[node]["unique_raw_path_strings_combined"] = len(
                set().union(*data["raw_paths"].values()),
            )
        return nodes


def _write_report(environment=None, exitstatus=None):
    path = os.environ.get("QWENPAW_COST_REPORT")
    if not path:
        return
    payload = {
        "schema": 1,
        "python": sys.version,
        "platform": platform.platform(),
        "wrappers_enabled": _ENABLED,
        "nodes": _snapshot(),
        "targets": _TARGETS,
        "missing_targets": _MISSING,
        "environment_after_tests": environment,
        "pytest_exitstatus": exitstatus,
        "semantics": {
            "scope": "host Python only; child internal CPU is unobserved",
            "time": "wall seconds, inclusive; async overlaps/nesting never subtracted",
            "sync_self": "same-thread synchronous self wall only, subtract wrapped "
            "synchronous children; async value remains zero and is invalid",
            "record": "_read_record/_record_entries are helper calls, not physical "
            "reads; Path.read_text:RECORD success is actual filesystem read; "
            "returned_chars is decoded length, not encoded bytes",
            "native": "os.stat/lstat/fsync wrap original native boundary; realpath "
            "wraps Python API; ntpath._getfinalpathname separately measures Windows "
            "native lookup",
            "paths": "exact str/Path input strings counted only for canonical/_existing_path; no private path listings",
            "attribution": "node captured at wrapper entry from ContextVar; stages "
            "propagate to run_sync_io threads; arbitrary threads without copied "
            "context are unobserved",
            "overhead": "wrappers, locks, sets, contextvars, perf_counter and "
            "bounded coarse events perturb timings; compare baseline/profiled/warm "
            "baseline",
            "events": f"at most {_EVENT_LIMIT} coarse spans per node; excess explicitly counted",
        },
    }
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2), encoding="utf-8")


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    _install()


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_protocol(item, nextitem):
    token = _NODE.set(item.nodeid)
    stage = _STAGE.set("test")
    with _LOCK:
        data = _node_data(item.nodeid)
        data["modules_start"] = len(sys.modules)
        data["test_start_s"] = time.perf_counter() - _EPOCH
    try:
        yield
    finally:
        with _LOCK:
            data["modules_end"] = len(sys.modules)
            data["test_end_s"] = time.perf_counter() - _EPOCH
        _NODE.reset(token)
        _STAGE.reset(stage)
        _write_report()


def pytest_runtest_logreport(report):
    with _LOCK:
        data = _node_data(report.nodeid)
        data["pytest_phases"][report.when] = {
            "duration_s": report.duration,
            "outcome": report.outcome,
        }


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session, exitstatus):
    _restore()
    try:
        environment = _environment_inventory()
    except Exception as exc:
        environment = {"collection_error": type(exc).__name__}
    _write_report(environment, int(exitstatus))


def pytest_unconfigure(config):
    _restore()
