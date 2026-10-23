# -*- coding: utf-8 -*-
"""Reload admits dependencies before disturbing an existing SDK service."""

# Assert production lifecycle identity at the SDK service boundary.
# pylint: disable=protected-access
import asyncio
import importlib
import sys

import pytest

from qwenpaw.plugins.channels import ChannelMessage
from qwenpaw.plugins.messages import ContentBlock
from qwenpaw.plugins.testing import PluginTestHost

from .sdk_author_fixture import make_plugin


SOURCE = """
from qwenpaw.plugins.channels import ChannelSpec
from qwenpaw.plugins.commands import CommandSpec, CommandResult
from qwenpaw.plugins.messages import Message, ContentBlock


class Plugin:
    def register(self, plugin):
        class Transport:
            async def start(self, call):
                pass

            async def stop(self, call):
                return True

            async def send(self, call, message):
                rows = plugin.storage.read_json("sent.json", [])
                plugin.storage.write_json(
                    "sent.json",
                    rows + ["".join(block.text for block in message.content)],
                )

        async def reply(call, args):
            return CommandResult(
                "reply", Message("assistant", (ContentBlock(text=args),)),
            )

        plugin.register_channel(
            ChannelSpec("admission-channel", "Admission", Transport),
        )
        plugin.register_command(CommandSpec("admissionprobe", reply))


plugin = Plugin()
"""


def distribution(root, name, version, requires=()):
    """Real import and RECORD ownership evidence, with no package mocks."""
    module = name.replace("-", "_")
    root.mkdir(parents=True, exist_ok=True)
    info = root / f"{module}-{version}.dist-info"
    info.mkdir()
    (info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
        + "".join(f"Requires-Dist: {value}\n" for value in requires),
        encoding="utf-8",
    )
    (info / "top_level.txt").write_text(module + "\n", encoding="utf-8")
    (root / f"{module}.py").write_text(
        f"VERSION = {version!r}\n",
        encoding="utf-8",
    )
    (info / "RECORD").write_text(
        f"{module}.py,,\n{info.name}/METADATA,,\n"
        f"{info.name}/top_level.txt,,\n{info.name}/RECORD,,\n",
        encoding="utf-8",
    )
    return info


async def delivered(host, context, text):
    before = context.storage.read_json("sent.json", [])
    await host.receive_channel(
        "admission-channel",
        ChannelMessage(
            "sender",
            (ContentBlock(text=f"/admissionprobe {text}"),),
        ),
    )
    for _ in range(200):
        rows = context.storage.read_json("sent.json", [])
        if len(rows) > len(before):
            assert rows == before + [text]
            return
        await asyncio.sleep(0.01)
    pytest.fail(f"Existing channel did not deliver {text!r}")


@pytest.mark.parametrize("prior_reload", [False, True])
async def test_host_dependency_refusal_keeps_exact_live_sdk_channel(
    tmp_path,
    monkeypatch,
    prior_reload,
):
    site = tmp_path / "host-site"
    distribution(site, "reload-admission-peer", "1.0")
    distribution(
        site,
        "reload-admission-owner",
        "1.0",
        requires=("reload-admission-peer>=2",),
    )
    monkeypatch.syspath_prepend(str(site))
    if not prior_reload:
        module = importlib.import_module("reload_admission_owner")
        assert module.VERSION == "1.0"
    try:
        async with PluginTestHost(tmp_path / "host") as host:
            root = make_plugin(
                tmp_path,
                SOURCE,
                ["channels", "commands", "storage"],
            )
            assert not (root / "requirements.txt").exists()
            loader = host._loader
            usage = loader.registry.dependency_usage
            tokens = declaration_tokens(monkeypatch, usage)
            context = await host.load(root)
            if prior_reload:
                primed = await host.reload("extension-contract")
                assert primed.ok, primed.errors
                context = host.context("extension-contract")
                module = importlib.import_module("reload_admission_owner")
                assert module.VERSION == "1.0"
            old_tokens = tuple(t for t in tokens if usage.has_declaration(t))
            tokens.clear()
            record = loader.get_loaded_plugin("extension-contract")
            instance = loader.lifecycle.get_instance("extension-contract")
            definition, generation = record.instance, instance.generation
            ledger = tuple(instance._runtime)
            await host.start_channel("admission-channel")
            await host.stop_channel("admission-channel")
            await host.start_channel("admission-channel")
            workspace = await host._workspace("test")
            manager = workspace.channel_manager
            channel = manager._find_channel("admission-channel")

            report = await host.reload("extension-contract")

            assert not report.ok, report.errors
            assert report.unchanged, report.errors
            assert any("reload-admission-peer" in e for e in report.errors)
            assert loader.get_loaded_plugin("extension-contract") is record
            assert (
                loader.lifecycle.get_instance("extension-contract") is instance
            )
            assert record.instance is definition
            assert instance.generation == generation
            assert report.generation == generation
            assert tuple(instance._runtime) == ledger
            assert old_tokens and all(
                usage.has_declaration(t) for t in old_tokens
            )
            assert tokens and not any(usage.has_declaration(t) for t in tokens)
            assert host.context("extension-contract") is context
            assert manager._find_channel("admission-channel") is channel
            assert record.status == "active" and instance.activated
            await delivered(host, context, "after")
            await host.stop_channel("admission-channel")
    finally:
        sys.modules.pop("reload_admission_owner", None)


def declaration_tokens(monkeypatch, usage):
    """Observe opaque registry tokens without replacing admission decisions."""
    tokens = []
    original = usage.declare

    def declare(*args, **kwargs):
        token = original(*args, **kwargs)
        tokens.append(token)
        return token

    monkeypatch.setattr(usage, "declare", declare)
    return tokens


async def test_successful_sdk_reload_transfers_admission_until_disposal(
    tmp_path,
    monkeypatch,
):
    async with PluginTestHost(tmp_path / "host") as host:
        context = await host.load(
            make_plugin(tmp_path, SOURCE, ["channels", "commands", "storage"]),
        )
        loader = host._loader
        old = loader.lifecycle.get_instance("extension-contract")
        usage = loader.registry.dependency_usage
        tokens = declaration_tokens(monkeypatch, usage)
        report = await host.reload("extension-contract")
        assert report.ok and not report.needs_restart, report.errors
        current = loader.lifecycle.get_instance("extension-contract")
        assert current is not old
        assert current.generation == old.generation + 1
        assert host.context("extension-contract") is not context
        assert tokens and sum(usage.has_declaration(t) for t in tokens) == 1
        await host.start_channel("admission-channel")
        await delivered(host, host.context("extension-contract"), "new")
        await host.stop_channel("admission-channel")
        # Delivery has a workspace-owned completion tail; close() settles it.
        await host.close()
        assert not any(usage.has_declaration(t) for t in tokens)
        assert not loader.registry.runtime_owners.leases("extension-contract")


async def test_sdk_activation_failure_restores_saved_admission_and_service(
    tmp_path,
    monkeypatch,
):
    source = SOURCE.replace(
        "    def register(self, plugin):\n",
        "    def register(self, plugin):\n"
        "        if plugin.config.get('fail'):\n"
        "            raise RuntimeError('candidate activation failed')\n",
    )
    async with PluginTestHost(tmp_path / "host") as host:
        await host.load(
            make_plugin(
                tmp_path,
                source,
                ["channels", "commands", "storage", "config"],
            ),
            config={"fail": False},
        )
        loader = host._loader
        old = loader.lifecycle.get_instance("extension-contract")
        definition = loader.get_loaded_plugin("extension-contract").instance
        usage = loader.registry.dependency_usage
        old_declarations = tuple(
            row for row in usage.snapshot().uses if row.origin == "declaration"
        )
        tokens = declaration_tokens(monkeypatch, usage)
        report = await host.reload("extension-contract", config={"fail": True})
        assert not report.ok and not report.needs_restart, report.errors
        assert any("candidate activation failed" in e for e in report.errors)
        assert any("previous service was restored" in e for e in report.errors)
        record = loader.get_loaded_plugin("extension-contract")
        current = loader.lifecycle.get_instance("extension-contract")
        assert record.status == "active" and current.activated
        assert record.instance is definition
        assert current.generation == old.generation
        assert current.config["fail"] is False
        restored = tuple(
            row for row in usage.snapshot().uses if row.origin == "declaration"
        )
        assert len(restored) == len(old_declarations) == 1
        assert (
            restored[0].source_revision == old_declarations[0].source_revision
        )
        assert restored[0].requirements == old_declarations[0].requirements
        await host.start_channel("admission-channel")
        await delivered(host, host.context("extension-contract"), "restored")
        await host.stop_channel("admission-channel")
        await host.close()
        assert not any(usage.has_declaration(t) for t in tokens)


async def test_cancelled_preflight_settles_real_scanner_before_releasing_gate(
    tmp_path,
    monkeypatch,
):
    import threading
    from pathlib import Path

    site = tmp_path / "host-site"
    info = distribution(site, "reload-cancellation-owner", "1.0")
    monkeypatch.syspath_prepend(str(site))
    importlib.import_module("reload_cancellation_owner")
    release = threading.Event()
    reloading = competing = None
    try:
        async with PluginTestHost(tmp_path / "host") as host:
            context = await host.load(
                make_plugin(
                    tmp_path,
                    SOURCE,
                    ["channels", "commands", "storage"],
                ),
            )
            loader = host._loader
            old = loader.lifecycle.get_instance("extension-contract")
            record = loader.get_loaded_plugin("extension-contract")
            ledger = tuple(old._runtime)
            usage = loader.registry.dependency_usage
            tokens = declaration_tokens(monkeypatch, usage)
            await host.start_channel("admission-channel")
            manager = (await host._workspace("test")).channel_manager
            channel = manager._find_channel("admission-channel")
            entered, acquired = asyncio.Event(), asyncio.Event()
            loop = asyncio.get_running_loop()
            original_open = Path.open

            def slow_record(path, *args, **kwargs):
                if path == info / "RECORD":
                    loop.call_soon_threadsafe(entered.set)
                    assert release.wait(
                        5,
                    ), "preflight scanner was not released"
                return original_open(path, *args, **kwargs)

            async def competing_lifecycle():
                async with loader.plugin_lifecycle("extension-contract"):
                    acquired.set()

            monkeypatch.setattr(Path, "open", slow_record)
            reloading = asyncio.create_task(host.reload("extension-contract"))
            try:
                await asyncio.wait_for(entered.wait(), 5)
                assert loader.get_loaded_plugin("extension-contract") is record
                assert host.context("extension-contract") is context
                assert manager._find_channel("admission-channel") is channel
                assert tuple(old._runtime) == ledger
                competing = asyncio.create_task(competing_lifecycle())
                for _ in range(3):
                    reloading.cancel()
                    await asyncio.sleep(0)
                    assert not reloading.done()
                    assert not acquired.is_set()
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await reloading
                await asyncio.wait_for(competing, 5)
                assert tokens and not any(
                    usage.has_declaration(t) for t in tokens
                )
                assert (
                    loader.lifecycle.get_instance("extension-contract") is old
                )
                assert tuple(old._runtime) == ledger
                assert host.context("extension-contract") is context
                await delivered(host, context, "after cancellation")
                await host.stop_channel("admission-channel")
            finally:
                release.set()
                await asyncio.gather(
                    *(
                        task
                        for task in (reloading, competing)
                        if task is not None
                    ),
                    return_exceptions=True,
                )
    finally:
        release.set()
        sys.modules.pop("reload_cancellation_owner", None)


@pytest.mark.parametrize("changed", ["environment", "requirements"])
async def test_reload_rejects_admission_evidence_changed_before_switch(
    tmp_path,
    monkeypatch,
    changed,
):
    site = tmp_path / "host-site"
    info = distribution(site, "reload-evidence-peer", "1.0")
    monkeypatch.syspath_prepend(str(site))
    async with PluginTestHost(tmp_path / "host") as host:
        root = make_plugin(
            tmp_path,
            SOURCE,
            ["channels", "commands", "storage"],
        )
        await host.load(root)
        activated = tmp_path / "candidate-activated"
        candidate_parent = tmp_path / "candidate"
        candidate_parent.mkdir()
        candidate = make_plugin(
            candidate_parent,
            SOURCE.replace(
                "    def register(self, plugin):\n",
                "    def register(self, plugin):\n"
                "        from pathlib import Path\n"
                f"        Path({str(activated)!r}).touch()\n",
            ),
            ["channels", "commands", "storage"],
        )
        requirements = candidate / "requirements.txt"
        requirements.write_text(
            "reload-evidence-peer==1.0\n",
            encoding="utf-8",
        )
        usage = host._loader.registry.dependency_usage
        tokens = declaration_tokens(monkeypatch, usage)

        def change_evidence(_plugin_id):
            if changed == "environment":
                metadata = info / "METADATA"
                metadata.write_text(
                    metadata.read_text().replace(
                        "Version: 1.0",
                        "Version: 2.0",
                    ),
                    encoding="utf-8",
                )
            else:
                requirements.write_text(
                    "reload-evidence-peer>=2\n",
                    encoding="utf-8",
                )

        loader = host._loader
        async with loader.plugin_lifecycle("extension-contract"):
            report = await loader.reload_plugin_unlocked(
                "extension-contract",
                new_source=candidate,
                before_unload=change_evidence,
            )
        assert not report.ok, report.errors
        assert not activated.exists(), "incompatible candidate ran register()"
        assert any(
            "changed" in e.lower() or "DEPENDENCY" in e for e in report.errors
        ), report.errors
        assert sum(usage.has_declaration(t) for t in tokens) <= 1
        await host.close()
        assert not any(usage.has_declaration(t) for t in tokens)


@pytest.mark.parametrize("fail_activation", [False, True])
async def test_compatible_environment_addition_refreshes_reload_and_restore(
    tmp_path,
    monkeypatch,
    fail_activation,
):
    site = tmp_path / "host-site"
    distribution(site, "reload-compatible-peer", "1.0")
    monkeypatch.syspath_prepend(str(site))
    source = SOURCE.replace(
        "    def register(self, plugin):\n",
        "    def register(self, plugin):\n"
        "        if plugin.config.get('fail'):\n"
        "            raise RuntimeError('compatible candidate activation failed')\n",
    )
    async with PluginTestHost(tmp_path / "host") as host:
        root = make_plugin(
            tmp_path,
            source,
            ["channels", "commands", "storage", "config"],
        )
        (root / "requirements.txt").write_text(
            "reload-compatible-peer==1.0\n",
            encoding="utf-8",
        )
        await host.load(root, config={"fail": False})
        loader = host._loader
        old = loader.lifecycle.get_instance("extension-contract")
        definition = loader.get_loaded_plugin("extension-contract").instance
        usage = loader.registry.dependency_usage
        tokens = declaration_tokens(monkeypatch, usage)

        def add_compatible_distribution(_plugin_id):
            distribution(site, "reload-compatible-addition", "3.0")

        async with loader.plugin_lifecycle("extension-contract"):
            report = await loader.reload_plugin_unlocked(
                "extension-contract",
                config={"fail": fail_activation},
                before_unload=add_compatible_distribution,
            )
        assert report.ok is not fail_activation, report.errors
        assert not report.needs_restart, report.errors
        current = loader.lifecycle.get_instance("extension-contract")
        record = loader.get_loaded_plugin("extension-contract")
        assert record.status == "active" and current.activated
        if fail_activation:
            assert any(
                "previous service was restored" in e for e in report.errors
            )
            assert record.instance is definition
            assert current.generation == old.generation
        else:
            assert current is not old
            assert current.generation == old.generation + 1
        assert sum(usage.has_declaration(t) for t in tokens) == 1
        await host.start_channel("admission-channel")
        await delivered(host, host.context("extension-contract"), "compatible")
        await host.stop_channel("admission-channel")
        await host.close()
        assert not any(usage.has_declaration(t) for t in tokens)


async def test_host_import_after_unload_cannot_reuse_preflight_permission(
    tmp_path,
    monkeypatch,
):
    site = tmp_path / "host-site"
    distribution(site, "reload-late-peer", "1.0")
    distribution(
        site,
        "reload-late-owner",
        "1.0",
        requires=("reload-late-peer>=2",),
    )
    monkeypatch.syspath_prepend(str(site))
    try:
        async with PluginTestHost(tmp_path / "host") as host:
            root = make_plugin(
                tmp_path,
                SOURCE,
                ["channels", "commands", "storage"],
            )
            await host.load(root)
            activated = tmp_path / "late-candidate-activated"
            candidate_parent = tmp_path / "candidate"
            candidate_parent.mkdir()
            candidate = make_plugin(
                candidate_parent,
                SOURCE.replace(
                    "    def register(self, plugin):\n",
                    "    def register(self, plugin):\n"
                    "        from pathlib import Path\n"
                    f"        Path({str(activated)!r}).touch()\n",
                ),
                ["channels", "commands", "storage"],
            )

            def import_conflicting_owner(_plugin_id):
                module = importlib.import_module("reload_late_owner")
                assert module.VERSION == "1.0"

            loader = host._loader
            usage = loader.registry.dependency_usage
            tokens = declaration_tokens(monkeypatch, usage)
            async with loader.plugin_lifecycle("extension-contract"):
                report = await loader.reload_plugin_unlocked(
                    "extension-contract",
                    new_source=candidate,
                    after_unload=import_conflicting_owner,
                )
            assert not report.ok, report.errors
            assert (
                not activated.exists()
            ), "late incompatible host import escaped admission"
            assert any("reload-late-peer" in e for e in report.errors)
            await host.close()
            assert not any(usage.has_declaration(t) for t in tokens)
    finally:
        sys.modules.pop("reload_late_owner", None)


@pytest.mark.parametrize("observer_kind", ["sync", "async"])
async def test_preflight_observer_error_withdraws_only_temporary_admission(
    tmp_path,
    monkeypatch,
    observer_kind,
):
    async with PluginTestHost(tmp_path / "host") as host:
        root = make_plugin(
            tmp_path,
            SOURCE,
            ["channels", "commands", "storage"],
        )
        context = await host.load(root)
        loader = host._loader
        record = loader.get_loaded_plugin("extension-contract")
        old = loader.lifecycle.get_instance("extension-contract")
        ledger = tuple(old._runtime)
        usage = loader.registry.dependency_usage
        declarations = tuple(
            row for row in usage.snapshot().uses if row.origin == "declaration"
        )
        tokens = declaration_tokens(monkeypatch, usage)
        await host.start_channel("admission-channel")
        manager = (await host._workspace("test")).channel_manager
        channel = manager._find_channel("admission-channel")
        backend = loader.backend_resolver.resolve(
            root,
            record.manifest,
        ).backend

        def fail_observation(*_args):
            raise RuntimeError("preflight observer failed")

        async def fail_async_observation(*_args):
            fail_observation()

        with monkeypatch.context() as failure:
            failure.setattr(
                backend,
                "observe_host_dependencies",
                fail_observation
                if observer_kind == "sync"
                else fail_async_observation,
            )
            report = await host.reload("extension-contract")
        assert not report.ok and report.unchanged, report.errors
        assert any("preflight observer failed" in e for e in report.errors)
        assert loader.get_loaded_plugin("extension-contract") is record
        assert loader.lifecycle.get_instance("extension-contract") is old
        assert host.context("extension-contract") is context
        assert tuple(old._runtime) == ledger
        assert manager._find_channel("admission-channel") is channel
        assert tokens and not any(usage.has_declaration(t) for t in tokens)
        assert (
            tuple(
                row
                for row in usage.snapshot().uses
                if row.origin == "declaration"
            )
            == declarations
        )
        await delivered(host, context, "after observer error")
        await host.stop_channel("admission-channel")


async def test_in_place_requirements_drift_restores_saved_old_dependency_binding(
    tmp_path,
    monkeypatch,
):
    site = tmp_path / "host-site"
    distribution(site, "reload-in-place-peer", "1.0")
    monkeypatch.syspath_prepend(str(site))
    async with PluginTestHost(tmp_path / "host") as host:
        root = make_plugin(
            tmp_path,
            SOURCE,
            ["channels", "commands", "storage"],
        )
        requirements = root / "requirements.txt"
        requirements.write_text(
            "reload-in-place-peer==1.0\n",
            encoding="utf-8",
        )
        await host.load(root)
        loader = host._loader
        old = loader.lifecycle.get_instance("extension-contract")
        definition = loader.get_loaded_plugin("extension-contract").instance
        activated = tmp_path / "in-place-candidate-activated"
        usage = loader.registry.dependency_usage
        tokens = declaration_tokens(monkeypatch, usage)

        def replace_candidate(_plugin_id):
            requirements.write_text(
                "reload-in-place-peer>=2\n",
                encoding="utf-8",
            )
            (root / "main.py").write_text(
                SOURCE.replace(
                    "    def register(self, plugin):\n",
                    "    def register(self, plugin):\n"
                    "        from pathlib import Path\n"
                    f"        Path({str(activated)!r}).touch()\n",
                ),
                encoding="utf-8",
            )

        async with loader.plugin_lifecycle("extension-contract"):
            report = await loader.reload_plugin_unlocked(
                "extension-contract",
                before_unload=replace_candidate,
            )
        assert not report.ok, report.errors
        assert (
            not activated.exists()
        ), "incompatible in-place candidate executed"
        assert not report.needs_restart, report.errors
        assert any("previous service was restored" in e for e in report.errors)
        record = loader.get_loaded_plugin("extension-contract")
        restored = loader.lifecycle.get_instance("extension-contract")
        assert record.status == "active" and restored.activated
        assert record.instance is definition
        assert restored.generation == old.generation
        declarations = tuple(
            row for row in usage.snapshot().uses if row.origin == "declaration"
        )
        assert len(declarations) == 1
        assert declarations[0].requirements == ("reload-in-place-peer==1.0",)
        await host.start_channel("admission-channel")
        await delivered(
            host,
            host.context("extension-contract"),
            "old binding",
        )
        await host.stop_channel("admission-channel")
        await host.close()
        assert not any(usage.has_declaration(t) for t in tokens)
