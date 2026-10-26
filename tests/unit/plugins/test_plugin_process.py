# -*- coding: utf-8 -*-
"""Normal generic plugin supervision through actual OS workers and IPC."""

import asyncio
import base64
from dataclasses import replace
from pathlib import Path
import os
import sys

import pytest
import pytest_asyncio

from qwenpaw.plugins.execution import CallUnknown, PluginProcess
from tests.channel_support.process import (
    ROOT,
    alive,
    dependency_site,
    digest,
    eventually,
    launch,
    runtime_layout,
)


@pytest_asyncio.fixture
async def process():
    worker = PluginProcess()
    yield worker
    result = await worker.stop(timeout=0.5)
    assert result.exited and not result.residual


@pytest.mark.asyncio
async def test_lifecycle_bidirectional_ipc_logs_and_repeated_stop(
    tmp_path,
    process,
):
    spec, _ = launch(tmp_path)
    notices = []
    process.register_method(
        "sample.host",
        lambda params, _request: {"received": params},
    )
    process.register_notification(
        "sample.notice",
        lambda params, _request: notices.append(params),
    )
    health = await process.start(spec)
    assert health["state"] == "ready"
    probe = await process.call("sample.probe", {})
    assert (
        probe["pid"] == health["pid"] != os.getpid() and probe["isolated"] == 1
    )
    assert probe["identity"] == {
        "plugin_id": spec.plugin_id,
        "instance_id": spec.instance_id,
        "generation": 1,
    }
    assert await process.call("sample.echo", {"text": "hello"}) == {
        "text": "hello",
    }
    assert await process.call("sample.reverse", {"notice": 7}) == {
        "received": probe["identity"],
    }
    await eventually(lambda: notices == [{"notice": 7}])
    await eventually(lambda: process.health_snapshot()["log_bytes"] > 0)
    stopped = await process.stop()
    assert stopped.exited and not stopped.forced and stopped.returncode == 0
    assert not alive(probe["pid"])
    assert (await process.stop()).exited


@pytest.mark.asyncio
async def test_crash_restarts_generation_and_retains_host_handler(
    tmp_path,
    process,
):
    spec, _ = launch(tmp_path, restart_limit=1, restart_backoff=0.02)
    seen = []
    process.register_method(
        "sample.host",
        lambda params, _request: seen.append(params) or params,
    )
    process.register_notification(
        "sample.notice",
        lambda _params, _request: None,
    )
    first = await process.start(spec)
    with pytest.raises(CallUnknown):
        await process.call("sample.crash", {})
    await eventually(
        lambda: process.health_snapshot()["state"] == "ready"
        and process.health_snapshot()["generation"] == 2,
    )
    fresh = await process.call("sample.probe", {})
    assert fresh["pid"] != first["pid"] and not alive(first["pid"])
    assert (await process.call("sample.reverse", {}))["generation"] == 2
    assert seen == [fresh["identity"]]


@pytest.mark.asyncio
async def test_blocked_sdk_start_keeps_parent_loop_responsive(
    tmp_path,
    process,
):
    spec, marker = launch(tmp_path, mode="blocked")
    ticks = []

    async def heartbeat():
        while not marker.exists():
            ticks.append(1)
            await asyncio.sleep(0.01)

    beating = asyncio.create_task(heartbeat())
    health = await process.start(spec)
    await eventually(marker.exists)
    await beating
    # A parent request/task can run while the child SDK blocks its own loop.
    assert (
        ticks
        and await asyncio.wait_for(asyncio.sleep(0, result="responsive"), 0.1)
        == "responsive"
    )
    assert int(marker.read_text()) == health["pid"]
    stopped = await asyncio.wait_for(process.stop(timeout=0.3), 3)
    assert stopped.exited and stopped.forced and not alive(health["pid"])


@pytest.mark.asyncio
async def test_forced_stop_reaps_uncooperative_process_tree(tmp_path, process):
    spec, marker = launch(tmp_path, mode="tree", restart_limit=2)
    health = await process.start(spec)
    await eventually(marker.exists)
    descendant = int(marker.read_text())
    assert alive(descendant) and alive(health["pid"])
    result = await process.stop(timeout=0.3)
    assert result.exited and result.forced and result.residual == ()
    assert not alive(descendant) and not alive(health["pid"])
    assert process.health_snapshot()["state"] == "stopped"


@pytest.mark.asyncio
async def test_cancel_inflight_call_cleans_up_and_next_request_succeeds(
    tmp_path,
    process,
):
    spec, marker = launch(tmp_path)
    await process.start(spec)
    pending = asyncio.create_task(process.call("sample.slow", {}))
    await eventually(Path(str(marker) + ".pending").exists)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert await process.call("sample.echo", {"after": "cancel"}) == {
        "after": "cancel",
    }
    unknown = process.health_snapshot()["unknown_outcomes"]
    assert len(unknown) == 1 and unknown[0]["method"] == "sample.slow"
    assert unknown[0]["reason_code"] == "PLATFORM_RESULT_UNKNOWN"


@pytest.mark.asyncio
async def test_large_text_and_attachment_payload_roundtrip_is_exact(
    tmp_path,
    process,
):
    spec, _ = launch(tmp_path)
    await process.start(spec)
    binary = bytes(range(256)) * 3300
    value = {
        "text": "  原文\n⟦ unchanged ⟧  ",
        "attachments": [
            "data:application/octet-stream;base64,"
            + base64.b64encode(binary).decode(),
        ],
    }
    assert len(value["attachments"][0]) > 1024 * 1024
    received = await process.call("sample.echo", value)
    assert received == value
    assert (
        base64.b64decode(received["attachments"][0].split(",", 1)[1]) == binary
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("layout", ["source", "frozen"])
async def test_runtime_roots_interpreter_and_dependency_visible_in_worker(
    tmp_path,
    monkeypatch,
    process,
    layout,
):
    from qwenpaw.plugins.channel_bootstrap import (
        resolve_default_channel_runtime,
    )

    monkeypatch.setattr(
        "qwenpaw.config.utils.get_config_path",
        lambda: tmp_path / "config.json",
    )
    if layout == "frozen":
        expected_support, site = runtime_layout(tmp_path, monkeypatch)
        runtime = resolve_default_channel_runtime()
    else:
        site = dependency_site(tmp_path / "dependencies")
        runtime = resolve_default_channel_runtime()
        expected_support = ROOT / "src"
        runtime = replace(runtime, dependency_paths=(site,))
    assert runtime.support_root == expected_support
    assert site in runtime.dependency_paths
    spec, _ = launch(
        tmp_path,
        python_executable=runtime.python_executable,
        support_root=runtime.support_root,
        dependency_paths=runtime.dependency_paths,
    )
    requirements = spec.code_root / "requirements.txt"
    requirements.write_text("sample-shared-sdk==1.0\n")
    spec = replace(
        spec,
        config={**spec.config, "sdk": True},
        requirements_path=requirements,
        source_revision=digest(spec.code_root),
    )
    await process.start(spec)
    probe = await process.call("sample.probe", {})
    assert probe["sdk"] == "offline-sdk" and probe["isolated"] == 1
    assert (
        Path(probe["executable"]).resolve()
        == Path(runtime.python_executable).resolve()
    )
    assert Path(probe["source"]).parent == spec.code_root
    assert "sample_shared_sdk" not in sys.modules


@pytest.mark.asyncio
@pytest.mark.parametrize("broken_source", [False, True])
async def test_shared_upgrade_replaces_provider_or_restores_exact_old_tree(
    tmp_path,
    monkeypatch,
    broken_source,
):
    import shutil
    import subprocess
    import zipfile

    from qwenpaw.plugins.dependency_upgrade import DependencyUpgradeManager
    from tests.channel_support.host import Host, local_wheel

    host = Host(tmp_path, monkeypatch)
    site = tmp_path / "plugin_runtime/site"
    site.mkdir(parents=True)
    host.site = site
    monkeypatch.setattr(sys, "path", sys.path.copy())
    monkeypatch.setenv("QWENPAW_PLUGIN_SITE", str(site))
    host.registry.channel_runtime = replace(
        host.registry.channel_runtime,
        dependency_paths=(site,),
        dependency_install_target=site,
    )
    old = local_wheel(tmp_path / "old", version="0.5", value="0.5")
    with zipfile.ZipFile(next(old.glob("*.whl"))) as archive:
        archive.extractall(site)
    module = site / "sample_shared_sdk.py"
    module.chmod(0o640)
    (site / "neighbor.txt").write_bytes(b"preserve unrelated bytes\x00")
    before, mode = digest(site), module.stat().st_mode
    source = host.artifact(sdk=True)
    published = tmp_path / "published-sdk"
    if broken_source:
        (source / "sample.py").write_text(
            "from pathlib import Path\n"
            "import sample_shared_sdk as sdk\n"
            f"Path({str(published)!r}).write_text(\n"
            "    sdk.VALUE + '\\n' + Path(sdk.__file__).read_text())\n"
            "raise RuntimeError('new source cannot initialize')\n",
        )
    wheel = next(local_wheel(tmp_path / "new", value="1.0").glob("*.whl"))

    def resolve(command, **kwargs):
        assert "wheel" in command and "install" not in command
        target = Path(command[command.index("--wheel-dir") + 1])
        shutil.copy2(wheel, target / wheel.name)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(
        host.loader,
        "_run_subprocess_with_streaming_log",
        resolve,
    )
    try:
        manager = DependencyUpgradeManager(host.loader)
        operation = await manager.stage(source)
        assert operation["status"] == "needs_confirmation"
        assert operation["changes"] == [
            {
                "name": "sample-shared-sdk",
                "current_version": "0.5",
                "target_version": "1.0",
            },
        ]
        assert digest(site) == before and not operation["requires_restart"]
        assert not published.exists()
        result = await manager.apply(operation["id"])
        if broken_source:
            assert result["status"] == "failed", result
            assert (
                result["error"]["code"] == "DEPENDENCY_UPGRADE_FAILED"
            ), result
            assert published.read_text() == "1.0\nVALUE = '1.0'\n"
            assert digest(site) == before and module.stat().st_mode == mode
            assert not host.installed.exists()
        else:
            assert result["status"] == "succeeded", result
            assert module.read_text() == "VALUE = '1.0'\n"
            assert not (site / "sample_shared_sdk-0.5.dist-info").exists()
            assert (site / "sample_shared_sdk-1.0.dist-info/RECORD").is_file()
            assert (host.installed / "sample.py").read_bytes() == (
                source / "sample.py"
            ).read_bytes()
            assert (await manager.apply(operation["id"])) == result
        assert (site / "neighbor.txt").read_bytes() == (
            b"preserve unrelated bytes\x00"
        )
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_frozen_ordinary_loader_gate_installs_missing_sdk_in_overlay(
    tmp_path,
    monkeypatch,
):
    import importlib
    import json
    import shutil

    from qwenpaw.plugins.channel_bootstrap import (
        resolve_default_channel_runtime,
    )
    from tests.channel_support.host import Host, local_wheel

    monkeypatch.setattr(sys, "path", sys.path.copy())
    host = Host(tmp_path, monkeypatch)
    support, packaged = runtime_layout(tmp_path, monkeypatch)
    (packaged / "sample_shared_sdk.py").unlink()
    shutil.rmtree(packaged / "sample_shared_sdk-1.0.dist-info")
    runtime = resolve_default_channel_runtime()
    host.registry.channel_runtime = runtime
    monkeypatch.setenv(
        "QWENPAW_PLUGIN_SITE",
        str(runtime.dependency_install_target),
    )
    source = tmp_path / "ordinary-plugin"
    source.mkdir()
    (source / "requirements.txt").write_text("sample-shared-sdk==1.0\n")
    (source / "plugin.json").write_text(
        json.dumps(
            {
                "id": "ordinary-plugin",
                "version": "1.0.0",
                "entry": {"backend": "main.py"},
            },
        ),
    )
    (source / "main.py").write_text(
        "import sample_shared_sdk\n"
        "class Plugin:\n"
        "    def register(self, api): pass\n"
        "    def sdk_value(self): return sample_shared_sdk.VALUE\n"
        "plugin = Plugin()\n",
    )
    wheels = local_wheel(tmp_path / "wheels")
    for name, value in {
        "PIP_NO_INDEX": "1",
        "PIP_FIND_LINKS": str(wheels),
        "PIP_NO_CACHE_DIR": "1",
        "PIP_CONFIG_FILE": os.devnull,
    }.items():
        monkeypatch.setenv(name, value)
    packaged_assets = [
        support,
        packaged,
        Path(runtime.python_executable).parent,
        support.parent
        / "runtime"
        / (
            "Lib/site-packages"
            if os.name == "nt"
            else (
                f"lib/python{sys.version_info.major}.{sys.version_info.minor}/"
                "site-packages"
            )
        ),
    ]
    before = [digest(path) for path in packaged_assets]
    manifest = (support.parent / "runtime.json").read_bytes()
    try:
        absent = await host.loader._ensure_dependencies_installed(
            source,
            "ordinary-plugin",
            allow_install=False,
        )
        assert not absent.already_satisfied and absent.missing
        record = await host.loader.load_plugin_from_path(source)
        assert record.status == "active", record.diagnostics
        assert record.instance.sdk_value() == "offline-sdk"
        sdk = importlib.import_module("sample_shared_sdk")
        assert sdk.VALUE == "offline-sdk"
        assert Path(sdk.__file__).parent == runtime.dependency_install_target
        assert (
            runtime.dependency_install_target
            / ("sample_shared_sdk-1.0.dist-info/RECORD")
        ).is_file()
        checked = await host.loader._ensure_dependencies_installed(
            source,
            "ordinary-plugin",
            allow_install=False,
        )
        assert checked.already_satisfied and not checked.missing
        assert [digest(path) for path in packaged_assets] == before
        assert (support.parent / "runtime.json").read_bytes() == manifest
    finally:
        await host.loader.unload_plugin("ordinary-plugin")
        sys.modules.pop("sample_shared_sdk", None)
        importlib.invalidate_caches()
        await host.close()
