# -*- coding: utf-8 -*-
"""Local runtimes never invoke dependency package managers."""

# pylint: disable=protected-access
from unittest.mock import Mock

import pytest

from qwenpaw.plugins.loader import PluginLoader


def test_local_runtime_rejects_dependency_installation(tmp_path, monkeypatch):
    monkeypatch.setenv("QWENPAW_RUNTIME_PROVISIONER", "local")
    loader = PluginLoader([tmp_path])
    installer = Mock()
    monkeypatch.setattr(
        loader,
        "_run_subprocess_with_streaming_log",
        installer,
    )
    with pytest.raises(RuntimeError, match="administrator"):
        loader._install_requirements(tmp_path / "requirements.txt", "app")
    installer.assert_not_called()


@pytest.mark.asyncio
async def test_cancelled_late_maintenance_entry_clears_own_marker(
    tmp_path,
    monkeypatch,
):
    import asyncio
    import threading
    from contextlib import contextmanager
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from qwenpaw.plugins import dependency_maintenance as maintenance
    from qwenpaw.plugins import dependency_transaction as transaction
    from qwenpaw.plugins import dependency_upgrade

    monkeypatch.setenv("QWENPAW_DEPENDENCY_STATE_DIR", str(tmp_path / "state"))
    site = tmp_path / "site"
    site.mkdir()
    directory = tmp_path / "operation"
    directory.mkdir()
    operation = {
        "id": "cancelled-entry",
        "status": "ready",
        "plugin_id": "sample",
        "capability": "private-token",
        "source_revision": "source-hash",
        "requires_restart": False,
        "proposed": {},
        "affected_instances": [],
        "payload": {"sites": [str(site)], "old": {}},
    }
    fresh = SimpleNamespace(to_mapping=lambda: {})
    binding = SimpleNamespace(
        description=None,
        backend=SimpleNamespace(
            audit_dependencies=AsyncMock(return_value=fresh),
        ),
    )
    owners = SimpleNamespace(
        dependency_leases=lambda: [],
        leases=lambda *_: [],
    )
    usage = SimpleNamespace(
        capture_admissions=lambda _: [],
        restore_rebased=lambda _: None,
    )
    loader = SimpleNamespace(
        registry=SimpleNamespace(
            runtime_owners=owners,
            dependency_usage=usage,
        ),
        get_loaded_plugin=lambda _: None,
        _install_paths=lambda *_: (None, tmp_path / "plugin"),
    )
    manager = SimpleNamespace(
        loader=loader,
        _directory=lambda _: directory,
        _read=lambda _: operation,
        _save=lambda *_: None,
        _public=lambda value: value,
        _binding=AsyncMock(return_value=binding),
        _consumers=AsyncMock(return_value=[]),
        _check_reviewed_usage=lambda *_: None,
    )
    monkeypatch.setattr(
        dependency_upgrade,
        "_source_hash",
        lambda _: "source-hash",
    )
    monkeypatch.setattr(transaction, "_verify_reviewed_plan", lambda *_: None)
    monkeypatch.setattr(transaction, "backup_source", lambda *_: None)
    monkeypatch.setattr(transaction, "rollback", lambda *_: None)
    monkeypatch.setattr(transaction, "restore_source", lambda *_: None)
    started, release = threading.Event(), threading.Event()
    original = maintenance.maintenance

    @contextmanager
    def slow_entry(*args, **kwargs):
        with original(*args, **kwargs) as value:
            started.set()
            assert release.wait(3)
            yield value

    monkeypatch.setattr(maintenance, "maintenance", slow_entry)
    task_capabilities = []

    async def apply():
        try:
            await transaction.apply_operation(manager, operation["id"])
        finally:
            task_capabilities.append(maintenance.capability_context.get())

    pending = asyncio.create_task(apply())
    try:
        assert await asyncio.to_thread(started.wait, 2)
        assert maintenance.marker(site).exists()
        pending.cancel()
        await asyncio.sleep(0)
        assert not pending.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(pending, 5)
    assert task_capabilities == [""]
    assert not maintenance.marker(site).exists()
    assert operation["retryable"] is True
    with original([site], "next-operation", "next-capability"):
        pass
    maintenance.clear_maintenance([site], "next-operation")
