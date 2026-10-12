# -*- coding: utf-8 -*-
# pylint: disable=protected-access
"""Standalone bootstrap for an isolated Channel Runner process."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import types
from typing import Any, TYPE_CHECKING

_MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "source_revision",
        "descriptor_path",
        "descriptor_sha256",
        "allowed_platform_tags",
    },
)
_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_STARTUP_ENVIRONMENT = frozenset(
    {
        "COMSPEC",
        "PATH",
        "PATHEXT",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "WINDIR",
    },
)
_AMBIENT_IMPORT_ENVIRONMENT = frozenset({"PYTHONHOME", "PYTHONPATH"})
_THREAD_TERMINATE = 0x0001
_ERROR_NOT_FOUND = 1168


# Absolute-path execution cannot import the package before validating roots.
def _execution_helpers():
    path = (
        Path(__file__).resolve().parents[1] / "plugins/execution/bootstrap.py"
    )
    spec = importlib.util.spec_from_file_location(
        "_qwenpaw_execution_bootstrap",
        path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("BOOTSTRAP_UNAVAILABLE")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_execution = _execution_helpers()
if TYPE_CHECKING:
    from ..plugins.execution.bootstrap import (
        BootstrapError,
        _ThreadPipeWriter as _SharedThreadPipeWriter,
    )
else:
    BootstrapError = _execution.BootstrapError
    _SharedThreadPipeWriter = _execution._ThreadPipeWriter
_ThreadPipeReader = _execution._ThreadPipeReader
_WindowsThreadHandle = _execution._WindowsThreadHandle
_open_windows_thread_handle = _execution._open_windows_thread_handle
_fail = _execution._fail
_require_isolated_python = _execution._require_isolated_python
_absolute_existing_file = _execution._absolute_existing_file
_absolute_existing_directory = _execution._absolute_existing_directory
_hash_code_root = _execution._hash_code_root
_clean_environment = _execution._clean_environment
_capture_protocol_output = _execution._capture_protocol_output
_redirect_ordinary_output = _execution._redirect_ordinary_output
_entrypoint_source = _execution._entrypoint_source
_load_driver_class = _execution._load_driver_class


class _ThreadPipeWriter(_SharedThreadPipeWriter):
    """Retain the legacy test/host HANDLE factory injection point."""

    def __init__(self, handle):
        super().__init__(
            handle,
            thread_handle_factory=_open_windows_thread_handle,
        )


async def _open_protocol_transport(
    framed_transport_class,
    protocol_handle,
    *,
    input_handle=None,
    limits=None,
):
    return await _execution._open_protocol_transport(
        framed_transport_class,
        protocol_handle,
        input_handle=input_handle,
        limits=limits,
        platform_name=os.name,
        writer_factory=_ThreadPipeWriter,
    )


class _RunnerProcess:
    """Own the validated driver instance and protocol transport."""

    def __init__(
        self,
        descriptor: Any,
        transport: Any,
        source_revision: str,
        identity: Any | None,
        protocol_host_class: Any,
        rpc_peer_class: Any,
    ) -> None:
        self._descriptor = descriptor
        self._transport = transport
        self._identity = identity
        self._protocol_host = protocol_host_class(source_revision)
        self._rpc_peer_class = rpc_peer_class
        self._driver: Any | None = None

    async def start(self, expected_source: Path) -> None:
        """Load the Driver and run its trusted protocol session."""
        identity = self._identity
        hello = (
            self._protocol_host.create_hello(identity)
            if identity is not None
            else None
        )
        driver_class = _load_driver_class(
            self._descriptor.entrypoint,
            expected_source,
        )
        if not isinstance(driver_class, type):
            raise _fail(
                "ENTRYPOINT_INVALID",
                "Runner entrypoint must be a ChannelDriver class",
            )
        self._driver = driver_class()
        if identity is None:
            if callable(
                getattr(self._driver, "create_lifecycle_spec", None),
            ):
                raise _fail(
                    "LAUNCH_IDENTITY_REQUIRED",
                    "ChannelDriver requires a launch identity",
                )
            return
        required = (
            "bind",
            "create_lifecycle_spec",
            "attach_lifecycle",
        )
        if any(
            not callable(getattr(self._driver, name, None))
            for name in required
        ):
            raise _fail(
                "ENTRYPOINT_INVALID",
                "Runner entrypoint does not implement ChannelDriver",
            )
        peer = self._rpc_peer_class(self._transport)
        self._driver.bind(peer, identity)
        lifecycle_spec = self._driver.create_lifecycle_spec(
            identity,
            secret_handle_consumer=None,
        )
        controller = self._protocol_host.create_lifecycle_controller(
            lifecycle_spec,
        )
        controller.register_rpc_methods(peer)
        self._driver.attach_lifecycle(controller)
        await peer.start()
        try:
            await self._protocol_host.exchange_hello(
                peer,
                controller,
                identity,
                hello=hello,
            )
            await peer.wait_closed()
        finally:
            stop = getattr(self._driver, "stop", None)
            if callable(stop):
                result = self._driver.stop()
                if hasattr(result, "__await__"):
                    await result
            await peer.aclose()


def _parse_arguments(argv: list[str]) -> tuple[Path, Path, Path | None]:
    """Parse the closed v1 command-line surface."""
    if len(argv) not in {5, 7} or argv[1] != "--code-root":
        raise _fail(
            "INVALID_BOOTSTRAP_ARGUMENT",
            "Expected code root, manifest, and optional launch identity",
        )
    if argv[2] == "" or argv[3] != "--manifest" or argv[4] == "":
        raise _fail(
            "INVALID_BOOTSTRAP_ARGUMENT",
            "Expected code root, manifest, and optional launch identity",
        )
    identity_path: Path | None = None
    if len(argv) == 7:
        if argv[5] != "--launch-identity" or argv[6] == "":
            raise _fail(
                "INVALID_BOOTSTRAP_ARGUMENT",
                "Expected --launch-identity <path>",
            )
        identity_path = _absolute_existing_file(
            argv[6],
            "launch_identity_path",
        )
    return (
        _absolute_existing_directory(argv[2], "code_root"),
        _absolute_existing_file(argv[4], "manifest_path"),
        identity_path,
    )


def _read_launch_identity(path: Path | None) -> object | None:
    """Read non-source launch identity before importing Channel code."""
    if path is None:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise _fail(
            "LAUNCH_IDENTITY_INVALID",
            "Launch identity is not valid JSON",
        ) from exc


def _read_manifest(path: Path) -> dict[str, Any]:
    """Read the closed integrity manifest before importing Channel code."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise _fail("MANIFEST_INVALID", "Manifest is not valid JSON") from exc
    if not isinstance(data, dict) or set(data) != _MANIFEST_FIELDS:
        raise _fail("MANIFEST_INVALID", "Manifest fields do not match v1")
    if data["schema_version"] != 1:
        raise _fail("MANIFEST_INVALID", "Manifest schema_version must be 1")
    for field in ("source_revision", "descriptor_sha256"):
        value = data[field]
        if (
            not isinstance(value, str)
            or _DIGEST_PATTERN.fullmatch(value) is None
        ):
            raise _fail(
                "MANIFEST_INVALID",
                f"Manifest {field} is invalid",
            )
    descriptor_path = data["descriptor_path"]
    if (
        not isinstance(descriptor_path, str)
        or not Path(
            descriptor_path,
        ).is_absolute()
    ):
        raise _fail(
            "MANIFEST_INVALID",
            "Manifest descriptor_path must be absolute",
        )
    platform_tags = data["allowed_platform_tags"]
    if (
        not isinstance(platform_tags, list)
        or not platform_tags
        or any(
            not isinstance(tag, str) or not tag or not tag.isascii()
            for tag in platform_tags
        )
        or len(platform_tags) != len(set(platform_tags))
    ):
        raise _fail(
            "MANIFEST_INVALID",
            "Manifest allowed_platform_tags is invalid",
        )
    return data


def _sha256_file(path: Path) -> str:
    """Hash one exact file without applying descriptor semantics."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _validate_manifest(code_root: Path, data: dict[str, Any]) -> Path:
    """Validate the code root and exact descriptor integrity."""
    try:
        actual = _hash_code_root(code_root)
    except OSError as exc:
        raise _fail("CODE_ROOT_INVALID", "Unable to hash code_root") from exc
    if actual != data["source_revision"]:
        raise _fail(
            "SOURCE_REVISION_MISMATCH",
            "code_root digest does not match source_revision",
        )
    try:
        descriptor_path = Path(data["descriptor_path"]).resolve(
            strict=True,
        )
    except OSError as exc:
        raise _fail(
            "DESCRIPTOR_INVALID",
            "Descriptor path does not exist",
        ) from exc
    if not descriptor_path.is_file() or not descriptor_path.is_relative_to(
        code_root,
    ):
        raise _fail(
            "DESCRIPTOR_OUTSIDE_CODE_ROOT",
            "Descriptor is not a regular file inside code_root",
        )
    try:
        descriptor_digest = _sha256_file(descriptor_path)
    except OSError as exc:
        raise _fail(
            "DESCRIPTOR_INVALID",
            "Unable to hash descriptor",
        ) from exc
    if descriptor_digest != data["descriptor_sha256"]:
        raise _fail(
            "DESCRIPTOR_MISMATCH",
            "Descriptor digest does not match",
        )
    return descriptor_path


def _remove_ambient_import_environment() -> None:
    """Remove import path variables before loading the Protocol SDK."""
    for name in tuple(os.environ):
        if name.upper() in _AMBIENT_IMPORT_ENVIRONMENT:
            os.environ.pop(name, None)


def _runner_support_root() -> Path:
    """Resolve the trusted support root containing this bootstrap and SDK."""
    bootstrap_path = Path(__file__).resolve()
    try:
        support_root = bootstrap_path.parents[2]
    except IndexError as exc:
        raise _fail(
            "PROTOCOL_SDK_INVALID",
            "Runner support artifact layout is invalid",
        ) from exc
    protocol_root = support_root / "qwenpaw" / "channel_protocol"
    if not (
        bootstrap_path.parent == protocol_root
        and (protocol_root / "descriptor.py").is_file()
        and (protocol_root / "framing.py").is_file()
        and (protocol_root / "errors.py").is_file()
        and (protocol_root / "runner_host.py").is_file()
    ):
        raise _fail(
            "PROTOCOL_SDK_INVALID",
            "Runner support artifact does not contain the Protocol SDK",
        )
    return support_root


def _reject_protocol_sdk_shadow(code_root: Path) -> None:
    """Reject a Channel artifact that can shadow the trusted Protocol SDK."""
    candidates = (
        code_root / "qwenpaw" / "channel_protocol",
        code_root / "qwenpaw" / "channel_protocol.py",
    )
    if any(path.exists() or path.is_symlink() for path in candidates):
        raise _fail(
            "PROTOCOL_SDK_SHADOWED",
            "Channel code_root must not provide the Protocol SDK",
        )


def _configure_import_roots(code_root: Path) -> Path:
    """Load protocol code from support and Channel code from code_root."""
    support_root = _runner_support_root()
    _reject_protocol_sdk_shadow(code_root)
    selected = str(code_root)
    support = str(support_root)
    sys.dont_write_bytecode = True
    sys.path[:] = [selected, support] + [
        item
        for item in sys.path
        if item
        and Path(item).resolve() != code_root
        and Path(item).resolve() != support_root
    ]
    package = types.ModuleType("qwenpaw")
    package.__package__ = "qwenpaw"
    package_paths: list[str] = []
    channel_namespace = code_root / "qwenpaw"
    if channel_namespace.is_dir():
        package_paths.append(str(channel_namespace))
    package_paths.append(str(support_root / "qwenpaw"))
    package.__path__ = package_paths
    sys.modules["qwenpaw"] = package
    return support_root


def _load_protocol_sdk(
    support_root: Path,
) -> tuple[Any, Any, type[BaseException], Any, Any, Any]:
    """Load descriptor and framing only from the trusted support root."""
    try:
        descriptor_module = importlib.import_module(
            "qwenpaw.channel_protocol.descriptor",
        )
        framing_module = importlib.import_module(
            "qwenpaw.channel_protocol.framing",
        )
        errors_module = importlib.import_module(
            "qwenpaw.channel_protocol.errors",
        )
        runner_host_module = importlib.import_module(
            "qwenpaw.channel_protocol.runner_host",
        )
        rpc_module = importlib.import_module(
            "qwenpaw.channel_protocol.rpc",
        )
    except ImportError as exc:
        raise _fail(
            "PROTOCOL_SDK_IMPORT_FAILED",
            "Unable to import the Channel Protocol SDK",
        ) from exc
    expected_root = support_root / "qwenpaw" / "channel_protocol"
    for module in (
        descriptor_module,
        framing_module,
        errors_module,
        runner_host_module,
        rpc_module,
    ):
        module_file = getattr(module, "__file__", None)
        if module_file is None or not Path(
            module_file,
        ).resolve().is_relative_to(
            expected_root,
        ):
            raise _fail(
                "PROTOCOL_SDK_INVALID",
                "Protocol SDK was not loaded from Runner support",
            )
    return (
        descriptor_module.ChannelDescriptor,
        framing_module.FramedTransport,
        errors_module.DescriptorValidationError,
        runner_host_module.RunnerLaunchIdentity,
        runner_host_module.RunnerProtocolHost,
        rpc_module.RpcPeer,
    )


def _parse_launch_identity(value: object | None, identity_class: Any) -> Any:
    """Validate the closed non-source launch identity with trusted SDK."""
    if value is None:
        return None
    try:
        return identity_class.from_mapping(value)
    except Exception as exc:
        raise _fail(
            "LAUNCH_IDENTITY_INVALID",
            "Launch identity failed v1 validation",
        ) from exc


def _load_descriptor(
    path: Path,
    manifest: dict[str, Any],
    descriptor_class: Any,
    validation_error: type[BaseException],
) -> Any:
    """Parse the descriptor through the CH-0-002 validator."""
    try:
        data = path.read_bytes()
        descriptor = descriptor_class.from_json(
            data,
            allowed_platform_tags=manifest["allowed_platform_tags"],
        )
    except (OSError, UnicodeError, validation_error) as exc:
        raise _fail(
            "DESCRIPTOR_INVALID",
            "Descriptor failed v1 validation",
        ) from exc
    if (
        descriptor.process_mode != "runner_process"
        or descriptor.entrypoint.scope != "runner"
    ):
        raise _fail(
            "DESCRIPTOR_NOT_RUNNER",
            "Descriptor does not declare a Runner ChannelDriver",
        )
    return descriptor


def _bind_launch_identity_to_descriptor(
    identity: Any | None,
    descriptor: Any,
) -> Any | None:
    """Bind descriptor-owned launch claims before Channel import."""
    if identity is None:
        return None
    if (
        identity.channel_key != descriptor.channel_key
        or identity.capabilities != descriptor.capabilities
    ):
        raise _fail(
            "LAUNCH_IDENTITY_MISMATCH",
            "Launch identity does not match the verified descriptor",
        )
    return replace(
        identity,
        channel_key=descriptor.channel_key,
        capabilities=descriptor.capabilities,
    )


async def _start_runner(
    descriptor: Any,
    expected_source: Path,
    source_revision: str,
    identity: Any | None,
    framed_transport_class: Any,
    protocol_host_class: Any,
    rpc_peer_class: Any,
    protocol_handle: Any,
) -> None:
    """Start one driver with the CH-0-003 protocol transport."""
    transport = await _open_protocol_transport(
        framed_transport_class,
        protocol_handle,
        input_handle=sys.stdin.buffer if identity is not None else None,
    )
    runner = _RunnerProcess(
        descriptor,
        transport,
        source_revision,
        identity,
        protocol_host_class,
        rpc_peer_class,
    )
    try:
        await runner.start(expected_source)
    finally:
        await transport.aclose()


def _report_error(error: BootstrapError) -> None:
    """Report a pre-import failure on stderr without protocol output."""
    payload = {
        "error": error.reason_code,
        "message": str(error),
    }
    sys.stderr.write(f"{json.dumps(payload, separators=(',', ':'))}\n")
    sys.stderr.flush()


def main(argv: list[str] | None = None) -> int:
    """Validate, isolate, redirect output, then construct one driver."""
    arguments = list(sys.argv if argv is None else argv)
    try:
        _require_isolated_python()
        code_root, manifest_path, identity_path = _parse_arguments(arguments)
        manifest = _read_manifest(manifest_path)
        launch_identity = _read_launch_identity(identity_path)
        descriptor_path = _validate_manifest(code_root, manifest)
        _remove_ambient_import_environment()
        support_root = _configure_import_roots(code_root)
        (
            descriptor_class,
            framed_transport_class,
            validation_error,
            identity_class,
            protocol_host_class,
            rpc_peer_class,
        ) = _load_protocol_sdk(support_root)
        identity = _parse_launch_identity(
            launch_identity,
            identity_class,
        )
        descriptor = _load_descriptor(
            descriptor_path,
            manifest,
            descriptor_class,
            validation_error,
        )
        identity = _bind_launch_identity_to_descriptor(identity, descriptor)
        entrypoint_source = _entrypoint_source(
            code_root,
            descriptor.entrypoint,
        )
        _clean_environment(descriptor.environment_passthrough_allowlist)
        protocol_handle = _capture_protocol_output()
        _redirect_ordinary_output()
        asyncio.run(
            _start_runner(
                descriptor,
                entrypoint_source,
                manifest["source_revision"],
                identity,
                framed_transport_class,
                protocol_host_class,
                rpc_peer_class,
                protocol_handle,
            ),
        )
        return 0
    except BootstrapError as exc:
        _report_error(exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
