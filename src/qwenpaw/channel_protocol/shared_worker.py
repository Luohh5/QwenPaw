# -*- coding: utf-8 -*-
# pylint: disable=protected-access
"""Shared Channel handshake and context adapter for the common worker."""

from __future__ import annotations
import importlib.util
import os
from pathlib import Path
import sys
from typing import Any


class DriverContext:
    """Preserve the Channel context and platform state wire contract."""

    def __init__(self, peer: Any, identity: Any) -> None:
        self.peer, self.identity = peer, identity

    async def set_platform_state(self, state: str) -> None:
        if state not in {"connecting", "connected", "disconnected", "failed"}:
            raise ValueError("PLATFORM_STATE_INVALID")
        await self.peer.notify(
            "shared.state",
            {**self.identity.to_mapping(), "platform_state": state},
        )


class ChannelRuntime:
    hello_method = "runner.hello"
    control_prefix = "shared."
    initial_state = "connecting"
    failed_state = "failed"

    def identity(self, packet):
        from .models import IdentityParams

        return IdentityParams.from_mapping(
            {
                name: packet[name]
                for name in ("channel_key", "instance_id", "generation")
            },
        )

    def peer(self, transport):
        from .rpc import RpcPeer

        return RpcPeer(transport)

    def context(self, peer, identity):
        return DriverContext(peer, identity)

    async def state(self, context, state):
        await context.set_platform_state(state)

    def verify(self, boot, packet, identity):
        from .runner_host import RunnerProtocolHost
        from .shared import SharedChannelDescriptor

        root = Path(packet["code_root"])
        if boot._hash_code_root(root) != packet["source_revision"]:
            raise boot.BootstrapError(
                "SOURCE_REVISION_MISMATCH",
                "source mismatch",
            )
        path = boot._absolute_existing_file(
            packet["descriptor_path"],
            "descriptor_path",
        )
        if not path.is_relative_to(root):
            raise boot.BootstrapError(
                "DESCRIPTOR_OUTSIDE_CODE_ROOT",
                "descriptor outside root",
            )
        descriptor = SharedChannelDescriptor.load(path)
        if descriptor.channel_key != identity.channel_key:
            raise boot.BootstrapError("IDENTITY_MISMATCH", "identity mismatch")
        requirements = (path.parent / descriptor.requirements_file).resolve(
            strict=True,
        )
        if not requirements.is_relative_to(root):
            raise boot.BootstrapError(
                "DEPENDENCY_PATH_INVALID",
                "requirements outside root",
            )
        source = boot._entrypoint_source(root, descriptor.entrypoint)
        hello = RunnerProtocolHost(
            packet["source_revision"],
        ).create_shared_hello(
            channel_key=identity.channel_key,
            instance_id=identity.instance_id,
            generation=identity.generation,
            qwenpaw_version=packet["qwenpaw_version"],
            requirements_path=requirements,
            capabilities=descriptor.capabilities,
        )
        return descriptor.entrypoint, source, hello.to_mapping()


def main() -> int:
    path = Path(__file__).resolve().parents[1] / "plugins/execution/worker.py"
    spec = importlib.util.spec_from_file_location(
        "_qwenpaw_execution_worker",
        path,
    )
    if spec is None or spec.loader is None:
        return 70
    worker = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = worker
    spec.loader.exec_module(worker)

    def runtime():
        runtime_class = importlib.import_module(
            "qwenpaw.channel_protocol.shared_worker",
        ).ChannelRuntime
        return runtime_class()

    return worker.main(
        runtime,
        protected_namespaces=(
            "qwenpaw/channel_protocol",
            "qwenpaw/channel_protocol.py",
        ),
    )


if __name__ == "__main__":
    os._exit(main())
