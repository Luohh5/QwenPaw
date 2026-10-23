# -*- coding: utf-8 -*-
"""PawPort requires a live Harness before source discovery."""

# Job setup injects ready/failed snapshots to exercise entry rechecks.
# pylint: disable=protected-access

import subprocess
import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

# Reuse the real SDK loader and its isolated singleton registry.
# pylint: disable-next=unused-import
from tests.unit.plugins.conftest import fresh_registry

# pylint: disable-next=unused-import
from tests.unit.plugins.test_sdk_entry_contract import (
    plugin_loader,
    write_plugin,
)


def _workspace(tmp_path):
    return SimpleNamespace(
        agent_id="destination",
        workspace_dir=tmp_path / "destination",
    )


async def _plugin(loader, tmp_path, source, *, fail=False):
    plugin_id = f"qwenpaw-harness-{source}"
    root = tmp_path / "plugins" / plugin_id
    body = (
        "from qwenpaw.plugins.harnesses import "
        "HarnessCapabilities, HarnessSpec\n"
        "class Plugin:\n"
        "    def register(self, context):\n"
        + (
            "        raise RuntimeError('synthetic activation failure')\n"
            if fail
            else (
                "        context.register_harness(HarnessSpec(\n"
                f"            {source!r}, {source!r}, "
                "lambda context, settings: None, "
                "HarnessCapabilities()))\n"
            )
        )
        + "plugin = Plugin()\n"
    )
    manifest = write_plugin(
        root,
        body,
        sdk={"version": "1.1.0", "capabilities": ["harnesses"]},
        meta={"harnesses": [{"id": source, "label": source}]},
    )
    record = await loader.load_plugin(manifest, root, {})
    assert record.status == ("failed" if fail else "active")
    return plugin_id


def _detail(error, source, state):
    assert error.detail == {
        "code": "harness_plugin_required",
        "source": source,
        "plugin_id": f"qwenpaw-harness-{source}",
        "state": state,
    }


@pytest.mark.parametrize("state", ["missing", "disabled", "failed"])
async def test_missing_or_disabled_plugin_blocks_import_before_scan(
    tmp_path,
    loader,
    monkeypatch,
    state,
):
    from qwenpaw.app.routers import portability_imports as router
    from qwenpaw.portability.import_jobs import PortabilityImportJobManager

    if state != "missing":
        plugin_id = await _plugin(
            loader,
            tmp_path,
            "codex",
            fail=state == "failed",
        )
        if state == "disabled":
            await loader.unload_plugin(plugin_id)
        assert loader.registry.get_harness_states()[plugin_id] == state
    factory = Mock(side_effect=AssertionError("provider factory ran"))
    manager = PortabilityImportJobManager(service_factory=factory)
    monkeypatch.setattr(router, "PORTABILITY_IMPORT_JOBS", manager)
    workspace = _workspace(tmp_path)

    async def agent(_request):
        return workspace

    async def destination(_workspace):
        return None

    monkeypatch.setattr(router, "get_agent_for_request", agent)
    monkeypatch.setattr(router, "_require_qwenpaw_backend", destination)
    request = Request(
        {"type": "http", "client": ("testclient", 1), "headers": []},
    )
    try:
        with pytest.raises(HTTPException) as caught:
            await router.create_import_job(
                router.CreateImportJobRequest(sources=["codex"]),
                request,
            )
        assert caught.value.status_code == 409
        _detail(caught.value, "codex", state)
        assert factory.call_count == 0
    finally:
        await manager.shutdown()


@pytest.mark.usefixtures("fresh_registry")
def test_explicit_source_home_cannot_bypass_plugin_requirement(
    tmp_path,
    monkeypatch,
):
    from qwenpaw.portability import providers

    home = tmp_path / "explicit-codex-home"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    resolve = Mock(side_effect=AssertionError("source path was probed"))
    monkeypatch.setattr(providers, "resolve_source_location", resolve)
    with pytest.raises(RuntimeError) as caught:
        providers.create_migration_provider("codex", _workspace(tmp_path))
    _detail(caught.value, "codex", "missing")
    resolve.assert_not_called()


async def test_multi_source_request_checks_all_plugins_before_scan(
    tmp_path,
    loader,
):
    from qwenpaw.portability.import_jobs import PortabilityImportJobManager

    await _plugin(loader, tmp_path, "codex")
    assert loader.registry.backend_registry.get("codex") is not None
    factory = Mock(side_effect=AssertionError("partial scan started"))
    manager = PortabilityImportJobManager(service_factory=factory)
    try:
        with pytest.raises(RuntimeError) as caught:
            await manager.create(_workspace(tmp_path), ["codex", "qoder"])
        _detail(caught.value, "qoder", "missing")
        factory.assert_not_called()
    finally:
        await manager.shutdown()


async def test_start_and_retry_recheck_plugin_state(tmp_path, loader):
    from qwenpaw.portability.import_jobs import (
        ImportProviderSnapshot,
        ImportRun,
        PortabilityImportJobManager,
        _LiveJob,
    )
    from qwenpaw.portability.models import (
        ImportAssetResult,
        ImportAssetState,
        ImportSelection,
        MigrationAssetPlan,
        MigrationPlan,
    )

    plugin_id = await _plugin(loader, tmp_path, "codex")
    workspace = _workspace(tmp_path)
    factory = Mock(side_effect=AssertionError("provider factory ran"))
    manager = PortabilityImportJobManager(service_factory=factory)
    plan = MigrationPlan(
        plan_id="plan-" + "c" * 32,
        source="codex",
        agent_id=workspace.agent_id,
        created_at=datetime.now(timezone.utc),
        actions=[
            MigrationAssetPlan(
                asset_type="skill",
                source_id="one",
                name="One",
            ),
        ],
    )
    selection = {"codex": ImportSelection(sessions=False, skills=["one"])}
    for state in ("awaiting_selection", "failed"):
        provider = ImportProviderSnapshot(
            source="codex",
            state="ready" if state == "awaiting_selection" else "failed",
            assets=[
                ImportAssetResult(
                    asset_type="skill",
                    source_id="one",
                    name="One",
                    state=ImportAssetState.FAILED,
                ),
            ],
        )
        job_id = f"import-{len(manager._jobs) + 1:032x}"
        manager._jobs[(workspace.agent_id, job_id)] = _LiveJob(
            workspace=workspace,
            snapshot=ImportRun(
                job_id=job_id,
                agent_id=workspace.agent_id,
                state=state,
                providers=[provider],
            ),
            plans={"codex": plan},
        )
    await loader.unload_plugin(plugin_id)
    try:
        for job_id, operation in (
            ("import-" + f"{1:032x}", manager.start),
            ("import-" + f"{2:032x}", manager.retry),
        ):
            with pytest.raises(RuntimeError) as caught:
                await operation(workspace, job_id, selection)
            _detail(caught.value, "codex", "disabled")
        factory.assert_not_called()
    finally:
        await manager.shutdown()


def test_source_listing_does_not_load_provider_or_probe_cli():
    script = """
import asyncio
import sys
from pathlib import Path
from unittest.mock import patch
from starlette.requests import Request
from qwenpaw.app.routers.portability_imports import list_import_sources
assert 'qwenpaw.portability.providers.codex' not in sys.modules
assert 'qwenpaw.portability.providers.qoder' not in sys.modules
request = Request({'type': 'http', 'client': ('testclient', 1), 'headers': []})
with patch.object(Path, 'is_dir', side_effect=AssertionError('path probe')), \\
     patch('shutil.which', side_effect=AssertionError('CLI probe')):
    rows = asyncio.run(list_import_sources(request))
assert {row['source'] for row in rows} == {'codex', 'qoder'}
assert all(row['plugin_state'] == 'missing' for row in rows)
assert all(
    row['plugin_id'] == 'qwenpaw-harness-' + row['source']
    for row in rows
)
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
