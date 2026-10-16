"""Opt-in read-only resource observer around one unchanged acceptance node.

Load with pytest -p c8_current_resource_observer and set
QWENPAW_C8_METRICS_FILE / QWENPAW_C8_METRICS_CHANNEL. No product method,
Driver, native transport, or original assertion is replaced. Timing starts at
this fixture's setup, not at application readiness or platform connect.
"""

import asyncio
import json
import os
from pathlib import Path
import time

import psutil
import pytest
import pytest_asyncio

_REPORTS = {}
_SAMPLES = []
_KNOWN_PIDS = set()
_ERRORS = []
_LOOP = None
_STARTED = None
_CHANNEL = None
_NODEID = None
_SEEN_CONNECTED = set()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    result = yield
    report = result.get_result()
    _REPORTS.setdefault(item.nodeid, {})[report.when] = report.outcome


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    yield
    if _LOOP is not None:
        _SAMPLES.append(
            _snapshot("business_call_return", _STARTED, _CHANNEL, item.nodeid)
        )


def _process(pid):
    row = {"pid": pid}
    try:
        process = psutil.Process(pid)
        row.update(
            rss_bytes=process.memory_info().rss,
            threads=process.num_threads(),
            status=process.status(),
            cmdline=process.cmdline(),
        )
        try:
            row["inet_sockets"] = [
                {
                    "fd": item.fd,
                    "type": int(item.type),
                    "local": list(item.laddr),
                    "remote": list(item.raddr),
                    "status": item.status,
                }
                for item in process.net_connections(kind="inet")
            ]
        except (psutil.AccessDenied, psutil.NoSuchProcess) as exc:
            row["socket_observation_error"] = type(exc).__name__
    except (psutil.AccessDenied, psutil.NoSuchProcess) as exc:
        row["observation_error"] = type(exc).__name__
    return row


def _snapshot(label, started, channel, nodeid):
    # Read the already-existing registry. Never create one for measurement.
    from qwenpaw.plugins.registry import PluginRegistry

    registry = PluginRegistry._instance
    workers = []
    unattached_leases = []
    if registry is not None:
        owners = registry.runtime_owners
        for lease in owners.leases():
            if lease.key.kind != "channel" or lease.key.name != channel:
                continue
            proxy = owners.resource(lease)
            if proxy is None:
                unattached_leases.append(lease.instance_id)
                continue
            health = proxy.channel_process.health_snapshot()
            pid = health.get("pid")
            if not pid:
                continue
            _KNOWN_PIDS.add(pid)
            workers.append(
                {
                    **_process(pid),
                    "plugin_id": lease.plugin_id,
                    "scope_id": lease.key.scope_id,
                    "instance_id": lease.instance_id,
                    "generation": lease.generation,
                    "process_state": health.get("state"),
                    "platform_state": health.get("platform_state"),
                }
            )
    parent = _process(os.getpid())
    external = [
        _process(process.pid)
        for process in psutil.Process().children(recursive=True)
        if process.pid not in _KNOWN_PIDS
    ]
    known_unleased = [
        _process(pid)
        for pid in _KNOWN_PIDS - {row["pid"] for row in workers}
        if psutil.pid_exists(pid)
    ]
    complete = all("rss_bytes" in p for p in [parent, *workers])
    return {
        "nodeid": nodeid,
        "label": label,
        "seconds_since_observer_setup": time.monotonic() - started,
        "parent": parent,
        "workers": workers,
        "unattached_lease_instance_ids": unattached_leases,
        "known_worker_pids_still_alive_without_lease": known_unleased,
        "external_or_other_children_excluded": external,
        "sum_rss_bytes": (
            parent["rss_bytes"] + sum(p["rss_bytes"] for p in workers)
            if complete
            else None
        ),
        "host_tasks": [
            {
                "name": task.get_name(),
                "coro": getattr(task.get_coro(), "__qualname__", "unknown"),
            }
            for task in asyncio.all_tasks(_LOOP)
            if task.get_name() != "c8-read-only-resource-observer"
        ],
    }


@pytest_asyncio.fixture(autouse=True)
async def c8_resource_observer(request):
    global _LOOP, _STARTED, _CHANNEL, _NODEID
    _LOOP = asyncio.get_running_loop()
    destination = os.environ.get("QWENPAW_C8_METRICS_FILE")
    channel = os.environ.get("QWENPAW_C8_METRICS_CHANNEL")
    if not destination or not channel:
        raise RuntimeError("C8_OBSERVER_REQUIRES_EXPLICIT_OUTPUT_AND_CHANNEL")
    started = time.monotonic()
    _STARTED, _CHANNEL, _NODEID = started, channel, request.node.nodeid
    running = True

    async def observe():
        while running:
            row = _snapshot("periodic", started, channel, request.node.nodeid)
            connected = {
                w["pid"]
                for w in row["workers"]
                if w["process_state"] == "ready"
                and w["platform_state"] == "connected"
            }
            if connected - _SEEN_CONNECTED:
                row["label"] = "first_platform_connected_observation"
                row["new_connected_pids"] = sorted(connected - _SEEN_CONNECTED)
                _SEEN_CONNECTED.update(connected)
            _SAMPLES.append(row)
            await asyncio.sleep(0.05)

    task = asyncio.create_task(
        observe(), name="c8-read-only-resource-observer"
    )
    try:
        yield
    finally:
        running = False
        try:
            await task
            _SAMPLES.append(
                _snapshot(
                    "observer_fixture_teardown",
                    started,
                    channel,
                    request.node.nodeid,
                )
            )
        except Exception as exc:
            _ERRORS.append({"type": type(exc).__name__, "detail": str(exc)})
            raise


def pytest_sessionfinish(session, exitstatus):
    destination = os.environ.get("QWENPAW_C8_METRICS_FILE")
    if not destination:
        return
    if _LOOP is not None:
        _SAMPLES.append(
            _snapshot(
                "session_after_all_fixture_teardown",
                _STARTED,
                _CHANNEL,
                _NODEID,
            )
        )
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "channel": os.environ.get("QWENPAW_C8_METRICS_CHANNEL"),
                "pytest_exitstatus": int(exitstatus),
                "node_reports": _REPORTS,
                "samples": _SAMPLES,
                "errors": _ERRORS,
                "known_worker_pids": sorted(_KNOWN_PIDS),
                "known_workers_alive_at_session_end": [
                    pid for pid in _KNOWN_PIDS if psutil.pid_exists(pid)
                ],
                "scope": "Existing acceptance fixture; not full App or comparable cross-channel timing",
                "rss_note": "Per-process RSS sum double-counts shared pages; missing samples stay null",
                "tasks_note": "Host tasks only; child asyncio task count is not available",
                "observer_cost": "One host asyncio sampler task at 50ms; psutil and pytest overhead included",
            },
            indent=2,
        )
        + "\n"
    )
