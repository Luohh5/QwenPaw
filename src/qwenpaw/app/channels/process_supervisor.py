# -*- coding: utf-8 -*-
"""Channel handshake adapter over the public plugin process component."""

from __future__ import annotations
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from ...plugins.execution.process import (  # pylint: disable=unused-import
    PluginProcess,
    ProcessError,
    CallUnknown,
    StopResult,
)
from ...channel_protocol.models import IdentityParams
from ...channel_protocol.rpc import RpcPeer
from ...channel_protocol.shared import (
    SharedChannelDescriptor,
    validate_shared_hello,
)


@dataclass(frozen=True)
class LaunchSpec:
    """Explicit trusted executable/code/site roots and private launch data."""

    python_executable: str
    support_root: Path
    code_root: Path
    descriptor_path: Path
    source_revision: str
    channel_key: str
    instance_id: str
    generation: int
    qwenpaw_version: str
    config: Mapping[str, Any] = field(repr=False)
    dependency_paths: tuple[Path, ...] = ()
    enabled: bool = True
    startup_timeout: float = 5.0
    shutdown_timeout: float = 2.0
    runtime_identity: Mapping[str, Any] | None = None
    restart_limit: int = 0
    restart_backoff: float = 0.1


ChannelProcessError = ProcessError
ChannelCallUnknown = CallUnknown


class ChannelProcess(PluginProcess):
    """Preserve Channel identity, platform states and shared v2 wire data."""

    identity_type = IdentityParams
    hello_method = "runner.hello"
    control_prefix = "shared."
    state_key = "platform_state"
    request_prefix = "channel"
    initial_state = "disconnected"
    connecting_state = "connecting"
    component_states = {"connecting", "connected", "disconnected", "failed"}
    failed_reason = "PLATFORM_START_FAILED"

    async def _evidence(self, params, request):
        result = await super()._evidence(params, request)
        from ...plugins.channel_bootstrap import validate_runtime_identity
        from ...plugins.execution.errors import ProtocolValidationError

        try:
            validate_runtime_identity(
                self.dependency_snapshot,
                self._spec.runtime_identity,
                self._spec.python_executable,
            )
        except ValueError as exc:
            reason = "CHANNEL_RUNTIME_IDENTITY_MISMATCH"
            if self._ready is not None and not self._ready.done():
                self._ready.set_exception(self.error_type(reason))
            raise ProtocolValidationError(reason, reason_code=reason) from exc
        return result

    def _identity(self, spec):
        return IdentityParams.from_mapping(
            {
                name: getattr(spec, name)
                for name in ("channel_key", "instance_id", "generation")
            },
        )

    def _validate_spec(self, spec):
        self._validate_launch(spec)
        path = Path(spec.descriptor_path)
        if not path.is_absolute() or not path.exists():
            raise ChannelProcessError("LAUNCH_PATH_INVALID")
        if not path.resolve().is_relative_to(Path(spec.code_root).resolve()):
            raise ChannelProcessError("DESCRIPTOR_OUTSIDE_CODE_ROOT")
        self._identity(spec)

    def _packet(self, spec):
        packet = {
            name: getattr(spec, name)
            for name in (
                "source_revision",
                "channel_key",
                "instance_id",
                "generation",
                "qwenpaw_version",
                "startup_timeout",
            )
        }
        packet.update(
            {
                name: str(Path(getattr(spec, name)).resolve())
                for name in ("support_root", "code_root", "descriptor_path")
            },
        )
        packet["dependency_paths"] = [
            str(Path(path).resolve()) for path in spec.dependency_paths
        ]
        descriptor = SharedChannelDescriptor.load(spec.descriptor_path)
        packet["requirements_path"] = str(
            (
                Path(spec.descriptor_path).parent
                / descriptor.requirements_file
            ).resolve(),
        )
        packet["config"] = dict(spec.config)
        return self._encode_packet(packet)

    def _worker_path(self, spec):
        return (
            Path(spec.support_root)
            / "qwenpaw/channel_protocol/shared_worker.py"
        )

    def _make_peer(self, transport):
        return RpcPeer(transport)

    async def _hello(self, hello, _request):
        spec = self._spec
        descriptor = SharedChannelDescriptor.load(spec.descriptor_path)
        validate_shared_hello(
            hello,
            channel_key=spec.channel_key,
            instance_id=spec.instance_id,
            generation=self._generation,
            source_revision=spec.source_revision,
            qwenpaw_version=spec.qwenpaw_version,
            requirements_path=Path(spec.descriptor_path).parent
            / descriptor.requirements_file,
        )
        self.observed_hello = (
            hello.to_mapping() if hasattr(hello, "to_mapping") else dict(hello)
        )
        return {"accepted": True}
