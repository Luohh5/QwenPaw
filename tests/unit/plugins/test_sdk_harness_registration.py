# -*- coding: utf-8 -*-
"""Internal ownership bridge tested through the production plugin loader."""

import re

import pytest

# The imported fixture is registered under its declared name, loader.
# pylint: disable-next=unused-import
from .test_sdk_entry_contract import plugin_loader, write_plugin

SOURCE = """
from qwenpaw.plugins.harnesses import HarnessCapabilities, HarnessSpec

class Plugin:
    def register(self, ctx):
        self.factories = 0
        def create(context, settings):
            self.factories += 1
            raise AssertionError("catalog must never construct a backend")
        self.handle = ctx.register_harness(
            HarnessSpec(
                "test-backend", "Test backend", create, HarnessCapabilities()
            )
        )
plugin = Plugin()
"""


def manifest(
    tmp_path,
    name="harness-owner",
    *,
    ids=("test-backend",),
    source=SOURCE,
    capabilities=("harnesses",),
    version="1.1.0",
):
    root = tmp_path / "plugins" / name
    declaration = write_plugin(
        root,
        source,
        sdk={"version": version, "capabilities": list(capabilities)},
        meta={
            "harnesses": [
                {"id": identifier, "label": identifier} for identifier in ids
            ],
        },
    )
    return root, declaration


def assert_failed(record, message):
    """Registration errors persist as failed loader records."""
    assert record.status == "failed" and not record.enabled
    assert re.search(message, "; ".join(record.diagnostics), re.IGNORECASE)


async def test_register_does_not_call_factory(tmp_path, loader):
    root, declaration = manifest(tmp_path)
    record = await loader.load_plugin(declaration, root, {})
    try:
        registry = loader.registry.backend_registry
        for _ in range(2):
            assert any(
                row.id == "test-backend" for row in registry.descriptors()
            )
        assert record.instance.factories == 0
    finally:
        await loader.unload_plugin(declaration.id)
    assert not any(row.id == "test-backend" for row in registry.descriptors())


async def test_duplicate_backend_names_occupant(tmp_path, loader):
    root, declaration = manifest(tmp_path)
    await loader.load_plugin(declaration, root, {})
    other_root, other = manifest(tmp_path, "other-owner")
    try:
        failed = await loader.load_plugin(other, other_root, {})
        assert_failed(failed, "harness-owner")
        assert loader.registry.backend_registry.get("test-backend") is not None
    finally:
        await loader.unload_plugin(other.id)
        await loader.unload_plugin(declaration.id)


@pytest.mark.parametrize(
    "generation,context_identity",
    [(2, "new-generation"), (1, "new-context")],
)
async def test_stale_handle_cannot_remove_replacement(
    generation,
    context_identity,
):
    from qwenpaw.plugins.harnesses import HarnessCapabilities, HarnessSpec
    from qwenpaw.plugins.resources import ResourceOwner
    from qwenpaw.runtime.backends.registry import BackendRegistry

    registry = BackendRegistry()
    old_owner = ResourceOwner("harness-owner", 1, instance_id="old-context")
    new_owner = ResourceOwner(
        "harness-owner",
        generation,
        instance_id=context_identity,
    )
    spec = HarnessSpec(
        "test-backend",
        "Test backend",
        lambda context, settings: None,
        HarnessCapabilities(),
    )
    old_handle = registry.register(old_owner, spec)
    registry.unregister_owner(old_owner)
    assert not old_handle.closed
    new_handle = registry.register(new_owner, spec)
    replacement = registry.get("test-backend")
    try:
        assert replacement is not None
        await old_handle.close()
        assert registry.get("test-backend") is replacement
        registry.unregister_owner(old_owner)
        assert registry.get("test-backend") is replacement
    finally:
        await new_handle.close()


@pytest.mark.parametrize(
    "ids",
    [("test-backend",), ("different-backend",), ()],
)
async def test_descriptor_matches_manifest_ids(tmp_path, loader, ids):
    root, declaration = manifest(tmp_path, ids=ids)
    if ids != ("test-backend",):
        failed = await loader.load_plugin(declaration, root, {})
        assert_failed(failed, "manifest|harness")
        assert loader.registry.backend_registry.get("test-backend") is None
        return
    await loader.load_plugin(declaration, root, {})
    try:
        descriptor = next(
            row
            for row in loader.registry.backend_registry.descriptors()
            if row.id == "test-backend"
        )
        assert descriptor.plugin_id == declaration.id
    finally:
        await loader.unload_plugin(declaration.id)


@pytest.mark.parametrize(
    "identifier",
    ["qwenpaw", "Bad", "1bad", "bad/id", ""],
)
async def test_harness_rejects_reserved_or_invalid_id(
    tmp_path,
    loader,
    identifier,
):
    root, declaration = manifest(
        tmp_path,
        ids=(identifier,),
        source=SOURCE.replace('"test-backend"', repr(identifier)),
    )
    failed = await loader.load_plugin(declaration, root, {})
    assert_failed(failed, "ID|id|reserved")
    assert loader.registry.backend_registry.get(identifier) is None


async def test_harness_requires_declared_capability(tmp_path, loader):
    root, declaration = manifest(tmp_path, capabilities=())
    failed = await loader.load_plugin(declaration, root, {})
    assert_failed(failed, "harnesses")
    assert loader.registry.backend_registry.get("test-backend") is None


async def test_sdk_10_plugin_still_loads(tmp_path, loader):
    root, declaration = manifest(
        tmp_path,
        version="1.0",
        ids=(),
        capabilities=(),
        source=(
            "class Plugin:\n    def register(self, ctx):\n"
            "        self.plugin_id = ctx.plugin_id\nplugin = Plugin()\n"
        ),
    )
    record = await loader.load_plugin(declaration, root, {})
    try:
        assert (
            record.status == "active"
            and record.instance.plugin_id == declaration.id
        )
    finally:
        await loader.unload_plugin(declaration.id)


async def test_sdk_11_old_host_refusal_is_real_preflight(
    tmp_path,
    loader,
    monkeypatch,
):
    from qwenpaw.plugins import SDKCompatibilityError, sdk_version

    marker = tmp_path / "should-not-import"
    source = (
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n" + SOURCE
    )
    root, declaration = manifest(tmp_path, source=source)
    # Configure an old host's supported inputs; execute its actual validator.
    monkeypatch.setattr(sdk_version, "SDK_VERSION", "1.0.0")
    monkeypatch.setattr(
        sdk_version,
        "SDK_CAPABILITIES",
        sdk_version.SDK_CAPABILITIES - {"harnesses"},
    )
    with pytest.raises(SDKCompatibilityError, match="version"):
        await loader.load_plugin(declaration, root, {})
    assert not marker.exists()
    assert loader.get_loaded_plugin(declaration.id) is None


async def test_manifest_declaration_alone_is_not_callable(tmp_path, loader):
    root, declaration = manifest(
        tmp_path,
        source=(
            "class Plugin:\n    def register(self, ctx):\n"
            "        pass\nplugin = Plugin()\n"
        ),
    )
    failed = await loader.load_plugin(declaration, root, {})
    assert_failed(failed, "manifest|harness")
    assert loader.registry.backend_registry.get("test-backend") is None


def test_harness_declaration_import_does_not_load_runtime_or_vendors():
    import json
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json, sys; import qwenpaw.plugins.harnesses; "
            "import qwenpaw.runtime.backends.registry; "
            "print(json.dumps(sorted(sys.modules)))",
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    modules = json.loads(result.stdout)
    for prefix in (
        "qwenpaw.runtime.runtime",
        "qwenpaw.harnesses",
        "qwenpaw.app",
        "codex",
        "qoder",
    ):
        assert not any(
            name == prefix or name.startswith(prefix + ".") for name in modules
        ), prefix


async def test_harness_capability_requires_sdk_11_before_import(
    tmp_path,
    loader,
):
    from qwenpaw.plugins import SDKCompatibilityError

    marker = tmp_path / "old-sdk-module-executed"
    source = (
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n" + SOURCE
    )
    root, declaration = manifest(tmp_path, version="1.0", source=source)
    with pytest.raises(SDKCompatibilityError, match="1\\.1|harnesses"):
        await loader.load_plugin(declaration, root, {})
    assert not marker.exists()
