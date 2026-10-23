# -*- coding: utf-8 -*-
"""Fresh installed metadata must attest actual loaded host dependencies."""

import asyncio
import csv
import importlib.util
from importlib.metadata import PathDistribution
import io
from pathlib import Path
import sys
import threading
from types import ModuleType, SimpleNamespace

import pytest

from qwenpaw.plugins.dependency_usage import DependencyDeclaration
from qwenpaw.plugins.python_backend import PythonBackend

pytestmark = pytest.mark.asyncio


def _record(*rows):
    output = io.StringIO()
    csv.writer(output).writerows(rows)
    return output.getvalue()


@pytest.fixture(name="host")
def host_fixture(tmp_path, monkeypatch, fresh_registry):
    """Bound actual metadata discovery while publishing real usage evidence."""
    site = tmp_path / "site"
    site.mkdir()
    # Use a real, bounded installed environment; no inventory result is mocked.
    monkeypatch.setattr(sys, "path", [str(site)])
    usage = fresh_registry.dependency_usage
    observations = []

    def metadata(name="observer-owner", *, record=None, top="observer_module"):
        info = site / (name.replace("-", "_") + "-1.0.dist-info")
        info.mkdir(exist_ok=True)
        (info / "METADATA").write_text(
            f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n",
        )
        (info / "top_level.txt").write_text(top + "\n")
        if record is not None:
            (info / "RECORD").write_text(record)
        return info

    def load(path, name="observer_module"):
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        assert Path(module.__file__).absolute() == Path(path).absolute()
        return module

    async def observe():
        plugin = f"observer-{len(observations)}"
        token = usage.declare(DependencyDeclaration(plugin, "fresh", (), None))
        observations.append(token)
        try:
            await PythonBackend.observe_host_dependencies(
                SimpleNamespace(
                    source_path=tmp_path / "plugin",
                    manifest=SimpleNamespace(id=plugin),
                ),
                usage,
                token,
            )
        finally:
            usage.withdraw(token)
        return tuple(
            use
            for use in usage.snapshot(None).uses
            if use.origin == "host" and use.plugin_id == plugin
        )

    return SimpleNamespace(
        site=site,
        metadata=metadata,
        load=load,
        observe=observe,
        usage=usage,
        description=lambda plugin: SimpleNamespace(
            source_path=tmp_path / "plugin",
            manifest=SimpleNamespace(id=plugin),
        ),
    )


def _versions(observed):
    return {
        name: row["version"]
        for use in observed
        if use.snapshot is not None
        for name, row in use.snapshot.distributions.items()
    }


@pytest.fixture(name="public_inventory")
def public_inventory_fixture():
    """Use the supported interpreter's public inventory as a small oracle."""

    def listed(info, filename):
        return any(
            str(path) == filename
            for path in PathDistribution(info).files or ()
        )

    return listed


async def test_host_ownership_ignores_unloaded_inventory_without_stating_it(
    host,
    monkeypatch,
):
    """Loaded ownership must avoid touching unrelated installed assets."""
    loaded = host.site / "observer_module.py"
    loaded.write_text("VALUE = 'loaded'\n")
    unrelated = [host.site / f"unused_{number}.py" for number in range(256)]
    for path in unrelated:
        path.write_text("VALUE = 'not imported'\n")
    host.metadata(
        record=_record(
            (loaded.name, "", ""),
            *((path.name, "", "") for path in unrelated),
        ),
    )
    host.load(loaded)
    native_stat = Path.stat
    visited = []
    inventory = set(unrelated)

    def count_inventory_stat(path, *args, **kwargs):
        if path in inventory:
            visited.append(path)
        return native_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", count_inventory_stat)
    assert _versions(await host.observe()) == {"observer-owner": "1.0"}
    count = len(visited)
    assert (
        count <= 1
    ), f"Host ownership stated {count} unloaded inventory files"


@pytest.mark.parametrize(
    "record_name,expected",
    [
        ("alias.py", True),
        ("../outside/actual.py", True),
        ("nested/../alias.py", True),
        ("absolute", False),
    ],
)
async def test_record_preserves_lexical_resolved_and_relative_origin_membership(
    host,
    tmp_path,
    public_inventory,
    record_name,
    expected,
):
    """Inventory membership must preserve both imported symlink origins."""
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "actual.py"
    target.write_text("VALUE = 'symlinked source'\n")
    link = host.site / "alias.py"
    link.symlink_to(target)
    (host.site / "nested").mkdir()
    filename = str(target) if record_name == "absolute" else record_name
    info = host.metadata(record=_record((filename, "", "")))
    host.load(link)
    assert bool(_versions(await host.observe())) is expected
    if expected:
        assert _versions(await host.observe()) == {"observer-owner": "1.0"}
    target.unlink()
    if expected and public_inventory(info, filename):
        assert _versions(await host.observe()) == {"observer-owner": "1.0"}
    else:
        assert not (
            await host.observe()
        ), "The public inventory excludes this origin"


@pytest.mark.parametrize("hash_value,size", [("not-a-hash", "-1"), ("", "")])
async def test_record_csv_filename_and_existing_permissive_fields_keep_ownership(
    host,
    hash_value,
    size,
):
    """Quoted filenames and permissive stdlib fields retain current meaning."""
    path = host.site / "comma,name.py"
    path.write_text("VALUE = 'CSV filename'\n")
    host.metadata(record=_record((path.name, hash_value, size)))
    host.load(path)
    assert _versions(await host.observe()) == {"observer-owner": "1.0"}


@pytest.mark.parametrize(
    "inventory,expected",
    [
        ("absent", True),
        ("empty", True),
        ("missing-record", False),
        ("sources", True),
        ("missing-sources", False),
        ("installed-files", True),
    ],
)
async def test_absent_and_empty_existing_inventory_keep_distinct_fallbacks(
    host,
    inventory,
    expected,
):
    """Absent inventory may infer ownership from a top-level module origin."""
    path = host.site / "observer_module.py"
    path.write_text("VALUE = 'legacy source'\n")
    record = {
        "empty": "",
        "missing-record": _record(("missing.py", "", "")),
    }.get(inventory)
    info = host.metadata(record=record)
    if inventory in {"sources", "missing-sources"}:
        filename = path.name if inventory == "sources" else "missing.py"
        (info / "SOURCES.txt").write_text(filename + "\n")
    if inventory == "installed-files":
        (info / "installed-files.txt").write_text("../" + path.name + "\n")
    host.load(path)
    assert bool(_versions(await host.observe())) is expected


@pytest.mark.parametrize(
    "bad_row",
    [
        ("unused.py", "", "not-an-integer"),
        ("unused.py", "", "", "extra-column"),
    ],
)
@pytest.mark.parametrize("bad_distribution", [False, True])
async def test_later_malformed_record_row_cannot_publish_clean_host_evidence(
    host,
    bad_row,
    bad_distribution,
):
    """A matched early row cannot hide malformed later inventory evidence."""
    path = host.site / "observer_module.py"
    path.write_text("VALUE = 'matched before malformed row'\n")
    first = (path.name, "", "")
    record = _record(first) if bad_distribution else _record(first, bad_row)
    host.metadata(record=record)
    if bad_distribution:
        host.metadata(
            "unrelated-broken",
            record=_record(bad_row),
            top="unused",
        )
    host.load(path)
    (unknown,) = await host.observe()
    assert unknown.environment is None
    assert unknown.snapshot is None


@pytest.mark.parametrize(
    "denied",
    ["unloaded", "loaded", "record-read-error", "record-permission"],
)
async def test_only_unloaded_asset_stat_errors_are_irrelevant_to_host_ownership(
    host,
    monkeypatch,
    public_inventory,
    denied,
):
    """Actual origin and unsuppressed read failures must remain unknown."""
    loaded, unused = host.site / "observer_module.py", host.site / "unused.py"
    loaded.write_text("VALUE = 'loaded'\n")
    unused.write_text("VALUE = 'unloaded'\n")
    info = host.metadata(
        record=_record((loaded.name, "", ""), (unused.name, "", "")),
    )
    host.load(loaded)
    native_stat, native_open = Path.stat, Path.open

    def guarded_stat(path, *args, **kwargs):
        target = unused if denied == "unloaded" else loaded
        if denied in {"unloaded", "loaded"} and path == target:
            raise PermissionError("controlled installed-file access failure")
        return native_stat(path, *args, **kwargs)

    def guarded_open(path, *args, **kwargs):
        if path == info / "RECORD":
            if denied == "record-read-error":
                raise OSError("controlled inventory I/O failure")
            if denied == "record-permission":
                # PathDistribution.read_text treats this as absent metadata.
                raise PermissionError("controlled inventory read permission")
        return native_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", guarded_stat)
    monkeypatch.setattr(Path, "open", guarded_open)
    expected = (
        "owner" if denied in {"unloaded", "record-permission"} else "unknown"
    )
    if denied == "loaded":
        try:
            expected = (
                "owner" if public_inventory(info, loaded.name) else "no-owner"
            )
        except PermissionError:
            expected = "unknown"
    observed = await host.observe()
    if expected == "owner":
        versions = _versions(observed)
        assert versions == {"observer-owner": "1.0"}
    elif expected == "no-owner":
        assert not observed
    else:
        (unknown,) = observed
        assert unknown.environment is None
        assert unknown.snapshot is None


async def test_host_observation_refreshes_record_metadata_and_loaded_origins(
    host,
    monkeypatch,
    public_inventory,
):
    """Inventory, versions, imports and file existence are fresh each time."""
    first, later = host.site / "observer_module.py", host.site / "later.py"
    first.write_text("VALUE = 'first'\n")
    later.write_text("VALUE = 'later'\n")
    info = host.metadata(record=_record((first.name, "", "")))
    host.load(first)
    assert _versions(await host.observe()) == {"observer-owner": "1.0"}
    (info / "RECORD").write_text(_record((later.name, "", "")))
    assert not (
        await host.observe()
    ), "Fresh RECORD must exclude the former filename"
    host.load(later, "observer_later")
    (info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: observer-owner\nVersion: 2.0\n",
    )
    assert _versions(await host.observe()) == {"observer-owner": "2.0"}
    monkeypatch.delitem(sys.modules, "observer_later")
    assert not (
        await host.observe()
    ), "An unimported module cannot keep ownership"
    host.load(later, "observer_later")
    later.unlink()
    if public_inventory(info, later.name):
        assert _versions(await host.observe()) == {"observer-owner": "2.0"}
    else:
        assert not (
            await host.observe()
        ), "The public inventory excludes this origin"


async def test_audit_metadata_read_yields_to_loop(host, monkeypatch):
    """A real installed METADATA read must not stall other loop work."""
    info = host.metadata(record="")
    native_open = Path.open
    entered, heartbeat = threading.Event(), threading.Event()
    release = threading.Event()
    read_threads, progress = [], []
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()

    def pulse():
        progress.append(not release.is_set())
        heartbeat.set()

    def unblock():
        if entered.wait(5):
            loop.call_soon_threadsafe(pulse)
            heartbeat.wait(2)  # Deadlock rescue for the old blocking path.
        release.set()

    def slow_open(path, *args, **kwargs):
        if path == info / "METADATA":
            read_threads.append(threading.get_ident())
            entered.set()
            assert release.wait(5), "metadata read was never released"
        return native_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", slow_open)
    watchdog = threading.Thread(target=unblock, daemon=True)
    watchdog.start()
    try:
        snapshot = await PythonBackend(None).audit_dependencies(
            SimpleNamespace(source_path=host.site.parent / "plugin"),
        )
        await asyncio.sleep(0)
        assert snapshot.versions == {"observer-owner": "1.0"}
        assert progress == [True], "event loop stalled during METADATA I/O"
        assert read_threads and set(read_threads) != {loop_thread}
    finally:
        release.set()
        watchdog.join(5)


@pytest.fixture(name="file_gate")
def file_gate_fixture(monkeypatch):
    """Pause an actual file read while the loop can inspect its owners."""
    native_open = Path.open
    gates = []

    def install(target, *, on_read=None):
        loop = asyncio.get_running_loop()
        targets = (target,) if isinstance(target, Path) else target
        gate = SimpleNamespace(
            entered=asyncio.Event(),
            release=threading.Event(),
            threads=[],
        )
        gates.append(gate)

        def slow_open(path, *args, **kwargs):
            if path in targets:
                gate.threads.append(threading.get_ident())
                if len(gate.threads) == 1 or on_read is not None:
                    release = (
                        threading.Event()
                        if on_read is not None
                        else gate.release
                    )

                    def entered():
                        gate.entered.set()
                        if on_read is not None:
                            try:
                                on_read(len(gate.threads))
                            finally:
                                release.set()

                    loop.call_soon_threadsafe(entered)
                    assert release.wait(5), "real file read never released"
            return native_open(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", slow_open)
        return gate

    yield install
    for gate in gates:
        gate.release.set()


def _declare(host, plugin="async-observer"):
    return host.usage.declare(
        DependencyDeclaration(plugin, "fresh", (), None),
    )


def _host_uses(host, plugin="async-observer"):
    return tuple(
        row
        for row in host.usage.snapshot(None).uses
        if row.origin == "host" and row.plugin_id == plugin
    )


def _loaded_owner(host):
    path = host.site / "observer_module.py"
    path.write_text("VALUE = 'real installed source'\n")
    info = host.metadata(record=_record((path.name, "", "")))
    host.load(path)
    return info


async def test_host_record_read_and_publication_keep_loop_ownership(
    host,
    monkeypatch,
    file_gate,
):
    """Thread only file evidence; snapshot and registry access stay on loop."""
    from qwenpaw.plugins import python_backend

    info = _loaded_owner(host)
    loop_thread = threading.get_ident()
    snapshot_threads, mutation_threads = [], []

    class LoopPaths(list):
        def __iter__(self):
            snapshot_threads.append(threading.get_ident())
            assert threading.get_ident() == loop_thread
            return super().__iter__()

    class LoopModule(ModuleType):
        def __getattribute__(self, name):
            if name in {"__dict__", "__file__"}:
                snapshot_threads.append(threading.get_ident())
                assert threading.get_ident() == loop_thread
            return super().__getattribute__(name)

    sentinel = LoopModule("observer_snapshot_sentinel")
    sentinel.__file__ = str(host.site / "observer_module.py")

    class LoopModules(dict):
        def copy(self):
            snapshot_threads.append(threading.get_ident())
            assert threading.get_ident() == loop_thread
            return super().copy()

        def items(self):
            snapshot_threads.append(threading.get_ident())
            assert threading.get_ident() == loop_thread
            return super().items()

    monkeypatch.setattr(
        python_backend,
        "sys",
        SimpleNamespace(
            **{
                **vars(sys),
                "path": LoopPaths(sys.path),
                "modules": LoopModules(
                    {**sys.modules, sentinel.__name__: sentinel},
                ),
            },
        ),
    )
    for method in ("refine", "observe_host"):
        original = getattr(host.usage, method)

        def mutate(*args, _original=original, **kwargs):
            mutation_threads.append(threading.get_ident())
            assert threading.get_ident() == loop_thread
            return _original(*args, **kwargs)

        monkeypatch.setattr(host.usage, method, mutate)
    gate = file_gate(info / "RECORD")
    token = _declare(host)
    task = asyncio.create_task(
        PythonBackend.observe_host_dependencies(
            host.description("async-observer"),
            host.usage,
            token,
        ),
    )
    try:
        await asyncio.wait_for(gate.entered.wait(), 5)
        assert not gate.release.is_set() and not task.done()
        assert not _host_uses(host), "evidence published before RECORD read"
        assert any(
            row.origin == "declaration"
            for row in host.usage.snapshot(None).uses
        )
        gate.release.set()
        await task
        assert _versions(_host_uses(host)) == {"observer-owner": "1.0"}
        assert snapshot_threads and set(snapshot_threads) == {loop_thread}
        assert mutation_threads and set(mutation_threads) == {loop_thread}
        assert gate.threads and loop_thread not in gate.threads
    finally:
        gate.release.set()
        await asyncio.gather(task, return_exceptions=True)
        host.usage.withdraw(token)


async def test_import_during_record_scan_is_included_on_retry(host, file_gate):
    """Imports during an await cannot disappear from published host usage."""
    info = _loaded_owner(host)
    later = host.site / "observer_later.py"
    later.write_text("VALUE = 'imported while scanning'\n")
    host.metadata(
        "later-owner",
        record=_record((later.name, "", "")),
        top="observer_later",
    )
    gate = file_gate(info / "RECORD")
    token = _declare(host)
    task = asyncio.create_task(
        PythonBackend.observe_host_dependencies(
            host.description("async-observer"),
            host.usage,
            token,
        ),
    )
    try:
        await asyncio.wait_for(gate.entered.wait(), 5)
        host.load(later, "observer_later")
        gate.release.set()
        await task
        assert _versions(_host_uses(host)) == {
            "observer-owner": "1.0",
            "later-owner": "1.0",
        }
        assert (
            len(gate.threads) == 2
        ), "fresh import requires one bounded rescan"
    finally:
        gate.release.set()
        await asyncio.gather(task, return_exceptions=True)
        host.usage.withdraw(token)


async def test_continued_import_churn_publishes_unknown_after_two_scans(
    host,
    monkeypatch,
    file_gate,
):
    """An unstable host must settle conservatively instead of retry forever."""
    info = _loaded_owner(host)

    def import_during_scan(number):
        monkeypatch.setitem(
            sys.modules,
            f"observer_churn_{number}",
            SimpleNamespace(__file__=str(host.site / f"churn_{number}.py")),
        )

    gate = file_gate(info / "RECORD", on_read=import_during_scan)
    token = _declare(host)
    try:
        await asyncio.wait_for(
            PythonBackend.observe_host_dependencies(
                host.description("async-observer"),
                host.usage,
                token,
            ),
            5,
        )
        assert len(gate.threads) == 2
        (unknown,) = _host_uses(host)
        assert unknown.environment is None and unknown.snapshot is None
        (declaration,) = (
            row
            for row in host.usage.snapshot(None).uses
            if row.origin == "declaration"
        )
        assert declaration.environment is None and declaration.snapshot is None
    finally:
        host.usage.withdraw(token)


@pytest.mark.parametrize("replace_token", [False, True])
async def test_withdrawn_exact_token_cannot_publish_or_resurrect(
    host,
    file_gate,
    replace_token,
):
    """A fresh same-plugin declaration cannot authorize an old in-flight scan."""
    info = _loaded_owner(host)
    gate = file_gate(info / "RECORD")
    token = _declare(host)
    replacement = None
    task = asyncio.create_task(
        PythonBackend.observe_host_dependencies(
            host.description("async-observer"),
            host.usage,
            token,
        ),
    )
    try:
        await asyncio.wait_for(gate.entered.wait(), 5)
        host.usage.withdraw(token)
        if replace_token:
            replacement = _declare(host)
        gate.release.set()
        await task
        assert not _host_uses(host)
        declared = tuple(
            row
            for row in host.usage.snapshot(None).uses
            if row.origin == "declaration"
        )
        assert len(declared) == int(replace_token)
        assert all(row.snapshot is None for row in declared)
    finally:
        gate.release.set()
        await asyncio.gather(task, return_exceptions=True)
        host.usage.withdraw(token)
        if replacement is not None:
            host.usage.withdraw(replacement)


async def test_repeated_cancel_settles_file_and_publication_before_unlock(
    host,
    file_gate,
):
    """Cancellation cannot release observation custody before real evidence."""
    from qwenpaw.plugins.install_lock import environment_coordinator
    from qwenpaw.utils.io_utils import run_sync_io

    # Keep executor startup imports outside this stable-snapshot assertion.
    await run_sync_io(host.site.resolve)
    info = _loaded_owner(host)
    gate = file_gate(info / "RECORD")
    token = _declare(host)
    attempting, acquired = asyncio.Event(), asyncio.Event()

    async def installer():
        attempting.set()
        async with environment_coordinator((host.site,)):
            acquired.set()
            assert _versions(_host_uses(host)) == {"observer-owner": "1.0"}

    task = asyncio.create_task(
        PythonBackend.observe_host_dependencies(
            host.description("async-observer"),
            host.usage,
            token,
        ),
    )
    competing = None
    try:
        await asyncio.wait_for(gate.entered.wait(), 5)
        competing = asyncio.create_task(installer())
        await attempting.wait()
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done(), "cancellation escaped unfinished file I/O"
            assert not acquired.is_set(), "writer passed observation custody"
            assert any(
                row.origin == "declaration"
                for row in host.usage.snapshot(None).uses
            )
        gate.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(competing, 5)
        assert _versions(_host_uses(host)) == {"observer-owner": "1.0"}
    finally:
        gate.release.set()
        await asyncio.gather(
            *(item for item in (task, competing) if item is not None),
            return_exceptions=True,
        )
        host.usage.withdraw(token)


@pytest.mark.parametrize("churn", [False, True])
async def test_audit_path_change_retries_or_fails_unverified(
    host,
    monkeypatch,
    churn,
):
    """Admission cannot return inventory for a path set invalidated mid-read."""
    info = host.metadata(record="")
    alternate = host.site.parent / "alternate"
    alternate.mkdir()
    copied = alternate / info.name
    copied.mkdir()
    (copied / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: observer-owner\nVersion: 2.0\n",
    )
    (copied / "top_level.txt").write_text("observer_module\n")
    reads = []
    native_open = Path.open
    loop = asyncio.get_running_loop()

    def changing_open(path, *args, **kwargs):
        if path in (info / "METADATA", copied / "METADATA"):
            reads.append(path)
            release = threading.Event()

            def change():
                if len(reads) == 1 or churn:
                    current = alternate if len(reads) % 2 else host.site
                    monkeypatch.setattr(sys, "path", [str(current)])
                release.set()

            loop.call_soon_threadsafe(change)
            assert release.wait(5), "path-change callback never ran"
        return native_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", changing_open)
    operation = PythonBackend(None).audit_dependencies(
        host.description("async-observer"),
    )
    if churn:
        with pytest.raises((ValueError, RuntimeError)):
            await asyncio.wait_for(operation, 5)
    else:
        snapshot = await asyncio.wait_for(operation, 5)
        assert snapshot.versions == {"observer-owner": "2.0"}
        assert snapshot.dependency_sites == (str(alternate),)
    assert (
        len(reads) == 2
    ), "unstable admission evidence must use bounded retries"


@pytest.mark.parametrize("continued_churn", [False, True])
async def test_host_root_change_rechecks_new_environment_before_publication(
    host,
    monkeypatch,
    file_gate,
    continued_churn,
):
    """A scan cannot attest roots that changed outside its environment lock."""
    info = _loaded_owner(host)
    alternate = host.site.parent / "replacement-site"
    alternate.mkdir()
    source = alternate / "observer_module.py"
    source.write_text("VALUE = 'replacement source'\n")
    replacement = alternate / info.name
    replacement.mkdir()
    (replacement / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: observer-owner\nVersion: 2.0\n",
    )
    (replacement / "RECORD").write_text(_record((source.name, "", "")))

    def replace_environment(number):
        if number == 1 or continued_churn:
            current = alternate if number % 2 else host.site
            monkeypatch.setattr(sys, "path", [str(current)])
            host.load(current / "observer_module.py")

    gate = file_gate(
        (info / "RECORD", replacement / "RECORD"),
        on_read=replace_environment,
    )
    token = _declare(host)
    try:
        await asyncio.wait_for(
            PythonBackend.observe_host_dependencies(
                host.description("async-observer"),
                host.usage,
                token,
            ),
            5,
        )
        assert len(gate.threads) == 2
        if continued_churn:
            (unknown,) = _host_uses(host)
            assert unknown.environment is None and unknown.snapshot is None
        else:
            (owner,) = _host_uses(host)
            assert owner.snapshot.versions == {"observer-owner": "2.0"}
            assert owner.environment.sites == (str(alternate),)
    finally:
        host.usage.withdraw(token)


async def test_no_file_module_churn_does_not_invalidate_host_evidence(
    host,
    monkeypatch,
    file_gate,
):
    """Only origins relevant to ownership can force an observation retry."""
    info = _loaded_owner(host)

    def import_virtual_module(number):
        monkeypatch.setitem(
            sys.modules,
            f"observer_virtual_{number}",
            SimpleNamespace(),
        )

    gate = file_gate(info / "RECORD", on_read=import_virtual_module)
    token = _declare(host)
    try:
        await PythonBackend.observe_host_dependencies(
            host.description("async-observer"),
            host.usage,
            token,
        )
        assert _versions(_host_uses(host)) == {"observer-owner": "1.0"}
        assert len(gate.threads) == 1
    finally:
        host.usage.withdraw(token)
