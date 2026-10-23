# -*- coding: utf-8 -*-
"""Real authenticated bundled sources use the asynchronous I/O boundary."""

import asyncio
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace

import pytest

pytest.register_assert_rewrite("c6_offline_suite", "description_io_suite")
from c6_offline_suite import bundle, save_raw  # noqa: E402
from description_io_suite import SourceIO  # noqa: E402

pytest_plugins = ["c6_offline_suite"]


def bundled_observer(offline, monkeypatch):
    extracted = offline.root / "extractions"
    extracted.mkdir()
    native = tempfile.mkdtemp

    def extract_inside_fixture(*args, **kwargs):
        if kwargs.get("prefix") == "qwenpaw-official-":
            kwargs["dir"] = extracted
        return native(*args, **kwargs)

    monkeypatch.setattr(tempfile, "mkdtemp", extract_inside_fixture)
    return SourceIO(
        monkeypatch,
        SimpleNamespace(
            installed=offline.root / "host/plugins/feishu",
            network=offline.root / "network.jsonl",
        ),
        extra_roots=(extracted,),
    )


@pytest.mark.asyncio
async def test_bundled_provision_describes_source_off_loop(
    offline, monkeypatch
):
    bundle(offline)
    save_raw(
        offline,
        {
            "channel_plugin_state": {
                "version": 1,
                "preset": "light",
                "legacy_pending": ["feishu"],
            }
        },
    )
    observer = bundled_observer(offline, monkeypatch)
    assert (
        await offline.official.provision_bundled_channels(offline.loader) == {}
    )
    source = offline.loader.find_installed_plugin_dir("feishu")
    assert source is not None and (source / "channel.json").is_file()
    record = offline.loader.get_loaded_plugin("feishu")
    assert record is not None and record.status == "registered"
    assert "channel_validation_pending" in record.diagnostics
    assert not list((offline.root / "extractions").iterdir())
    observer.assert_off_loop({"load"})


@pytest.mark.asyncio
@pytest.mark.parametrize("retain", [False, True])
async def test_cancelled_bundle_entry_finishes_before_loop_retention(
    offline,
    monkeypatch,
    retain,
):
    from c5_support import eventually

    bundle(offline)
    observer = bundled_observer(offline, monkeypatch)
    observer.barrier = threading.Event()
    callbacks = []
    decisions = []
    yielded = []
    loop_thread = threading.get_ident()

    class RetentionDecision:
        def __bool__(self):
            decisions.append(
                (threading.get_ident(), asyncio.get_running_loop())
            )
            return retain

    def retain_source(root):
        callbacks.append(
            (root, threading.get_ident(), asyncio.get_running_loop())
        )
        assert root.exists()
        return RetentionDecision()

    async def enter_bundle():
        async with offline.official.async_bundled_channel_source(
            "feishu",
            retain=retain_source,
        ) as source:
            yielded.append(source)

    entering = asyncio.create_task(enter_bundle())
    try:
        await eventually(lambda: observer.entered.is_set() or entering.done())
        if entering.done():
            await entering
        assert observer.entered.is_set()
        entering.cancel()
        await asyncio.sleep(0)
        assert not entering.done(), "bundle entry abandoned its worker"
        assert not callbacks, "retention checked before source entry completed"
    finally:
        observer.barrier.set()
        with pytest.raises(asyncio.CancelledError):
            await entering
    assert not yielded
    assert len(callbacks) == 1
    root, callback_thread, callback_loop = callbacks[0]
    assert callback_thread == loop_thread
    assert callback_loop is asyncio.get_running_loop()
    assert decisions == [(loop_thread, asyncio.get_running_loop())]
    assert root.exists() is retain
    observer.assert_off_loop({"load"})
    if retain:
        import shutil

        await asyncio.to_thread(shutil.rmtree, root)
