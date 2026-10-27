# -*- coding: utf-8 -*-
"""Normal generic plugin supervision through actual OS workers and IPC."""

import asyncio
import base64
from dataclasses import replace
from pathlib import Path
import os
import sys
import threading

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
async def test_cancelled_payload_write_retains_cleanup_until_retry(
    monkeypatch,
):
    from qwenpaw.plugins.execution.payloads import PayloadStore

    store = await PayloadStore.acreate()
    process = PluginProcess()
    process._payloads = store
    assert process._process is None and process._spawn_task is None
    loop = asyncio.get_running_loop()
    entered, release = threading.Event(), threading.Event()
    original_open = store._open
    opened = []

    def blocked_open(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        fd = original_open(*args, **kwargs)
        opened.append(fd)
        return fd

    monkeypatch.setattr(store, "_open", blocked_open)
    pending = asyncio.create_task(
        store.stage(
            '"' + "x" * (1024 * 1024) + '"',
            max_frame_bytes=64,
            deadline=loop.time() + 5,
        ),
    )
    try:
        await eventually(entered.is_set)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        stopped = await process.stop(timeout=0.02)
        assert not stopped.exited
        stopped = await asyncio.shield(process._cleanup_task)
        assert stopped.residual == ("PAYLOAD_STORE_REMAINING",)
        assert process._payloads is store
        assert store.root.is_dir()
        assert store.pending_snapshot()["pending_files"] == 1
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        try:
            stopped = await process.stop(timeout=3)
            assert stopped.exited and not stopped.residual
            assert process._payloads is None
            assert not store.root.exists()
            repeated = await process.stop(timeout=0.1)
            assert repeated.exited and not repeated.residual
        finally:
            assert await store.cleanup(deadline=loop.time() + 3)
    assert opened  # The late write still had a valid directory handle.
    assert not store.root.exists()
    assert store.pending_snapshot()["pending_files"] == 0


@pytest.mark.asyncio
async def test_payload_quota_waits_for_confirmed_discard(monkeypatch):
    from qwenpaw.plugins.execution.errors import FrameLimitError
    from qwenpaw.plugins.execution.payloads import PayloadLimits, PayloadStore

    message = '"' + "原" * (400 * 1024) + '"'
    size = len(message.encode("utf-8"))
    store = await PayloadStore.acreate(
        PayloadLimits(max_pending_files=1, max_pending_bytes=size),
    )
    loop = asyncio.get_running_loop()
    entered, release = threading.Event(), threading.Event()
    original_unlink = store._unlink

    async def stage():
        return await store.stage(
            message,
            max_frame_bytes=64,
            deadline=loop.time() + 5,
        )

    try:
        results = await asyncio.gather(
            stage(),
            stage(),
            return_exceptions=True,
        )
        assert sum(isinstance(row, FrameLimitError) for row in results) == 1
        reference, token = next(
            row for row in results if isinstance(row, tuple)
        )
        assert len(reference.encode("utf-8")) < 1024

        def blocked_unlink(direction, name):
            if name == token + ".json":
                entered.set()
                assert release.wait(3)
            return original_unlink(direction, name)

        monkeypatch.setattr(store, "_unlink", blocked_unlink)
        store.discard(token)
        await eventually(entered.is_set)
        snapshot = store.pending_snapshot()
        assert snapshot["pending_files"] == 1
        assert snapshot["pending_bytes"] == size
        with pytest.raises(FrameLimitError):
            await stage()
        release.set()
        await store.adiscard(token)
        assert store.pending_snapshot()["pending_bytes"] == 0
        _, replacement = await stage()
        await store.adiscard(replacement)
    finally:
        release.set()
        assert await store.cleanup(deadline=loop.time() + 3)
    assert not store.root.exists()


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
    from qwenpaw.envs import store
    from qwenpaw.plugins import dependency_maintenance as maintenance

    selected_root = maintenance.state_root()
    key = "QWENPAW_DEPENDENCY_STATE_DIR"
    store._apply_to_environ({key: str(tmp_path / "untrusted-state")})
    assert maintenance.state_root() == selected_root
    monkeypatch.setenv(key, str(tmp_path / "direct-mutation"))
    await process.start(spec)
    probe = await process.call("sample.probe", {})
    assert Path(probe["dependency_state"]) == selected_root
    assert any(
        row["pid"] == probe["pid"] for row in maintenance.live_readers([site])
    )
    with pytest.raises(RuntimeError, match="BUSY"):
        with maintenance.maintenance([site], "test", "token"):
            pytest.fail("active worker allowed maintenance")
    assert probe["sdk"] == "offline-sdk" and probe["isolated"] == 1
    assert (
        Path(probe["executable"]).resolve()
        == Path(runtime.python_executable).resolve()
    )
    assert Path(probe["source"]).parent == spec.code_root
    assert "sample_shared_sdk" not in sys.modules


@pytest.mark.parametrize(
    ("layout", "allowed"),
    [
        ("conda-overlay", True),
        ("conda-native", False),
        ("venv-native", True),
        ("system", False),
        ("conda-overlap", False),
        ("conda-symlink", False),
        ("conda-native-alias", False),
        ("immutable-alias", False),
    ],
)
def test_dependency_site_permission_follows_real_target(
    tmp_path,
    layout,
    allowed,
):
    from qwenpaw.plugins.dependency_context import DependencyEnvironmentContext
    from qwenpaw.plugins.dependency_transaction import (
        dependency_site_is_mutable,
    )

    prefix, managed = tmp_path / "python", tmp_path / "plugin_runtime"
    native, overlay = prefix / "lib/site-packages", managed / "site"
    native.mkdir(parents=True)
    overlay.mkdir(parents=True)
    if layout.startswith("conda"):
        (prefix / "conda-meta").mkdir()
    elif layout == "venv-native":
        (prefix / "pyvenv.cfg").write_text("home = /synthetic\n")
    site = (
        overlay if layout in {"conda-overlay", "immutable-alias"} else native
    )
    if layout == "conda-overlap":
        managed = prefix
    elif layout == "conda-symlink":
        site = managed / "linked"
        site.symlink_to(native, target_is_directory=True)
    context = None
    if layout.endswith("-alias"):
        alias = (
            prefix.with_name("PYTHON")
            if layout.startswith("conda")
            else managed.with_name("PLUGIN_RUNTIME")
        )
        if not alias.exists() or not alias.samefile(
            prefix if layout.startswith("conda") else managed,
        ):
            pytest.skip("filesystem has no case aliases")
        immutable = () if layout.startswith("conda") else (alias,)
        prefix = alias if layout.startswith("conda") else prefix
        context = DependencyEnvironmentContext(
            str(prefix / "bin/python"),
            ROOT / "src",
            (site,),
            site,
            immutable,
        ).to_mapping()
    assert (
        dependency_site_is_mutable(site, prefix, managed, context) is allowed
    )


@pytest.mark.parametrize(
    "layout",
    [
        "conda-overlay",
        "explicit-target",
        "sibling-target",
        "conda-native",
        "system",
        "bundle",
        "immutable-provider",
        "overlay-bin",
        "overlay-Scripts",
        "venv-launcher",
        "venv-Scripts-launcher",
        "venv-package-cache-before",
        "venv-package-cache-late",
        "policy-change",
        "conda-native-alias",
        "bundle-alias",
        "sibling-target-alias",
        "venv-launcher-alias",
    ],
)
def test_dependency_transaction_enforces_site_and_file_permission(
    tmp_path,
    layout,
):
    from functools import partial
    import csv
    import py_compile
    import shutil
    import stat
    import subprocess
    import zipfile

    from packaging.markers import default_environment

    from qwenpaw.plugins.dependency_context import DependencyEnvironmentContext
    from qwenpaw.plugins.dependency_gate import SharedDependencyError
    from qwenpaw.plugins.dependency_transaction import (
        _owned,
        publish,
        rollback,
        verify_payload,
    )
    from qwenpaw.plugins.dependency_upgrade import DependencyUpgradeManager
    from qwenpaw.plugins.execution.dependencies import (
        DependencySnapshot,
        _installed_metadata,
    )
    from tests.channel_support.host import (
        _local_dependency_wheel,
        local_certifi_wheel,
    )

    aliased = layout.endswith("-alias")
    layout = layout.removesuffix("-alias")
    package = layout.startswith("venv-package-cache-")
    module_path = (
        "sample_shared_sdk/__init__.py" if package else "sample_shared_sdk.py"
    )
    native_launcher = layout in {"venv-launcher", "venv-Scripts-launcher"}
    directory = tmp_path / "operations" / "reviewed"
    directory.mkdir(parents=True)
    prefix = tmp_path / "python"
    prefix.mkdir()
    native = prefix / "lib/python3/site-packages"
    site = tmp_path / "plugin_runtime/site"
    if layout in {"conda-native", "system", "policy-change"} or (
        native_launcher or package
    ):
        site = native
    elif layout in {"explicit-target", "sibling-target", "bundle"}:
        site = tmp_path / "host-selected/site"
    site.mkdir(parents=True)
    if aliased:
        alias = prefix.with_name("PYTHON")
        if not alias.exists() or not alias.samefile(prefix):
            pytest.skip("filesystem has no case aliases")
        if layout in {"conda-native", "venv-launcher"}:
            prefix = alias
    if layout.startswith("conda"):
        (prefix / "conda-meta").mkdir()
    elif layout != "system":
        (prefix / "pyvenv.cfg").write_text("home = /synthetic\n")
    old = _local_dependency_wheel(
        tmp_path / "old",
        "sample-shared-sdk",
        "0.5",
        {module_path: b"VALUE = '0.5'\n"},
    )
    if package:
        installed = subprocess.run(
            [
                sys.executable,
                "-I",
                "-m",
                "pip",
                "install",
                "--no-index",
                "--no-deps",
                "--disable-pip-version-check",
                "--target",
                str(site),
                str(old),
            ],
            capture_output=True,
            text=True,
        )
        assert installed.returncode == 0, installed.stderr
        record = site / "sample_shared_sdk-0.5.dist-info/RECORD"
        pip_record = record.read_bytes()
        cache_rows = [
            row
            for row in csv.reader(pip_record.decode().splitlines())
            if row[0].endswith(".pyc")
        ]
        assert len(cache_rows) == 1
        package_cache = site / cache_rows[0][0]
        assert package_cache.is_file()
        if layout.endswith("-late"):
            package_cache.unlink()
        else:
            package_cache.chmod(0o600)
    else:
        with zipfile.ZipFile(old) as archive:
            archive.extractall(site)
    immutable = ()
    if layout in {"bundle", "immutable-provider"}:
        bundled = site if layout == "bundle" else tmp_path / "bundle/site"
        bundled.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(old) as archive:
            archive.extractall(bundled)
        immutable = (bundled,)
        if aliased:
            immutable = (
                bundled.parent.with_name(bundled.parent.name.upper())
                / bundled.name,
            )
    certifi = local_certifi_wheel(tmp_path / "certifi")
    with zipfile.ZipFile(certifi) as archive:
        archive.extractall(site)
    module = site / module_path
    module.chmod(0o640)
    (site / "neighbor.txt").write_bytes(b"unowned\x00")
    if package:
        (site / "neighbor.txt").chmod(0o604)
        neighbor_mode = stat.S_IMODE((site / "neighbor.txt").stat().st_mode)
    launcher = None
    if layout in {"overlay-bin", "overlay-Scripts"} or native_launcher:
        scripts = "Scripts" if "Scripts" in layout else "bin"
        launcher = prefix / scripts / ("sdk.py" if native_launcher else "sdk")
        launcher.parent.mkdir()
        launcher.write_bytes(b"#!/synthetic/python\nprint('old launcher')\n")
        launcher.chmod(0o751)
        launcher_mode = stat.S_IMODE(launcher.stat().st_mode)
        record = site / "sample_shared_sdk-0.5.dist-info/RECORD"
        with record.open("a") as stream:
            stream.write(
                Path(os.path.relpath(launcher, site)).as_posix() + ",,\n",
            )
        if native_launcher:
            precache = launcher.parent / "__pycache__/sdk.cpython-313.pyc"
            py_compile.compile(
                str(launcher),
                cfile=str(precache),
                doraise=True,
            )
            precache.chmod(0o600)
    rows, invalid, _ = _installed_metadata((site,))
    assert not invalid
    snapshot = DependencySnapshot(
        str(prefix / "bin/python"),
        str(prefix),
        (str(site),),
        default_environment(),
        ("py3-none-any",),
        rows,
        (str(site),),
    )
    target = site.parent / "other" if layout == "sibling-target" else site
    if aliased and layout == "sibling-target":
        target = (
            target.parent.with_name(target.parent.name.upper()) / target.name
        )
    context = DependencyEnvironmentContext(
        snapshot.executable,
        ROOT / "src",
        (site,),
        target,
        immutable,
    )
    if aliased and layout == "conda-native":
        context = DependencyEnvironmentContext.from_snapshot(snapshot)
    requirements = ["sample-shared-sdk==1.0"]
    requirements_path = tmp_path / "requirements.txt"
    requirements_path.write_text("sample-shared-sdk==1.0\n")
    wheel = _local_dependency_wheel(
        tmp_path / "new",
        "sample-shared-sdk",
        "1.0",
        {module_path: b"VALUE = '1.0'\n"},
    )
    args = directory, snapshot, requirements, []
    resolve = partial(
        DependencyUpgradeManager._prepare_resolution,
        *args,
        requirements_path,
        context,
    )
    prepare = partial(
        DependencyUpgradeManager._prepare_payload,
        *args,
        site,
        context,
    )

    def inventory(root=tmp_path):
        return {
            path.relative_to(root).as_posix(): (
                path.read_bytes() if path.is_file() else None,
                stat.S_IMODE(path.stat().st_mode),
            )
            for path in root.rglob("*")
        }

    before = inventory()
    protected = layout in {
        "sibling-target",
        "conda-native",
        "system",
        "bundle",
        "immutable-provider",
    }
    if protected:
        with pytest.raises(
            SharedDependencyError,
            match="bundled|environment-managed",
        ) as error:
            resolve()
        assert error.value.reason_code == "DEPENDENCY_ENVIRONMENT_PROTECTED"
        assert inventory() == before
        (directory / "wheels").mkdir()
    else:
        resolve()
    shutil.copy2(wheel, directory / "wheels" / wheel.name)
    if protected or layout.startswith("overlay-"):
        before = inventory()
        reason = (
            "DEPENDENCY_ENVIRONMENT_PROTECTED|bundled"
            if protected
            else "DEPENDENCY_PLAN_UNVERIFIED"
        )
        with pytest.raises(RuntimeError, match=reason):
            prepare()
        assert inventory() == before
        return
    _, changes, proposed, payload = prepare()
    operation = {
        "id": directory.name,
        "environment": snapshot.to_mapping(),
        "dependency_context": context.to_mapping(),
        "changes": changes,
        "proposed": proposed,
        "payload": payload,
    }
    if layout == "policy-change":
        from qwenpaw.plugins.dependency_maintenance import repair_marker

        (prefix / "conda-meta").mkdir()
        before = inventory()
        assert not repair_marker(site).exists()
        for write in (verify_payload, publish):
            with pytest.raises(
                RuntimeError,
                match="DEPENDENCY_ENVIRONMENT_PROTECTED",
            ):
                write(directory, operation)
            assert inventory() == before
            assert not repair_marker(site).exists()
        return
    cache = None
    if native_launcher:
        cache = launcher.parent / "__pycache__/sdk.cpython-313.opt-1.pyc"
        py_compile.compile(
            str(launcher),
            cfile=str(cache),
            doraise=True,
            optimize=1,
        )
        cache.chmod(0o640)
        runtime_before = inventory(prefix), inventory(site)
    if package:
        if layout.endswith("-late"):
            py_compile.compile(str(module), doraise=True)
            package_cache.chmod(0o600)
        assert package_cache.is_file()
        assert record.read_bytes() == pip_record
        runtime_before = inventory(prefix), inventory(site)
    publish(directory, operation)
    assert module.read_text() == "VALUE = '1.0'\n"
    assert not (site / "sample_shared_sdk-0.5.dist-info").exists()
    assert (site / "sample_shared_sdk-1.0.dist-info/RECORD").is_file()
    assert (site / "neighbor.txt").read_bytes() == b"unowned\x00"
    if launcher is not None:
        assert (
            launcher.read_bytes()
            == b"#!/synthetic/python\nprint('old launcher')\n"
        )
        assert stat.S_IMODE(launcher.stat().st_mode) == launcher_mode
        record = site / "sample_shared_sdk-1.0.dist-info/RECORD"
        assert (
            sum(
                (site / row[0]).samefile(launcher)
                for row in csv.reader(record.read_text().splitlines())
            )
            == 1
        )
        assert not cache.exists() and not precache.exists()
        rollback(directory)
        assert (inventory(prefix), inventory(site)) == runtime_before
    if package:
        assert not package_cache.exists()
        assert (
            stat.S_IMODE((site / "neighbor.txt").stat().st_mode)
            == neighbor_mode
        )
        rollback(directory)
        assert (inventory(prefix), inventory(site)) == runtime_before
        record.write_bytes(
            pip_record + (",".join(cache_rows[0]) + "\n").encode(),
        )
        with pytest.raises(
            RuntimeError,
            match="duplicate installed ownership",
        ):
            _owned(rows["sample-shared-sdk"], prefix)
        record.write_bytes(pip_record)
        assert (inventory(prefix), inventory(site)) == runtime_before


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
    from tests.channel_support.host import (
        Host,
        local_certifi_wheel,
        local_wheel,
    )

    host = Host(tmp_path, monkeypatch)
    site = tmp_path / "plugin_runtime/site"
    site.mkdir(parents=True)
    alias = site.with_name("SITE")
    if alias.exists() and alias.samefile(site):
        site = alias
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
    certifi_wheel = local_certifi_wheel(wheel.parent)

    def resolve(command, **kwargs):
        assert "wheel" in command and "install" not in command
        target = Path(command[command.index("--wheel-dir") + 1])
        shutil.copy2(wheel, target / wheel.name)
        shutil.copy2(certifi_wheel, target / certifi_wheel.name)
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
    from tests.channel_support.host import (
        Host,
        local_certifi_wheel,
        local_wheel,
    )

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
    local_certifi_wheel(wheels)
    for name, value in {
        "PIP_NO_INDEX": "1",
        "PIP_FIND_LINKS": str(wheels),
        "PIP_NO_CACHE_DIR": "1",
        "PIP_CONFIG_FILE": os.devnull,
    }.items():
        monkeypatch.setenv(name, value)
    from qwenpaw.plugins.dependency_maintenance import marker, repair_marker
    from qwenpaw.plugins.dependency_context import DependencyEnvironmentContext
    from qwenpaw.plugins.install_lock import environment_install_lock_path

    resources = support.parent

    def bundle_inventory():
        # Signing covers the whole resource tree, including empty directories
        # and coordination sidecars beside the packaged dependency sites.
        return {
            path.relative_to(resources).as_posix(): (
                ("symlink", os.readlink(path))
                if path.is_symlink()
                else (
                    ("directory", None)
                    if path.is_dir()
                    else ("file", path.read_bytes())
                )
            )
            for path in resources.rglob("*")
        }

    for site in runtime.dependency_paths:
        for path in (
            environment_install_lock_path(site),
            marker(site),
            repair_marker(site),
        ):
            assert not path.is_relative_to(resources)
    native_site = Path(runtime.python_executable).parent.parent / (
        "Lib/site-packages"
        if os.name == "nt"
        else f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
    )
    context = DependencyEnvironmentContext.from_runtime(runtime)
    assert context.protected(native_site)
    assert not context.protected(runtime.dependency_install_target)

    before = bundle_inventory()
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
        assert bundle_inventory() == before
    finally:
        try:
            if host.loader.get_loaded_plugin("ordinary-plugin") is not None:
                await host.loader.unload_plugin("ordinary-plugin")
        finally:
            sys.modules.pop("sample_shared_sdk", None)
            importlib.invalidate_caches()
            await host.close()
    assert bundle_inventory() == before


def test_executor_inspection_preserves_immutable_support_tree(tmp_path):
    import shutil
    import packaging
    from qwenpaw.plugins.dependency_context import DependencyEnvironmentContext

    support = tmp_path / "Bundle.app/Contents/Resources/site"
    support.mkdir(parents=True)
    shutil.copytree(
        Path(packaging.__file__).parent,
        support / "packaging",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    before = {
        p.relative_to(support): p.read_bytes()
        for p in support.rglob("*")
        if p.is_file()
    }
    context = DependencyEnvironmentContext(
        sys.executable,
        support,
        (support,),
        None,
        (support,),
    )
    assert context.inspect_executor()
    after = {
        p.relative_to(support): p.read_bytes()
        for p in support.rglob("*")
        if p.is_file()
    }
    assert set(after) == set(before)
    assert all(after[path] == value for path, value in before.items())
