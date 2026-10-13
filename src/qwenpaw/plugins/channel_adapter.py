# -*- coding: utf-8 -*-
"""Host-owned static Channel definitions; no executable plugin discovery."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ..channel_protocol.artifacts import code_root_digest
from ..channel_protocol.requirements import canonicalize_requirements
from ..channel_protocol.shared import (
    SharedChannelDescriptor,
    _read_requirements,
)
from .architecture import PluginManifest, PluginType

CHANNEL_RUNTIME_NOT_INTEGRATED = "channel_runtime_not_integrated"


class ChannelRuntimeNotIntegratedError(RuntimeError):
    """A descriptor operation was refused before transaction work began."""


def require_channel_runtime_integrated(manifest: PluginManifest) -> None:
    """Refuse mutation until Channel activation/transactions are wired."""
    if manifest.entry.descriptor is not None:
        raise ChannelRuntimeNotIntegratedError(CHANNEL_RUNTIME_NOT_INTEGRATED)


def _contained_file(root: Path, relative: str) -> Path:
    path = root / relative
    if not path.resolve().is_relative_to(root):
        raise ValueError("Channel file escapes code root")
    if any(
        part.is_symlink() for part in (path, *path.parents) if part != root
    ):
        raise ValueError("Channel files cannot use symlinks")
    if not path.is_file():
        raise FileNotFoundError(path)
    return path


@dataclass(frozen=True)
class ChannelRuntimeSettings:
    """Trusted host execution policy; never read from channel configuration."""

    python_executable: str
    support_root: Path
    qwenpaw_version: str
    selected_channels: frozenset[str] = frozenset()
    dependency_paths: tuple[Path, ...] = ()
    # Includes dependency-origin validation and the platform SDK's import.
    startup_timeout: float = 10.0
    shutdown_timeout: float = 2.0
    admission_timeout: float = 30.0
    dependency_install_target: Path | None = None
    runtime_identity: Mapping[str, Any] | None = None

    def __post_init__(self):
        if not self.selected_channels <= {"feishu", "dingtalk"}:
            raise ValueError("Unsupported process channel selection")
        if (
            min(
                self.startup_timeout,
                self.shutdown_timeout,
                self.admission_timeout,
            )
            <= 0
        ):
            raise ValueError("Channel timeouts must be positive")


@dataclass(frozen=True)
class ChannelDescription:
    """One plugin's metadata and source identity, without loading a driver."""

    plugin_id: str
    version: str
    descriptor: SharedChannelDescriptor
    descriptor_path: Path
    code_root: Path
    requirements_path: Path
    requirements: tuple[str, ...]
    source_revision: str
    name: str
    description: str

    def create_proxy(
        self,
        process,
        config: Mapping[str, Any],
        *,
        python_executable: str,
        support_root: Path,
        instance_id: str,
        generation: int,
        qwenpaw_version: str,
        dependency_paths: tuple[Path, ...] = (),
        runtime_identity=None,
        workspace_dir: Path | None = None,
        on_reply_sent=None,
        display_config=None,
        no_text_debounce: bool = True,
        startup_timeout: float = 5.0,
        shutdown_timeout: float = 2.0,
    ):
        """Validate light host config and construct an unstarted proxy."""
        from ..config.config import DingTalkConfig, FeishuConfig
        from ..app.channels.isolated_proxy import IsolatedChannelProxy
        from ..app.channels.process_supervisor import LaunchSpec

        model = {"feishu": FeishuConfig, "dingtalk": DingTalkConfig}.get(
            self.descriptor.channel_key,
        )
        if model is None:
            raise ValueError("Unsupported Channel configuration model")
        raw = config.model_dump() if hasattr(config, "model_dump") else config
        values = model.model_validate(raw).model_dump(mode="json")
        values["no_text_debounce"] = no_text_debounce
        spec = LaunchSpec(
            python_executable=python_executable,
            support_root=Path(support_root),
            code_root=self.code_root,
            descriptor_path=self.descriptor_path,
            source_revision=self.source_revision,
            channel_key=self.descriptor.channel_key,
            instance_id=instance_id,
            generation=generation,
            qwenpaw_version=qwenpaw_version,
            config=values,
            dependency_paths=dependency_paths,
            runtime_identity=runtime_identity,
            enabled=values["enabled"],
            startup_timeout=startup_timeout,
            shutdown_timeout=shutdown_timeout,
        )
        return IsolatedChannelProxy(
            process,
            launch_spec=spec,
            workspace_dir=workspace_dir,
            on_reply_sent=on_reply_sent,
            display_config=display_config,
        )


def describe_channel(
    source_path: Path,
    manifest: PluginManifest,
) -> ChannelDescription:
    """Validate source metadata without loading implementation code."""
    if (
        manifest.plugin_type != PluginType.CHANNEL
        or not manifest.entry.descriptor
    ):
        raise ValueError("Channel descriptor entry is required")
    root = Path(source_path).resolve()
    descriptor_path = _contained_file(root, manifest.entry.descriptor)
    descriptor = SharedChannelDescriptor.load(descriptor_path)
    if manifest.id != descriptor.channel_key:
        raise ValueError("Channel descriptor owner does not match manifest id")
    requirements_path = _contained_file(
        root,
        (descriptor_path.parent / descriptor.requirements_file)
        .relative_to(root)
        .as_posix(),
    )
    if requirements_path != root / "requirements.txt":
        raise ValueError("Channel requirements must live at the plugin root")
    requirements = _read_requirements(requirements_path)
    if (
        manifest.dependencies
        and canonicalize_requirements(manifest.dependencies) != requirements
    ):
        raise ValueError("Manifest dependencies differ from requirements.txt")
    return ChannelDescription(
        plugin_id=manifest.id,
        version=manifest.version,
        descriptor=descriptor,
        descriptor_path=descriptor_path,
        code_root=root,
        requirements_path=requirements_path,
        requirements=requirements,
        source_revision=code_root_digest(root),
        name=manifest.name,
        description=manifest.description,
    )


@dataclass(frozen=True)
class StaticChannelDefinition:
    """Register metadata under the same lifecycle ledger as Python plugins."""

    description: ChannelDescription
    runtime_settings: ChannelRuntimeSettings | None = None
    global_enabled: bool = True

    def register(self, api) -> None:
        api.register_channel_descriptor(
            self.description,
            runtime_settings=self.runtime_settings,
            global_enabled=self.global_enabled,
        )
