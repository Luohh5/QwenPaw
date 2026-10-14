# -*- coding: utf-8 -*-
"""Host-owned static Channel definitions; no executable plugin discovery."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping
from contextlib import asynccontextmanager, nullcontext

from .execution_backend import DefinitionResult, PluginDescription

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
    selected_channels: frozenset[str] | None = frozenset()
    dependency_paths: tuple[Path, ...] = ()
    # Includes dependency-origin validation and the platform SDK's import.
    startup_timeout: float = 10.0
    shutdown_timeout: float = 2.0
    admission_timeout: float = 30.0
    dependency_install_target: Path | None = None
    runtime_identity: Mapping[str, Any] | None = None

    def __post_init__(self):
        if (
            min(
                self.startup_timeout,
                self.shutdown_timeout,
                self.admission_timeout,
            )
            <= 0
        ):
            raise ValueError("Channel timeouts must be positive")

    def allows(self, channel_key: str) -> bool:
        """None admits descriptors; an explicit collection is a whitelist."""
        return (
            self.selected_channels is None
            or channel_key in self.selected_channels
        )


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
        from .channel_config import (
            channel_config_dict,
            validate_channel_config,
        )
        from ..app.channels.catalog import clean_channel_config
        from ..app.channels.isolated_proxy import IsolatedChannelProxy
        from ..app.channels.process_supervisor import LaunchSpec

        raw = clean_channel_config(
            channel_config_dict(config),
            self.descriptor.config_schema,
        )
        raw["no_text_debounce"] = no_text_debounce
        values = validate_channel_config(self.descriptor.config_schema, raw)
        controls = LaunchSpec.__dataclass_fields__.keys() - {
            "config",
            "enabled",
        }
        values = {
            key: value for key, value in values.items() if key not in controls
        }
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
    from ..channel_protocol.shared import validate_channel_contract
    from .channel_config import validate_channel_config_schema

    validate_channel_contract(descriptor.channel_key, descriptor.capabilities)
    validate_channel_config_schema(descriptor.config_schema)
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


@dataclass(frozen=True)
class ChannelServices:
    """Host services used by static Channel execution, without a Loader."""

    runtime_settings: Callable
    prepare_candidate: Callable
    own_preparation: Callable
    stop_preparation: Callable
    release_preparation: Callable
    acquire_wheels: Callable
    dependency_usage: Callable
    workspaces: Callable
    registration: Callable
    record: Callable


class ChannelBackend:
    """Static definitions and execution checks for isolated Channels."""

    discover_disabled = True
    probe_before_activation = True

    def __init__(self, services: ChannelServices):
        self.services = services
        self._prepared = {}

    def runtime_for(self, manifest):
        settings = self.services.runtime_settings()
        return settings if settings and settings.allows(manifest.id) else None

    def dependency_targets(self, description):
        """Configured roots serialize inspection; the child verifies them."""
        settings = self.runtime_for(description.manifest)
        if settings is None:
            return ()
        return (
            *settings.dependency_paths,
            *(
                (settings.dependency_install_target,)
                if settings.dependency_install_target
                else ()
            ),
        )

    def require_selection(self, manifest):
        if self.runtime_for(manifest) is None:
            raise ChannelRuntimeNotIntegratedError(
                CHANNEL_RUNTIME_NOT_INTEGRATED,
            )

    def describe(self, source_path, manifest):
        payload = describe_channel(source_path, manifest)
        return PluginDescription(
            manifest,
            Path(source_path),
            payload.requirements_path,
            payload,
            code_root_digest(
                source_path,
                ignored_paths=(".qwenpaw-pawport.json",),
            ),
        )

    def registration_scope(self, description):
        del description
        return nullcontext()

    async def prepare(
        self,
        description,
        *,
        allow_install,
        probe,
        wheels=(),
        owns_dependency_env=True,
    ):
        # Static discovery must not inspect/import an unselected platform SDK.
        if not allow_install and not probe:
            return
        self.require_selection(description.manifest)
        prepared = await self.services.prepare_candidate(
            description.payload,
            allow_install=allow_install and owns_dependency_env,
            probe=probe,
            wheels=wheels if owns_dependency_env else (),
        )
        snapshot = getattr(prepared, "snapshot", prepared)
        self._prepared[description.manifest.id] = (description, snapshot)
        record = self.services.record(description.manifest.id)
        if (
            record is not None
            and record.backend_binding is not None
            and record.backend_binding.description == description
        ):
            self._validation(description.manifest.id, passed=True)
            row = self.services.registration(description.manifest.id)
            if row is not None:
                self.services.dependency_usage().refine(
                    row.dependency_token,
                    snapshot,
                )
        return snapshot

    async def load_definition(
        self,
        description,
        config,
        *,
        saved_definition=None,
    ):
        from .settings import is_plugin_enabled

        del saved_definition
        settings = self.runtime_for(description.manifest)
        enabled = is_plugin_enabled(config)
        diagnostics = () if settings else (CHANNEL_RUNTIME_NOT_INTEGRATED,)
        prepared = self._prepared.get(description.manifest.id)
        snapshot = None
        if (
            prepared is not None
            and prepared[0].manifest == description.manifest
            and prepared[0].source_identity == description.source_identity
        ):
            self._prepared.pop(description.manifest.id)
            snapshot = prepared[1]
            diagnostics += ("channel_validation_passed",)
        if not enabled:
            diagnostics += ("channel_validation_pending",)
        return DefinitionResult(
            StaticChannelDefinition(description.payload, settings, enabled),
            enabled=settings is not None and enabled,
            status=(
                "inactive"
                if not enabled
                else "registered"
                if settings
                else "catalogued"
            ),
            diagnostics=diagnostics,
            dependency_snapshot=snapshot,
        )

    def _validation(self, plugin_id, *, passed):
        record = self.services.record(plugin_id)
        if record is None:
            return
        marker = (
            "channel_validation_passed"
            if passed
            else "channel_validation_pending"
        )
        if passed:
            record.diagnostics = [
                item
                for item in record.diagnostics
                if item != "channel_validation_pending"
            ]
        if marker not in record.diagnostics:
            record.diagnostics.append(marker)

    async def before_activate(self, description):
        from .api import channel_passes_gates

        plugin_id = description.manifest.id
        self.require_selection(description.manifest)
        record = self.services.record(plugin_id)
        if record is None or not record.enabled:
            return
        row = self.services.registration(plugin_id)
        targets = [
            workspace
            for workspace in self.services.workspaces()
            if channel_passes_gates(workspace, plugin_id)
            and getattr(workspace, "channel_manager", None) is not None
        ]
        for workspace in targets:
            owner = await workspace.channel_manager.get_channel(plugin_id)
            if (
                owner is not None
                and getattr(owner, "_registration", None) is not row
            ):
                raise RuntimeError("CHANNEL_ALREADY_OWNED")
        if targets and "channel_validation_passed" not in record.diagnostics:
            import asyncio
            from .dependency_usage import admission_context

            if row is None or row.description_record != description.payload:
                raise RuntimeError(
                    "DEPENDENCY_PLAN_UNVERIFIED: admission source mismatch",
                )
            context = admission_context.set(
                (
                    self.services.dependency_usage(),
                    row.dependency_token,
                    asyncio.current_task(),
                    description,
                ),
            )
            try:
                await self.prepare(
                    description,
                    allow_install=False,
                    probe=True,
                )
            finally:
                admission_context.reset(context)
        elif not {
            "channel_validation_passed",
            "channel_validation_pending",
        }.intersection(record.diagnostics):
            self._validation(plugin_id, passed=False)

    def validate_update(self, old, candidate, *, in_place):
        del old
        self.require_selection(candidate.manifest)
        if in_place:
            raise RuntimeError(
                "Channel in-place source update is not supported",
            )

    def validate_config_update(self, description, config):
        """Reject unintegrated mutations without probing disabled plugins."""
        del config
        self.require_selection(description.manifest)

    def capture_restore_state(self, record):
        """Static definitions require no host import snapshot."""
        del record

    def restore_state(self, state):
        """Static Channel definitions hold no host import state."""

    @asynccontextmanager
    async def _dependency_inspection(self, description):
        """Own only this inspection child; never stop another preparation."""
        from uuid import uuid4
        from ..app.channels.process_supervisor import (
            ChannelProcess,
            LaunchSpec,
        )

        settings = self.services.runtime_settings()
        if settings is None:
            raise ChannelRuntimeNotIntegratedError(
                CHANNEL_RUNTIME_NOT_INTEGRATED,
            )
        process, lease = self.services.own_preparation(
            description.plugin_id,
            ChannelProcess,
            description.code_root,
            instance_id="probe:" + uuid4().hex,
        )
        from .dependency_usage import DependencyDeclaration

        usage = self.services.dependency_usage()
        token = usage.declare(
            DependencyDeclaration(
                description.plugin_id,
                description.source_revision,
                description.requirements,
                None,
            ),
        )
        try:
            usage.bind(lease, token)
            process.dependency_observer = lambda snapshot: usage.observe(
                lease,
                snapshot,
            )
            spec = LaunchSpec(
                python_executable=settings.python_executable,
                support_root=settings.support_root,
                code_root=description.code_root,
                descriptor_path=description.descriptor_path,
                source_revision=description.source_revision,
                channel_key=description.descriptor.channel_key,
                instance_id=lease.instance_id,
                generation=lease.generation,
                qwenpaw_version=settings.qwenpaw_version,
                config={},
                dependency_paths=settings.dependency_paths,
                runtime_identity=settings.runtime_identity,
                startup_timeout=settings.startup_timeout,
                shutdown_timeout=settings.shutdown_timeout,
            )
            yield process, spec, token, usage
        finally:
            try:
                observed = getattr(process, "dependency_snapshot", None)
                if observed is not None:
                    usage.observe(lease, observed)
            finally:
                try:
                    await self.services.release_preparation(lease)
                finally:
                    usage.withdraw(token)

    async def audit_dependencies(self, description):
        """Inspect actual child metadata under the caller's exclusion."""
        async with self._dependency_inspection(description.payload) as values:
            process, spec, _token, _usage = values
            return (await process.audit(spec)).snapshot

    async def prepare_candidate(
        self,
        description,
        *,
        wheels=(),
        probe=True,
        allow_install=False,
    ):
        """One check/install/probe boundary for activation and C5 staging."""
        from .dependency_gate import (
            prepare_shared_dependencies,
            SharedDependencyError,
        )
        from .dependency_usage import current_admission_token

        await self.services.stop_preparation(description.plugin_id)
        async with self._dependency_inspection(description) as values:
            process, spec, token, usage = values
            admission = current_admission_token(
                usage,
                description.plugin_id,
                description.code_root,
            )
            settings = self.services.runtime_settings()
            try:
                snapshot = await prepare_shared_dependencies(
                    process,
                    spec,
                    plugin_id=description.plugin_id,
                    requirements_file=description.requirements_path,
                    usage_registry=usage,
                    admission_token=admission or token,
                    install_target=settings.dependency_install_target,
                    wheels=wheels,
                )
            except SharedDependencyError as exc:
                if (
                    not allow_install
                    or wheels
                    or exc.decision.require_restart
                    or exc.decision.unsupported
                ):
                    raise
                snapshot = (await process.audit(spec)).snapshot
                target = settings.dependency_install_target
                if target is None:
                    raise RuntimeError(
                        "DEPENDENCY_INSTALL_TARGET_REQUIRED",
                    ) from exc
                await self.services.acquire_wheels(
                    process,
                    spec,
                    snapshot,
                    target,
                    description.plugin_id,
                    description.requirements_path,
                )
                snapshot = await prepare_shared_dependencies(
                    process,
                    spec,
                    plugin_id=description.plugin_id,
                    requirements_file=description.requirements_path,
                    usage_registry=usage,
                    admission_token=admission or token,
                    install_target=settings.dependency_install_target,
                )
            return await process.probe(spec) if probe else snapshot
