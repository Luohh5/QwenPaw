# -*- coding: utf-8 -*-
"""Trusted Runner protocol host identity and hello construction."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, TYPE_CHECKING

from .errors import DescriptorValidationError, ProtocolValidationError
from .identifiers import validate_digest
from .models import HelloParams, PROTOCOL_VERSION

if TYPE_CHECKING:
    from .shared import SharedHelloParams


_LAUNCH_IDENTITY_FIELDS = frozenset(
    {
        "qwenpaw_version",
        "channel_key",
        "instance_id",
        "environment_spec_id",
        "environment_id",
        "lock_sha256",
        "python_abi",
        "platform_tag",
        "generation",
        "capabilities",
    },
)


@dataclass(frozen=True, slots=True)
class RunnerLaunchIdentity:
    """Hold non-source identity supplied to one Runner process."""

    qwenpaw_version: str
    channel_key: str
    instance_id: str
    environment_spec_id: str
    environment_id: str
    lock_sha256: str
    python_abi: str
    platform_tag: str
    generation: int
    capabilities: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, value: object) -> "RunnerLaunchIdentity":
        """Parse the closed launch identity without source authority."""
        if not isinstance(value, Mapping) or any(
            not isinstance(key, str) for key in value
        ):
            raise ProtocolValidationError("launch identity must be an object")
        data = dict(value)
        if set(data) != _LAUNCH_IDENTITY_FIELDS:
            raise ProtocolValidationError(
                "launch identity fields do not match v1",
            )
        generation = data["generation"]
        if (
            not isinstance(generation, int)
            or isinstance(generation, bool)
            or generation < 1
        ):
            raise ProtocolValidationError(
                "generation must be a positive integer",
                path=("generation",),
            )
        hello = HelloParams.from_mapping(
            {
                **{
                    name: item
                    for name, item in data.items()
                    if name != "generation"
                },
                "protocol_version": PROTOCOL_VERSION,
                "source_revision": "0" * 64,
            },
        )
        return cls(
            qwenpaw_version=hello.qwenpaw_version,
            channel_key=hello.channel_key,
            instance_id=hello.instance_id,
            environment_spec_id=hello.environment_spec_id,
            environment_id=hello.environment_id,
            lock_sha256=hello.lock_sha256,
            python_abi=hello.python_abi,
            platform_tag=hello.platform_tag,
            generation=generation,
            capabilities=hello.capabilities,
        )


@dataclass(frozen=True, slots=True)
class RunnerLifecycleSpec:
    """Describe Driver hooks without carrying trusted source identity."""

    controller_class: Any
    args: tuple[Any, ...]
    kwargs: Mapping[str, Any]

    def __post_init__(self) -> None:
        """Freeze controller arguments and reject source authority."""
        if not callable(self.controller_class):
            raise TypeError("controller_class must be callable")
        if "source_revision" in self.kwargs:
            raise TypeError("source_revision is owned by RunnerProtocolHost")
        object.__setattr__(
            self,
            "kwargs",
            MappingProxyType(dict(self.kwargs)),
        )


@dataclass(frozen=True, slots=True, init=False)
class RunnerProtocolHost:
    """Construct hello from trusted source and non-source launch identity."""

    _source_revision: str

    def __init__(self, source_revision: str) -> None:
        try:
            object.__setattr__(
                self,
                "_source_revision",
                validate_digest(
                    source_revision,
                    name="Source revision",
                ),
            )
        except DescriptorValidationError as exc:
            raise ProtocolValidationError(
                str(exc),
                path=("source_revision",),
            ) from exc

    @property
    def source_revision(self) -> str:
        """Return the immutable revision supplied by trusted bootstrap."""
        return self._source_revision

    def create_hello(self, identity: Any) -> HelloParams:
        """Build hello without consulting Driver-controlled source data."""
        return HelloParams.from_mapping(
            {
                "protocol_version": PROTOCOL_VERSION,
                "qwenpaw_version": identity.qwenpaw_version,
                "channel_key": identity.channel_key,
                "instance_id": identity.instance_id,
                "source_revision": self._source_revision,
                "environment_spec_id": identity.environment_spec_id,
                "environment_id": identity.environment_id,
                "lock_sha256": identity.lock_sha256,
                "python_abi": identity.python_abi,
                "platform_tag": identity.platform_tag,
                "capabilities": list(identity.capabilities),
            },
        )

    def create_shared_hello(
        self,
        *,
        channel_key: str,
        instance_id: str,
        generation: int,
        qwenpaw_version: str,
        requirements_path: str | Path,
        capabilities: tuple[str, ...] = (),
        versions: Mapping[str, str] | None = None,
    ) -> SharedHelloParams:
        """Observe actual child metadata before importing any Driver."""
        from packaging.markers import default_environment
        from packaging.tags import sys_tags

        from .identifiers import current_python_abi
        from .shared import (
            SHARED_PROTOCOL_VERSION,
            SharedHelloParams,
            audit_shared_dependencies,
        )

        audit = audit_shared_dependencies(requirements_path, versions=versions)
        audit.require_satisfied()
        return SharedHelloParams.from_mapping(
            {
                "protocol_version": SHARED_PROTOCOL_VERSION,
                "dependency_mode": "shared",
                "channel_key": channel_key,
                "instance_id": instance_id,
                "generation": generation,
                "qwenpaw_version": qwenpaw_version,
                "source_revision": self._source_revision,
                "python_abi": current_python_abi(),
                "platform_tag": next(sys_tags()).platform,
                "dependencies": audit.to_mapping(),
                "marker_environment": default_environment(),
                "capabilities": list(capabilities),
            },
        )

    def create_lifecycle_controller(
        self,
        spec: RunnerLifecycleSpec,
    ) -> Any:
        """Construct Runner lifecycle with the host-owned source revision."""
        if not isinstance(spec, RunnerLifecycleSpec):
            raise TypeError("lifecycle spec must be RunnerLifecycleSpec")
        return spec.controller_class(
            *spec.args,
            source_revision=self._source_revision,
            **spec.kwargs,
        )

    async def exchange_hello(
        self,
        peer: Any,
        controller: Any,
        identity: Any,
        *,
        hello: HelloParams | None = None,
    ) -> dict[str, Any]:
        """Accept and send the host-owned hello before prepare."""
        selected = hello or self.create_hello(identity)
        if selected.source_revision != self._source_revision:
            raise TypeError("hello source is owned by RunnerProtocolHost")
        controller.accept_hello(selected)
        return await peer.call("runner.hello", selected.to_mapping())
