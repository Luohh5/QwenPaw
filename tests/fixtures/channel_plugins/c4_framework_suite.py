# -*- coding: utf-8 -*-
# pylint: disable=protected-access,redefined-outer-name
"""C4 admission and observations through the actual Loader/Registry owner."""

from dataclasses import replace
import json

import pytest

from workspace_runtime_suite import KEY, framework as runtime_framework, rows

framework = runtime_framework


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing", "dirty", "driver"])
async def test_failed_admission_never_projects_or_creates_consumer(
    framework,
    tmp_path,
    failure,
):
    from qwenpaw.plugins.execution.dependencies import repair_marker

    await framework.workspace("not-admitted")
    if failure == "missing":
        requirements = framework.source / "requirements.txt"
        requirements.write_text("c4-absent-sdk-admission>=1\n")
    elif failure == "dirty":
        site = tmp_path / "dirty-site"
        site.mkdir()
        repair_marker(site).write_text("incomplete previous install\n")
        framework.registry.channel_runtime = replace(
            framework.registry.channel_runtime,
            dependency_paths=(site,),
        )
    else:
        path = framework.source / KEY / "broken.py"
        path.write_text(
            "class Driver:\n"
            "    def bind(self, context): pass\n"
            "    def start(self, config): pass\n"
            "    async def stop(self): pass\n",
        )
        path = framework.source / "channel.json"
        descriptor = json.loads(path.read_text())
        descriptor["entrypoint"]["module"] = KEY + ".broken"
        path.write_text(json.dumps(descriptor))
    with pytest.raises(RuntimeError):
        await framework.activate()
    assert not framework.registry._channel_owners
    assert not framework.loader._channel_preparations
    assert not any(
        rows(path, "connected") for path in framework.network.iterdir()
    )
    assert not any(getattr(ws, "requests", []) for ws in framework.workspaces)


@pytest.mark.asyncio
async def test_current_dependency_observation_survives_stale_owner_release(
    framework,
):
    ws = await framework.workspace("dependency-owner")
    await framework.activate()
    old = await ws.channel_manager.get_channel(KEY)
    spec = old._launch_spec
    uses = framework.registry.channel_dependency_uses()
    assert any(not use["active"] and use["plugin_id"] == KEY for use in uses)
    observed = next(use for use in uses if use["active"])
    assert observed["instance_id"] == spec.instance_id
    assert observed["generation"] == spec.generation
    assert observed["snapshot"]["distributions"]
    assert (await ws.channel_manager.stop_one(KEY)).exited
    assert not any(
        use["active"] for use in framework.registry.channel_dependency_uses()
    )
    await ws.channel_manager.start_one(KEY, ws._config)
    fresh = await ws.channel_manager.get_channel(KEY)
    assert fresh._launch_spec.generation > spec.generation
    framework.registry.release_channel_owner(spec.instance_id, old)
    active = [
        use
        for use in framework.registry.channel_dependency_uses()
        if use["active"]
    ]
    assert (
        len(active) == 1
        and active[0]["generation"] == fresh._launch_spec.generation
    )
    assert (
        active[0]["snapshot"]
        == fresh.channel_process.dependency_snapshot.to_mapping()
    )


@pytest.mark.asyncio
@pytest.mark.skipif(
    KEY != "dingtalk",
    reason="controlled async socket-close fixture",
)
async def test_failed_actual_stop_retains_dependency_observation(
    framework,
    monkeypatch,
):
    ws = await framework.workspace("failed-dependency-owner")
    await framework.activate()
    channel = await ws.channel_manager.get_channel(KEY)
    work = framework.mailbox(channel)
    (work / "send-mode").write_text("close-block")
    tree = channel.channel_process._tree
    signal = tree.signal
    monkeypatch.setattr(tree, "signal", lambda **_kwargs: None)
    try:
        receipt = await ws.channel_manager.stop_one(KEY)
        assert not receipt.exited
        active = [
            use
            for use in framework.registry.channel_dependency_uses()
            if use["active"]
        ]
        assert len(active) == 1
        assert active[0]["generation"] == channel._launch_spec.generation
        assert active[0]["snapshot"]
    finally:
        monkeypatch.setattr(tree, "signal", signal)
        (work / "close-release").touch()
        assert (await ws.channel_manager.stop_one(KEY)).exited
