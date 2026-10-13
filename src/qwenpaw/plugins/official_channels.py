# -*- coding: utf-8 -*-
"""Offline official sources through the existing plugin lifecycle."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
from importlib import resources
import io
import json
import shutil
import sys
from pathlib import Path
import tempfile
import zipfile

from ..app.channels.catalog import OFFICIAL_CHANNEL_OWNERS
from .channel_bootstrap import (
    resolve_default_channel_runtime,
)


__all__ = [
    "resolve_default_channel_runtime",
    "provision_bundled_channels",
    "bundled_channel_source",
]


def _resource_root():
    if getattr(sys, "frozen", False):
        from .channel_bootstrap import runtime_manifest

        return runtime_manifest()[1] / "qwenpaw/official_channels"
    return resources.files("qwenpaw").joinpath("official_channels")


def _digest(content):
    return hashlib.sha256(content).hexdigest()


def _bundled_index(root):
    """Validate the small, closed C7 resource index before selecting a ZIP."""
    from ..__version__ import __version__

    with root.joinpath("index.json").open("rb") as stream:
        content = stream.read(64 * 1024 + 1)
    if len(content) > 64 * 1024:
        raise ValueError("Official index exceeds limits")
    index = json.loads(content)
    # Closed resource identity requires all these independent checks.
    # pylint: disable=too-many-boolean-expressions
    if (
        not isinstance(index, dict)
        or set(index) != {"schema_version", "host_version", "plugins"}
        or index["schema_version"] != 1
        or index["host_version"] != __version__
        or not isinstance(index["plugins"], list)
        or any(not isinstance(row, dict) for row in index["plugins"])
        or [row.get("plugin_id") for row in index["plugins"]]
        != sorted(OFFICIAL_CHANNEL_OWNERS)
    ):
        raise ValueError("Official index identity mismatch")
    for row in index["plugins"]:
        if set(row) != {
            "plugin_id",
            "version",
            "filename",
            "sha256",
            "descriptor_sha256",
            "requirements_sha256",
            "qwenpaw_version",
        }:
            raise ValueError("Official index fields mismatch")
        if (
            not isinstance(row["filename"], str)
            or Path(row["filename"]).name != row["filename"]
            or any(char in row["filename"] for char in ("\\", ":"))
            or not row["filename"].endswith(".zip")
        ):
            raise ValueError("Official archive filename invalid")
        for key in ("sha256", "descriptor_sha256", "requirements_sha256"):
            if (
                not isinstance(row[key], str)
                or len(row[key]) != 64
                or any(char not in "0123456789abcdef" for char in row[key])
            ):
                raise ValueError("Official archive digest invalid")
    return index


@contextmanager
def bundled_channel_source(plugin_id, *, retain=None):
    """Verify installed resource bytes and yield a private extracted source."""
    from .architecture import PluginManifest
    from .archive import safe_extract_zip
    from .channel_adapter import describe_channel

    if plugin_id not in OFFICIAL_CHANNEL_OWNERS:
        raise ValueError("Unknown official channel")
    root = _resource_root()
    index = _bundled_index(root)
    row = {row["plugin_id"]: row for row in index["plugins"]}[plugin_id]
    with root.joinpath(row["filename"]).open("rb") as stream:
        payload = stream.read(64 * 1024 * 1024 + 1)
    if len(payload) > 64 * 1024 * 1024 or _digest(payload) != row["sha256"]:
        raise ValueError("Official archive digest mismatch")
    target = Path(tempfile.mkdtemp(prefix="qwenpaw-official-"))
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            if any(
                not name.startswith(plugin_id + "/")
                for name in archive.namelist()
            ):
                raise ValueError("Official archive root mismatch")
            safe_extract_zip(archive, target)
        source = target / plugin_id
        raw = json.loads((source / "plugin.json").read_text(encoding="utf-8"))
        manifest = PluginManifest.from_dict(raw)
        if (
            manifest.id != plugin_id
            or manifest.version != row["version"]
            or raw.get("qwenpaw_version") != row["qwenpaw_version"]
            or raw.get("publish") is not False
        ):
            raise ValueError("Official manifest identity mismatch")
        from .._version_compat import check_plugin_version_compat

        compatible, reason = check_plugin_version_compat(manifest)
        if not compatible:
            raise ValueError(reason)
        description = describe_channel(source, manifest)
        if (
            _digest(description.descriptor_path.read_bytes())
            != row["descriptor_sha256"]
            or _digest(description.requirements_path.read_bytes())
            != row["requirements_sha256"]
        ):
            raise ValueError("Official source digest mismatch")
        yield source
    finally:
        if retain is None or not retain(target):
            shutil.rmtree(target)


def initialize_channel_policy():
    """Capture legacy intent once from raw persisted files, never defaults."""
    from ..config.utils import get_config_path, mutate_config
    from .channel_bootstrap import default_channel_preset

    def apply(config):
        state = config.channel_plugin_state
        if state.version >= 1:
            return
        path = get_config_path()
        raw = (
            json.loads(path.read_text(encoding="utf-8"))
            if path.is_file()
            else {}
        )
        saved = [raw]
        for profile in raw.get("agents", {}).get("profiles", {}).values():
            agent_path = (
                Path(profile["workspace_dir"]).expanduser() / "agent.json"
            )
            if agent_path.is_file():
                saved.append(
                    json.loads(agent_path.read_text(encoding="utf-8")),
                )
        state.legacy_pending = sorted(
            {
                key
                for data in saved
                for key in OFFICIAL_CHANNEL_OWNERS
                if data.get("channels", {}).get(key, {}).get("enabled") is True
                and key not in state.uninstalled
            },
        )
        state.preset = default_channel_preset()
        state.version = 1

    mutate_config(apply)


async def provision_bundled_channels(loader):
    """Seed absent sources, retaining finite migration retry intent."""
    from ..config.utils import load_config, mutate_config
    from ..utils.io_utils import run_sync_io

    await run_sync_io(initialize_channel_policy)
    config = await run_sync_io(load_config)
    state = config.channel_plugin_state
    requested = set(state.legacy_pending)
    if state.preset == "channels-full":
        requested.update(OFFICIAL_CHANNEL_OWNERS)
    errors = {}
    for plugin_id in sorted(requested - set(state.uninstalled)):
        try:
            if loader.find_installed_plugin_dir(plugin_id) is None:
                with bundled_channel_source(plugin_id) as source:
                    await loader.seed_bundled_channel(
                        source,
                        config.plugins.get(plugin_id),
                    )

            def completed(root, key=plugin_id):
                root.channel_plugin_state.legacy_pending = [
                    item
                    for item in root.channel_plugin_state.legacy_pending
                    if item != key
                ]

            await run_sync_io(mutate_config, completed)
        except Exception as exc:  # A resource failure must not stop Console.
            errors[plugin_id] = str(exc)
    return errors
