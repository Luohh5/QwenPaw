# -*- coding: utf-8 -*-
"""Select and strictly validate isolated Channel environments."""

from __future__ import annotations

from collections.abc import Collection, Mapping
import base64
from dataclasses import dataclass
import hashlib
from importlib import metadata
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
from typing import Literal
from urllib.parse import urlsplit
import zipfile

from packaging import tags as packaging_tags
from packaging.markers import Marker
from packaging.requirements import Requirement
from packaging.utils import InvalidName, canonicalize_name
from packaging.version import InvalidVersion, Version

from ...channel_protocol.artifacts import (
    LockFile,
    LockManifest,
    LockManifestEntry,
    LockPackage,
    read_json,
    sha256_file,
)
from ...channel_protocol.canonical import domain_sha256
from ...channel_protocol.descriptor import ChannelDescriptor
from ...channel_protocol.errors import (
    ArtifactValidationError,
    DescriptorValidationError,
)
from ...channel_protocol.identifiers import (
    DirectoryIdentity,
    EnvironmentIdentity,
    EnvironmentSpecIdentity,
    ENVIRONMENT_SPEC_DOMAIN,
    condition_set_sha256,
    validate_channel_key,
    validate_digest,
    validate_platform_tag,
    validate_python_abi,
)

SelectionErrorCode = Literal["config_invalid", "unsupported_platform"]
EnvironmentValidationStatus = Literal["installed", "repair_required"]

_HEX_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_WHEEL_NAME = re.compile(r"^[^/\\]+\.whl$")
_ENVIRONMENT_SPEC_FIELDS = frozenset(
    {
        "schema_version",
        "environment_spec_id",
        "channel_key",
        "lock_sha256",
        "python_abi",
        "platform_tag",
        "condition_set_sha256",
    },
)
_INSTALL_FIELDS = frozenset(
    {
        "schema_version",
        "environment_id",
        "environment_spec_id",
        "python_abi",
        "platform_tag",
        "lock_sha256",
        "source",
        "packages",
        "venv_tree_sha256",
    },
)
_INSTALL_SOURCE_FIELDS = frozenset({"kind", "base_url"})
_PROVENANCE_FIELDS = frozenset(
    {"name", "version", "wheel_filename", "wheel_sha256"},
)
_VENV_TREE_DOMAIN = "qwenpaw.channel.venv-tree.v1"
_PROBE_ENVIRONMENT_KEYS = frozenset(
    {
        "COMSPEC",
        "LANG",
        "LC_ALL",
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
_PACKAGING_ROOT = str(Path(packaging_tags.__file__).resolve().parent.parent)
_NO_SITE_PROBE_SCRIPT = """
import json
import os
import platform
import sys
import sysconfig

venv_root = sys.argv[1]
packaging_root = sys.argv[2]
sys.path.insert(0, packaging_root)
from packaging.tags import sys_tags

version = platform.python_version()
major, minor = sys.version_info[:2]
implementation = sys.implementation.name
tags = tuple(sys_tags())
abi = next(
    (f"{tag.interpreter}-{tag.abi}" for tag in tags if tag.abi != "none"),
    None,
)
marker_environment = {
    "implementation_name": implementation,
    "implementation_version": version,
    "os_name": os.name,
    "platform_machine": platform.machine(),
    "platform_release": platform.release(),
    "platform_system": platform.system(),
    "platform_version": platform.version(),
    "platform_python_implementation": platform.python_implementation(),
    "python_full_version": version,
    "python_version": f"{major}.{minor}",
    "sys_platform": sys.platform,
}
payload = {
    "abi": abi,
    "base_prefix": sys.base_prefix,
    "compatible_platform_tags": sorted({tag.platform for tag in tags}),
    "executable": sys.executable,
    "flags": {
        "ignore_environment": bool(sys.flags.ignore_environment),
        "isolated": bool(sys.flags.isolated),
        "no_site": bool(sys.flags.no_site),
        "no_user_site": bool(sys.flags.no_user_site),
    },
    "marker_environment": marker_environment,
    "prefix": sys.prefix,
    "pythonpath": os.environ.get("PYTHONPATH"),
}
venv_paths = sysconfig.get_paths(
    scheme="venv",
    vars={"base": venv_root, "platbase": venv_root},
)
payload["platlib"] = venv_paths["platlib"]
payload["purelib"] = venv_paths["purelib"]
print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
"""
_SITE_PROBE_SCRIPT = """
import json
import os
import site
import sys
import sysconfig

payload = {
    "executable": sys.executable,
    "flags": {
        "ignore_environment": bool(sys.flags.ignore_environment),
        "isolated": bool(sys.flags.isolated),
        "no_user_site": bool(sys.flags.no_user_site),
    },
    "platlib": sysconfig.get_paths()["platlib"],
    "prefix": sys.prefix,
    "purelib": sysconfig.get_paths()["purelib"],
    "pythonpath": os.environ.get("PYTHONPATH"),
    "site_packages": site.getsitepackages(),
    "sys_path": sys.path,
    "user_site": site.getusersitepackages(),
}
print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
"""


class EnvironmentManifestError(ValueError):
    """Report a malformed persisted environment manifest."""


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise EnvironmentManifestError("Manifest must contain an object")
    if any(not isinstance(key, str) for key in value):
        raise EnvironmentManifestError("Manifest keys must be strings")
    return value


def _closed(
    value: object,
    fields: frozenset[str],
) -> Mapping[str, object]:
    mapping = _mapping(value)
    if set(mapping) != fields:
        missing = sorted(fields - set(mapping))
        unknown = sorted(set(mapping) - fields)
        raise EnvironmentManifestError(
            f"Manifest fields do not match v1 shape: "
            f"missing={missing}, unknown={unknown}",
        )
    return mapping


def _string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise EnvironmentManifestError(f"{field} must be a non-empty string")
    return value


def _schema_version(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value != 1:
        raise EnvironmentManifestError("schema_version must be integer 1")
    return value


@dataclass(frozen=True)
class EnvironmentSpecManifest:
    """Closed persisted representation of an environment specification."""

    environment_spec_id: str
    channel_key: str
    lock_sha256: str
    python_abi: str
    platform_tag: str
    condition_set_sha256: str
    schema_version: int = 1

    @classmethod
    def from_identity(
        cls,
        identity: EnvironmentSpecIdentity,
    ) -> "EnvironmentSpecManifest":
        """Create a manifest from an already validated logical identity."""
        return cls(
            environment_spec_id=identity.environment_spec_id,
            channel_key=identity.channel_key,
            lock_sha256=identity.lock_sha256,
            python_abi=identity.python_abi,
            platform_tag=identity.platform_tag,
            condition_set_sha256=identity.condition_set_sha256,
        )

    @classmethod
    def from_mapping(
        cls,
        value: object,
        *,
        allowed_platform_tags: Collection[str],
    ) -> "EnvironmentSpecManifest":
        """Parse and strictly validate an environment spec manifest."""
        mapping = _closed(value, _ENVIRONMENT_SPEC_FIELDS)
        _schema_version(mapping["schema_version"])
        channel_key = _string(mapping["channel_key"], "channel_key")
        lock_sha256 = _string(mapping["lock_sha256"], "lock_sha256")
        validate_digest(lock_sha256, name="Lock digest")
        python_abi = _string(mapping["python_abi"], "python_abi")
        validate_python_abi(python_abi)
        platform_tag = _string(mapping["platform_tag"], "platform_tag")
        validate_platform_tag(
            platform_tag,
            allowed_platform_tags=allowed_platform_tags,
        )
        condition_digest = _string(
            mapping["condition_set_sha256"],
            "condition_set_sha256",
        )
        validate_digest(condition_digest, name="Condition digest")
        environment_spec_id = _string(
            mapping["environment_spec_id"],
            "environment_spec_id",
        )
        payload_identity = _identity_from_digest(
            channel_key=channel_key,
            lock_sha256=lock_sha256,
            python_abi=python_abi,
            platform_tag=platform_tag,
            condition_digest=condition_digest,
            allowed_platform_tags=allowed_platform_tags,
        )
        payload_identity.validate_id(environment_spec_id)
        return cls(
            environment_spec_id=environment_spec_id,
            channel_key=channel_key,
            lock_sha256=lock_sha256,
            python_abi=python_abi,
            platform_tag=platform_tag,
            condition_set_sha256=condition_digest,
        )

    def to_mapping(self) -> dict[str, object]:
        """Return the closed JSON representation."""
        return {
            "schema_version": self.schema_version,
            "environment_spec_id": self.environment_spec_id,
            "channel_key": self.channel_key,
            "lock_sha256": self.lock_sha256,
            "python_abi": self.python_abi,
            "platform_tag": self.platform_tag,
            "condition_set_sha256": self.condition_set_sha256,
        }


def _identity_from_digest(
    *,
    channel_key: str,
    lock_sha256: str,
    python_abi: str,
    platform_tag: str,
    condition_digest: str,
    allowed_platform_tags: Collection[str],
) -> EnvironmentSpecIdentity:
    """Rebuild an identity when only the persisted condition digest exists."""
    validate_channel_key(channel_key)
    validate_digest(lock_sha256, name="Lock digest")
    validate_python_abi(python_abi)
    validate_platform_tag(
        platform_tag,
        allowed_platform_tags=allowed_platform_tags,
    )
    validate_digest(condition_digest, name="Condition digest")
    payload = {
        "channel_key": channel_key,
        "condition_set_sha256": condition_digest,
        "lock_sha256": lock_sha256,
        "platform_tag": platform_tag,
        "python_abi": python_abi,
    }
    digest = domain_sha256(ENVIRONMENT_SPEC_DOMAIN, payload)
    return EnvironmentSpecIdentity(
        channel_key=channel_key,
        lock_sha256=lock_sha256,
        python_abi=python_abi,
        platform_tag=platform_tag,
        condition_set_sha256=condition_digest,
        environment_spec_id=f"ches1_{digest}",
    )


@dataclass(frozen=True)
class WheelProvenance:
    """Persisted wheel identity for one installed distribution."""

    name: str
    version: str
    wheel_filename: str
    wheel_sha256: str

    @classmethod
    def from_mapping(cls, value: object) -> "WheelProvenance":
        """Parse one closed wheel provenance entry."""
        mapping = _closed(value, _PROVENANCE_FIELDS)
        name = _string(mapping["name"], "name")
        try:
            canonical_name = canonicalize_name(name, validate=True)
        except InvalidName as exc:
            raise EnvironmentManifestError("Package name is invalid") from exc
        if canonical_name != name:
            raise EnvironmentManifestError("Package name must be canonical")
        version = _string(mapping["version"], "version")
        try:
            canonical_version = str(Version(version))
        except InvalidVersion as exc:
            raise EnvironmentManifestError(
                "Package version is invalid",
            ) from exc
        if canonical_version != version:
            raise EnvironmentManifestError("Package version must be canonical")
        filename = _string(mapping["wheel_filename"], "wheel_filename")
        if not _WHEEL_NAME.fullmatch(filename):
            raise EnvironmentManifestError("Wheel filename is invalid")
        digest = _string(mapping["wheel_sha256"], "wheel_sha256")
        if not _HEX_DIGEST.fullmatch(digest):
            raise EnvironmentManifestError(
                "Wheel digest must be 64 lowercase hex characters",
            )
        return cls(
            name=name,
            version=version,
            wheel_filename=filename,
            wheel_sha256=digest,
        )

    def to_mapping(self) -> dict[str, str]:
        """Return the closed JSON representation."""
        return {
            "name": self.name,
            "version": self.version,
            "wheel_filename": self.wheel_filename,
            "wheel_sha256": self.wheel_sha256,
        }


@dataclass(frozen=True)
class InstallSource:
    """Credential-free dependency source recorded for one installation."""

    kind: str
    base_url: str | None

    @classmethod
    def from_mapping(cls, value: object) -> "InstallSource":
        """Parse one closed credential-free source record."""
        mapping = _closed(value, _INSTALL_SOURCE_FIELDS)
        kind = _string(mapping["kind"], "kind")
        raw_url = mapping["base_url"]
        if raw_url is None:
            return cls(kind=kind, base_url=None)
        base_url = _string(raw_url, "base_url")
        try:
            parsed = urlsplit(base_url)
            port = parsed.port
        except ValueError as exc:
            raise EnvironmentManifestError(
                "Source base URL is invalid",
            ) from exc
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise EnvironmentManifestError(
                "Source base URL must be absolute HTTP(S)",
            )
        if parsed.username is not None or parsed.password is not None:
            raise EnvironmentManifestError(
                "Source base URL must not contain credentials",
            )
        if port is not None and not 0 < port < 65536:
            raise EnvironmentManifestError("Source base URL port is invalid")
        return cls(kind=kind, base_url=base_url)

    def to_mapping(self) -> dict[str, str | None]:
        """Return the closed JSON representation."""
        return {"kind": self.kind, "base_url": self.base_url}


@dataclass(frozen=True)
class InstallManifest:
    """Closed immutable metadata for one environment installation."""

    environment_id: str
    environment_spec_id: str
    python_abi: str
    platform_tag: str
    lock_sha256: str
    venv_tree_sha256: str
    source: InstallSource
    packages: tuple[WheelProvenance, ...]
    schema_version: int = 1

    @classmethod
    def from_mapping(
        cls,
        value: object,
        *,
        allowed_platform_tags: Collection[str],
    ) -> "InstallManifest":
        """Parse and strictly validate one installation manifest."""
        mapping = _closed(value, _INSTALL_FIELDS)
        _schema_version(mapping["schema_version"])
        environment_id = _string(
            mapping["environment_id"],
            "environment_id",
        )
        identity = EnvironmentIdentity.parse(environment_id)
        spec_id = _string(
            mapping["environment_spec_id"],
            "environment_spec_id",
        )
        if identity.environment_spec_id != spec_id:
            raise EnvironmentManifestError(
                "Environment ID does not match environment spec ID",
            )
        python_abi = _string(mapping["python_abi"], "python_abi")
        validate_python_abi(python_abi)
        platform_tag = _string(mapping["platform_tag"], "platform_tag")
        validate_platform_tag(
            platform_tag,
            allowed_platform_tags=allowed_platform_tags,
        )
        lock_sha256 = _string(mapping["lock_sha256"], "lock_sha256")
        validate_digest(lock_sha256, name="Lock digest")
        venv_tree_sha256 = _string(
            mapping["venv_tree_sha256"],
            "venv_tree_sha256",
        )
        validate_digest(venv_tree_sha256, name="Venv tree digest")
        source = InstallSource.from_mapping(mapping["source"])
        package_values = mapping["packages"]
        if not isinstance(package_values, list):
            raise EnvironmentManifestError("packages must be an array")
        packages = tuple(
            sorted(
                (
                    WheelProvenance.from_mapping(item)
                    for item in package_values
                ),
                key=lambda item: item.name,
            ),
        )
        names = [item.name for item in packages]
        if len(names) != len(set(names)):
            raise EnvironmentManifestError("Package names must be unique")
        return cls(
            environment_id=environment_id,
            environment_spec_id=spec_id,
            python_abi=python_abi,
            platform_tag=platform_tag,
            lock_sha256=lock_sha256,
            venv_tree_sha256=venv_tree_sha256,
            source=source,
            packages=packages,
        )

    def to_mapping(self) -> dict[str, object]:
        """Return the closed JSON representation."""
        return {
            "schema_version": self.schema_version,
            "environment_id": self.environment_id,
            "environment_spec_id": self.environment_spec_id,
            "python_abi": self.python_abi,
            "platform_tag": self.platform_tag,
            "lock_sha256": self.lock_sha256,
            "venv_tree_sha256": self.venv_tree_sha256,
            "source": self.source.to_mapping(),
            "packages": [item.to_mapping() for item in self.packages],
        }


@dataclass(frozen=True)
class EnvironmentSelection:
    """Result of selecting one exact release lock and environment spec."""

    environment_spec: EnvironmentSpecManifest | None
    lock: LockFile | None
    error_code: SelectionErrorCode | None = None
    reason: str | None = None

    @property
    def selected(self) -> bool:
        """Return whether selection produced one exact environment spec."""
        return self.environment_spec is not None


@dataclass(frozen=True)
class EnvironmentValidationResult:
    """Strict local validation result for one installed environment."""

    status: EnvironmentValidationStatus
    reasons: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        """Return whether the environment exactly matches its declaration."""
        return self.status == "installed"


def _selection_error(
    code: SelectionErrorCode,
    reason: str,
) -> EnvironmentSelection:
    return EnvironmentSelection(
        environment_spec=None,
        lock=None,
        error_code=code,
        reason=reason,
    )


def _matching_entry(
    manifest: LockManifest,
    *,
    python_abi: str,
    platform_tag: str,
    condition_digest: str,
) -> LockManifestEntry | None:
    matches = tuple(
        entry
        for entry in manifest.locks
        if entry.key == (python_abi, platform_tag, condition_digest)
    )
    if len(matches) > 1:
        raise ArtifactValidationError(
            "Multiple release locks match one environment target",
        )
    return matches[0] if matches else None


def _lock_matches_selection(
    lock: LockFile,
    descriptor: ChannelDescriptor,
    entry: LockManifestEntry,
    *,
    python_abi: str,
    platform_tag: str,
    condition_digest: str,
) -> bool:
    """Return whether a parsed lock exactly matches its selected target."""
    actual = (
        lock.channel_key,
        lock.python_abi,
        lock.platform_tag,
        lock.condition_set_sha256,
        lock.direct_requirements,
        lock.sha256(),
    )
    expected = (
        descriptor.channel_key,
        python_abi,
        platform_tag,
        condition_digest,
        descriptor.isolated_requirements,
        entry.lock_sha256,
    )
    return actual == expected


def select_environment_spec(
    *,
    descriptor: ChannelDescriptor,
    effective_config: Mapping[str, object],
    manifest: LockManifest,
    code_root: Path,
    python_abi: str,
    platform_tag: str,
    allowed_platform_tags: Collection[str],
) -> EnvironmentSelection:
    """Select and validate one exact lock for a target and config."""
    try:
        condition_set = descriptor.condition_set(effective_config)
    except DescriptorValidationError as exc:
        return _selection_error("config_invalid", str(exc))
    try:
        validate_python_abi(python_abi)
        validate_platform_tag(
            platform_tag,
            allowed_platform_tags=allowed_platform_tags,
        )
    except DescriptorValidationError as exc:
        return _selection_error("unsupported_platform", str(exc))
    if python_abi not in descriptor.supported_python_abis:
        return _selection_error(
            "unsupported_platform",
            "Python ABI is not supported by the descriptor",
        )
    if platform_tag not in descriptor.supported_platform_tags:
        return _selection_error(
            "unsupported_platform",
            "Platform tag is not supported by the descriptor",
        )
    manifest.validate_for_descriptor(descriptor)
    condition_digest = condition_set_sha256(condition_set)
    entry = _matching_entry(
        manifest,
        python_abi=python_abi,
        platform_tag=platform_tag,
        condition_digest=condition_digest,
    )
    if entry is None:
        return _selection_error(
            "unsupported_platform",
            "Release manifest has no exact lock for the target",
        )
    lock_path = Path(code_root).resolve() / Path(entry.lock_path)
    if not lock_path.is_file():
        raise ArtifactValidationError("Selected release lock is missing")
    if sha256_file(lock_path) != entry.lock_sha256:
        raise ArtifactValidationError(
            "Selected release lock digest mismatches",
        )
    lock = LockFile.from_mapping(
        read_json(lock_path),
        allowed_platform_tags=allowed_platform_tags,
    )
    if not _lock_matches_selection(
        lock,
        descriptor,
        entry,
        python_abi=python_abi,
        platform_tag=platform_tag,
        condition_digest=condition_digest,
    ):
        raise ArtifactValidationError(
            "Selected release lock does not match its declaration",
        )
    identity = EnvironmentSpecIdentity.create(
        channel_key=descriptor.channel_key,
        lock_sha256=entry.lock_sha256,
        python_abi=python_abi,
        platform_tag=platform_tag,
        condition_set=condition_set,
        allowed_platform_tags=allowed_platform_tags,
    )
    return EnvironmentSelection(
        environment_spec=EnvironmentSpecManifest.from_identity(identity),
        lock=lock,
    )


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _resolve_parent(path: Path) -> Path:
    """Resolve parent aliases while preserving the final path component."""
    absolute = Path(os.path.abspath(path))
    return absolute.parent.resolve() / absolute.name


def _lexically_inside(path: Path, root: Path) -> bool:
    """Check a path location without following its final symlink target."""
    try:
        _resolve_parent(path).relative_to(root.resolve())
    except ValueError:
        return False
    return True


def compute_venv_tree_sha256(venv_root: Path) -> str:
    """Hash the complete lexical contents of one immutable venv."""
    root = Path(venv_root).resolve()
    if not root.is_dir():
        raise EnvironmentManifestError("Environment venv directory is missing")
    entries: list[dict[str, object]] = []
    try:
        paths = sorted(
            root.rglob("*"),
            key=lambda path: path.relative_to(root).as_posix(),
        )
        for path in paths:
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                entries.append(
                    {
                        "kind": "symlink",
                        "path": relative,
                        "target": os.readlink(path),
                    },
                )
            elif path.is_file():
                with path.open("rb") as file:
                    file_digest = hashlib.file_digest(
                        file,
                        "sha256",
                    ).hexdigest()
                entries.append(
                    {
                        "kind": "file",
                        "path": relative,
                        "sha256": file_digest,
                        "size": path.stat().st_size,
                    },
                )
            elif not path.is_dir():
                raise EnvironmentManifestError(
                    f"Environment venv contains a special file: {relative}",
                )
    except (OSError, ValueError) as exc:
        raise EnvironmentManifestError(
            f"Environment venv tree cannot be read: {exc}",
        ) from exc
    return domain_sha256(_VENV_TREE_DOMAIN, entries)


def _run_probe(
    interpreter: Path,
    *,
    arguments: tuple[str, ...],
) -> Mapping[str, object]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in _PROBE_ENVIRONMENT_KEYS
    }
    result = subprocess.run(
        [str(interpreter), *arguments],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise EnvironmentManifestError(
            f"Environment interpreter probe failed: {result.stderr.strip()}",
        )
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise EnvironmentManifestError(
            "Environment interpreter probe returned invalid JSON",
        ) from exc
    return _mapping(value)


def _read_no_site_probe(
    interpreter: Path,
    venv_root: Path,
) -> Mapping[str, object]:
    """Read target facts without initializing its dependency environment."""
    return _run_probe(
        interpreter,
        arguments=(
            "-I",
            "-S",
            "-B",
            "-c",
            _NO_SITE_PROBE_SCRIPT,
            str(venv_root),
            _PACKAGING_ROOT,
        ),
    )


def _read_site_probe(interpreter: Path) -> Mapping[str, object]:
    """Inspect site state after the environment has passed integrity checks."""
    return _run_probe(
        interpreter,
        arguments=("-I", "-B", "-c", _SITE_PROBE_SCRIPT),
    )


def _expected_packages(
    lock: LockFile,
    marker_environment: Mapping[str, str],
) -> dict[str, LockPackage]:
    expected: dict[str, LockPackage] = {}
    for package in lock.packages:
        if package.marker is not None and not Marker(package.marker).evaluate(
            environment=marker_environment,
        ):
            continue
        expected[package.name] = package
    return expected


def _direct_urls(lock: LockFile) -> dict[str, str]:
    direct_urls: dict[str, str] = {}
    for text in lock.direct_requirements:
        requirement = Requirement(text)
        if requirement.url is not None:
            direct_urls[canonicalize_name(requirement.name)] = requirement.url
    return direct_urls


def _record_file_is_unhashed(relative: str) -> bool:
    path = PurePosixPath(relative)
    return path.suffix == ".pyc" or path.name in {
        "RECORD",
        "RECORD.jws",
        "RECORD.p7s",
    }


def _resolve_record_path(
    distribution: metadata.Distribution,
    package_path: metadata.PackagePath,
    environment_root: Path,
) -> tuple[Path | None, str | None]:
    """Resolve one safe RECORD path inside the dependency environment."""
    relative = str(package_path)
    if not relative or "\\" in relative:
        return None, f"RECORD path is invalid: {relative}"
    try:
        located = Path(distribution.locate_file(package_path))
        lexical = _resolve_parent(located)
        resolved = located.resolve()
    except (OSError, ValueError):
        return None, f"RECORD path cannot be resolved: {relative}"
    if not _inside(resolved, environment_root):
        return None, f"RECORD path escapes environment: {relative}"
    if not lexical.is_file():
        return None, f"RECORD file is missing: {relative}"
    return lexical, None


def _verify_record_integrity(
    package_path: metadata.PackagePath,
    resolved: Path,
) -> str | None:
    """Verify the size and hash declared by one resolved RECORD row."""
    relative = str(package_path)
    file_hash = package_path.hash
    file_size = package_path.size
    error: str | None = None
    if (file_hash is None) != (file_size is None):
        error = f"RECORD entry has incomplete integrity data: {relative}"
    elif file_hash is None:
        if not _record_file_is_unhashed(relative):
            error = f"RECORD entry lacks integrity data: {relative}"
    else:
        try:
            if resolved.stat().st_size != file_size:
                error = f"RECORD size mismatches: {relative}"
            else:
                digest = hashlib.new(
                    file_hash.mode,
                    resolved.read_bytes(),
                ).digest()
                encoded = base64.urlsafe_b64encode(digest)
                encoded = encoded.rstrip(b"=").decode()
                if encoded != file_hash.value:
                    error = f"RECORD hash mismatches: {relative}"
        except OSError:
            error = f"RECORD file cannot be read: {relative}"
        except ValueError:
            error = f"RECORD hash algorithm is unsupported: {relative}"
    return error


def _verify_record_entry(
    distribution: metadata.Distribution,
    package_path: metadata.PackagePath,
    environment_root: Path,
) -> tuple[tuple[str, ...], bool, Path | None]:
    """Validate one RECORD row and report whether it is RECORD itself."""
    relative = str(package_path)
    record_entry = PurePosixPath(relative).name == "RECORD"
    resolved, path_error = _resolve_record_path(
        distribution,
        package_path,
        environment_root,
    )
    if resolved is None:
        return (path_error or "RECORD path is invalid",), record_entry, None
    integrity_error = _verify_record_integrity(package_path, resolved)
    reasons = () if integrity_error is None else (integrity_error,)
    return reasons, record_entry, resolved


def _verify_record(
    distribution: metadata.Distribution,
    environment_root: Path,
) -> tuple[tuple[str, ...], frozenset[Path]]:
    reasons: list[str] = []
    try:
        files = distribution.files
    except Exception as exc:
        return (f"Distribution RECORD cannot be parsed: {exc}",), frozenset()
    if files is None:
        return ("Distribution RECORD is missing",), frozenset()
    seen: set[str] = set()
    recorded_paths: set[Path] = set()
    record_seen = False
    for package_path in files:
        try:
            relative = str(package_path)
            if relative in seen:
                reasons.append(f"RECORD path is duplicated: {relative}")
                continue
            seen.add(relative)
            entry_reasons, is_record, recorded_path = _verify_record_entry(
                distribution,
                package_path,
                environment_root,
            )
            reasons.extend(entry_reasons)
            record_seen = record_seen or is_record
            if recorded_path is not None:
                recorded_paths.add(recorded_path)
        except Exception as exc:
            reasons.append(f"Distribution RECORD cannot be parsed: {exc}")
    if not record_seen:
        reasons.append("Distribution RECORD does not list itself")
    return tuple(reasons), frozenset(recorded_paths)


def _actual_site_files(
    site_paths: tuple[Path, ...],
) -> tuple[frozenset[Path], tuple[str, ...]]:
    """Collect every executable or metadata-bearing site file path."""
    files: set[Path] = set()
    try:
        for site_path in site_paths:
            for path in site_path.rglob("*"):
                if path.is_symlink() or path.is_file():
                    files.add(_resolve_parent(path))
    except (OSError, ValueError) as exc:
        return frozenset(files), (
            f"Site-packages file inventory cannot be read: {exc}",
        )
    return frozenset(files), ()


def _validate_site_file_inventory(
    *,
    site_paths: tuple[Path, ...],
    recorded_paths: set[Path],
) -> tuple[str, ...]:
    """Require actual site files to be owned by locked RECORD entries."""
    actual_paths, scan_reasons = _actual_site_files(site_paths)
    expected_paths = frozenset(
        path
        for path in recorded_paths
        if any(_lexically_inside(path, root) for root in site_paths)
    )
    reasons = list(scan_reasons)
    unrecorded = actual_paths - expected_paths
    missing = expected_paths - actual_paths
    if unrecorded or missing:
        reasons.append(
            f"Site-packages files do not match locked RECORDs: "
            f"unrecorded={len(unrecorded)}, missing={len(missing)}",
        )
    return tuple(reasons)


def _distribution_inventory(
    site_paths: tuple[Path, ...],
) -> tuple[dict[str, metadata.Distribution], tuple[str, ...]]:
    inventory: dict[str, metadata.Distribution] = {}
    reasons: list[str] = []
    try:
        distributions = metadata.distributions(
            path=[str(path) for path in site_paths],
        )
        for distribution in distributions:
            try:
                raw_name = distribution.metadata.get("Name")
            except Exception as exc:
                reasons.append(
                    f"Installed distribution metadata cannot be parsed: {exc}",
                )
                continue
            if not raw_name:
                reasons.append("Installed distribution has no canonical name")
                continue
            name = canonicalize_name(raw_name)
            if name in inventory:
                reasons.append(f"Installed distribution is duplicated: {name}")
                continue
            inventory[name] = distribution
    except Exception as exc:
        reasons.append(
            f"Installed distribution inventory cannot be read: {exc}",
        )
    return inventory, tuple(reasons)


def _read_direct_url(
    distribution: metadata.Distribution,
) -> str | None:
    try:
        value = distribution.read_text("direct_url.json")
    except (OSError, UnicodeError) as exc:
        raise EnvironmentManifestError(
            "Installed direct_url.json cannot be read",
        ) from exc
    if value is None:
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise EnvironmentManifestError(
            "Installed direct_url.json is invalid",
        ) from exc
    mapping = _mapping(parsed)
    url = mapping.get("url")
    if not isinstance(url, str) or not url:
        raise EnvironmentManifestError(
            "Installed direct_url.json has no URL",
        )
    return url


def _validate_target_probe(
    *,
    probe: Mapping[str, object],
    interpreter: Path,
    venv_root: Path,
    expected_python_abi: str,
    expected_platform_tag: str,
) -> tuple[tuple[Path, ...], Mapping[str, str], tuple[str, ...]]:
    reasons: list[str] = []
    flags = probe.get("flags")
    if not isinstance(flags, Mapping) or any(
        flags.get(name) is not True
        for name in (
            "isolated",
            "ignore_environment",
            "no_site",
            "no_user_site",
        )
    ):
        reasons.append(
            "Target probe did not run without site in isolated mode",
        )
    if probe.get("pythonpath") is not None:
        reasons.append("Interpreter inherited PYTHONPATH")
    if Path(str(probe.get("executable"))).resolve() != interpreter.resolve():
        reasons.append("Interpreter path does not match installation")
    if probe.get("abi") != expected_python_abi:
        reasons.append(
            "Interpreter Python ABI does not match environment spec",
        )
    compatible_tags = probe.get("compatible_platform_tags")
    if not isinstance(compatible_tags, list) or any(
        not isinstance(value, str) for value in compatible_tags
    ):
        reasons.append("Interpreter compatible platform tags are invalid")
    elif expected_platform_tag not in compatible_tags:
        reasons.append(
            "Interpreter platform does not match environment spec",
        )
    raw_purelib = probe.get("purelib")
    raw_platlib = probe.get("platlib")
    site_paths: tuple[Path, ...] = ()
    if (
        isinstance(raw_purelib, str)
        and raw_purelib
        and isinstance(raw_platlib, str)
        and raw_platlib
    ):
        site_paths = tuple(
            sorted(
                {
                    Path(raw_purelib).resolve(),
                    Path(raw_platlib).resolve(),
                },
            ),
        )
        if any(not _inside(path, venv_root) for path in site_paths):
            reasons.append("Target site-packages path escapes the venv")
        if any(not path.is_dir() for path in site_paths):
            reasons.append("Target site-packages directory is missing")
    else:
        reasons.append("Target site-packages paths are invalid")
    marker_value = probe.get("marker_environment")
    marker_environment: Mapping[str, str] = {}
    if isinstance(marker_value, Mapping) and all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in marker_value.items()
    ):
        marker_environment = marker_value
    else:
        reasons.append("Interpreter marker environment is invalid")
    return site_paths, marker_environment, tuple(reasons)


def _validate_site_isolation(
    *,
    probe: Mapping[str, object],
    interpreter: Path,
    venv_root: Path,
    expected_site_paths: tuple[Path, ...],
) -> tuple[str, ...]:
    reasons: list[str] = []
    flags = probe.get("flags")
    if not isinstance(flags, Mapping) or any(
        flags.get(name) is not True
        for name in ("isolated", "ignore_environment", "no_user_site")
    ):
        reasons.append("Site probe did not run in isolated mode")
    if probe.get("pythonpath") is not None:
        reasons.append("Interpreter inherited PYTHONPATH")
    if Path(str(probe.get("executable"))).resolve() != interpreter.resolve():
        reasons.append("Interpreter path does not match installation")
    if Path(str(probe.get("prefix"))).resolve() != venv_root.resolve():
        reasons.append("Interpreter prefix does not match venv root")
    site_values = {str(probe.get("purelib")), str(probe.get("platlib"))}
    raw_site_packages = probe.get("site_packages")
    if isinstance(raw_site_packages, list) and all(
        isinstance(value, str) for value in raw_site_packages
    ):
        site_values.update(raw_site_packages)
    else:
        reasons.append("Interpreter site-packages paths are invalid")
    site_paths = tuple(
        sorted({Path(value).resolve() for value in site_values}),
    )
    if set(site_paths) != set(expected_site_paths):
        reasons.append("Site paths do not match the no-site target probe")
    if any(not _inside(path, venv_root) for path in site_paths):
        reasons.append("Interpreter exposes site-packages outside the venv")
    user_site = Path(str(probe.get("user_site"))).resolve()
    raw_sys_path = probe.get("sys_path")
    sys_paths = (
        tuple(Path(str(value)).resolve() for value in raw_sys_path if value)
        if isinstance(raw_sys_path, list)
        else ()
    )
    if user_site in sys_paths:
        reasons.append("Interpreter exposes the user site")
    for path in sys_paths:
        if path.name in {"site-packages", "dist-packages"} and not _inside(
            path,
            venv_root,
        ):
            reasons.append("Interpreter inherited an ambient site-packages")
    return tuple(reasons)


def _validate_pyvenv_config(venv_root: Path) -> tuple[str, ...]:
    config_path = venv_root / "pyvenv.cfg"
    if not config_path.is_file():
        return ("pyvenv.cfg is missing",)
    try:
        lines = config_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        return (f"pyvenv.cfg cannot be read: {exc}",)
    values: dict[str, str] = {}
    for line in lines:
        key, separator, value = line.partition("=")
        if separator:
            values[key.strip().lower()] = value.strip().lower()
    if values.get("include-system-site-packages") != "false":
        return ("venv inherits system site-packages",)
    return ()


def _load_environment_manifests(
    *,
    environment_directory: Path,
    expected_spec: EnvironmentSpecManifest,
    expected_environment_id: str,
    expected_lock: LockFile,
    allowed_platform_tags: Collection[str],
    allow_staging_directory: bool = False,
) -> tuple[InstallManifest | None, tuple[str, ...]]:
    reasons: list[str] = []
    spec_directory = environment_directory.parent
    try:
        if not allow_staging_directory:
            DirectoryIdentity.validate(
                logical_id=expected_spec.environment_spec_id,
                directory_key=spec_directory.name,
                manifest_logical_id=expected_spec.environment_spec_id,
            )
        DirectoryIdentity.validate(
            logical_id=expected_environment_id,
            directory_key=environment_directory.name,
            manifest_logical_id=expected_environment_id,
        )
    except DescriptorValidationError as exc:
        reasons.append(str(exc))
    try:
        spec_manifest = EnvironmentSpecManifest.from_mapping(
            read_json(spec_directory / "environment_spec.json"),
            allowed_platform_tags=allowed_platform_tags,
        )
        if spec_manifest != expected_spec:
            reasons.append("Environment spec manifest mismatches selection")
    except (
        ArtifactValidationError,
        DescriptorValidationError,
        EnvironmentManifestError,
        OSError,
    ) as exc:
        reasons.append(f"Environment spec manifest is invalid: {exc}")
    dependency_lock = environment_directory / "dependency.lock"
    try:
        if sha256_file(dependency_lock) != expected_spec.lock_sha256:
            reasons.append("Installed dependency lock digest mismatches")
        persisted_lock = LockFile.from_mapping(
            read_json(dependency_lock),
            allowed_platform_tags=allowed_platform_tags,
        )
        if persisted_lock != expected_lock:
            reasons.append("Installed dependency lock content mismatches")
    except (
        ArtifactValidationError,
        DescriptorValidationError,
        OSError,
    ) as exc:
        reasons.append(f"Installed dependency lock is invalid: {exc}")
    install_manifest: InstallManifest | None = None
    try:
        install_manifest = InstallManifest.from_mapping(
            read_json(environment_directory / "install.json"),
            allowed_platform_tags=allowed_platform_tags,
        )
        if (
            install_manifest.environment_id != expected_environment_id
            or install_manifest.environment_spec_id
            != expected_spec.environment_spec_id
            or install_manifest.python_abi != expected_spec.python_abi
            or install_manifest.platform_tag != expected_spec.platform_tag
            or install_manifest.lock_sha256 != expected_spec.lock_sha256
        ):
            reasons.append("Install manifest mismatches environment spec")
    except (
        ArtifactValidationError,
        DescriptorValidationError,
        EnvironmentManifestError,
        OSError,
    ) as exc:
        reasons.append(f"Install manifest is invalid: {exc}")
    return install_manifest, tuple(reasons)


def _repair_required(reasons: list[str]) -> EnvironmentValidationResult:
    """Return the only valid outcome for an installed environment mismatch."""
    return EnvironmentValidationResult(
        status="repair_required",
        reasons=tuple(reasons),
    )


def _validate_expected_identity(
    environment_id: str,
    environment_spec_id: str,
) -> tuple[str, ...]:
    """Validate the caller's selected installation identity."""
    try:
        identity = EnvironmentIdentity.parse(environment_id)
    except DescriptorValidationError as exc:
        return (str(exc),)
    if identity.environment_spec_id != environment_spec_id:
        return ("Environment ID does not match selected spec",)
    return ()


def _probe_target(
    *,
    interpreter: Path,
    venv_root: Path,
    expected_python_abi: str,
    expected_platform_tag: str,
) -> tuple[tuple[Path, ...], Mapping[str, str], tuple[str, ...]]:
    """Attest target facts without loading its dependency environment."""
    try:
        probe = _read_no_site_probe(interpreter, venv_root)
        return _validate_target_probe(
            probe=probe,
            interpreter=interpreter,
            venv_root=venv_root,
            expected_python_abi=expected_python_abi,
            expected_platform_tag=expected_platform_tag,
        )
    except (
        EnvironmentManifestError,
        OSError,
        subprocess.SubprocessError,
    ) as exc:
        return (), {}, (f"Target interpreter probe failed: {exc}",)


def _probe_site_isolation(
    *,
    interpreter: Path,
    venv_root: Path,
    expected_site_paths: tuple[Path, ...],
) -> tuple[str, ...]:
    """Load site only after every persisted environment file is trusted."""
    try:
        probe = _read_site_probe(interpreter)
        return _validate_site_isolation(
            probe=probe,
            interpreter=interpreter,
            venv_root=venv_root,
            expected_site_paths=expected_site_paths,
        )
    except (
        EnvironmentManifestError,
        OSError,
        subprocess.SubprocessError,
    ) as exc:
        return (f"Environment site isolation probe failed: {exc}",)


def _validate_venv_tree(
    venv_root: Path,
    expected_digest: str,
) -> tuple[str, ...]:
    """Compare current venv bytes to the immutable installation snapshot."""
    try:
        actual_digest = compute_venv_tree_sha256(venv_root)
    except EnvironmentManifestError as exc:
        return (str(exc),)
    if actual_digest != expected_digest:
        return ("Environment venv tree digest mismatches install manifest",)
    return ()


def _venv_interpreter(venv_root: Path) -> Path:
    """Return the canonical interpreter path for one venv root."""
    if os.name == "nt":
        return venv_root / "Scripts" / "python.exe"
    return venv_root / "bin" / "python"


def _launcher_shebang(content: bytes) -> str | None:
    """Decode one launcher shebang line from common script encodings."""
    first_line = content.split(b"\n", 1)[0].rstrip(b"\r")
    if first_line.startswith(b"\xff\xfe"):
        line = first_line.decode("utf-16").strip()
    elif first_line.startswith(b"#\x00!\x00"):
        line = first_line.decode("utf-16-le").strip()
    elif first_line.startswith(b"#!"):
        line = first_line[2:].decode("utf-8").strip()
    else:
        return None
    return line[2:].strip() if line.startswith("#!") else line


def _launcher_python_path(path: Path) -> str | None:
    """Return a Python interpreter token from a launcher shebang."""
    try:
        content = path.read_bytes()
        line = _launcher_shebang(content)
    except (OSError, UnicodeError):
        return None
    if line is None:
        return None
    tokens = line.split()
    if not tokens:
        return None
    command = tokens[0].strip('"')
    basename = command.replace("\\", "/").rsplit("/", 1)[-1]
    interpreter_path = None
    if basename.lower() == "env":
        interpreter_path = next(
            (
                candidate.strip('"')
                for token in tokens[1:]
                if not token.startswith("-")
                for candidate in (token,)
                if candidate.replace("\\", "/")
                .rsplit("/", 1)[-1]
                .lower()
                .startswith("python")
            ),
            None,
        )
    elif basename.lower().startswith("python"):
        interpreter_path = command
    elif basename.lower() in {"sh", "bash", "zsh"}:
        match = re.search(
            rb"[\"']([^\"'\r\n]*python[^\"'\r\n]*)[\"']",
            content[:1024],
            flags=re.IGNORECASE,
        )
        if match is not None:
            interpreter_path = match.group(1).decode("utf-8")
    return interpreter_path


def _is_distlib_windows_launcher(content: bytes) -> bool:
    """Identify a Windows executable containing a distlib script archive."""
    if not content.startswith(b"MZ") or b"#!" not in content:
        return False
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            return "__main__.py" in archive.namelist()
    except (OSError, zipfile.BadZipFile):
        return False


def _windows_launcher_validation_reason(
    path: Path,
    allowed_paths: Collection[str],
) -> str | None:
    """Return a validation error for one embedded Windows launcher."""
    try:
        content = path.read_bytes()
    except OSError as exc:
        return f"Python launcher cannot be read: {path.name}: {exc}"
    if not _is_distlib_windows_launcher(content):
        return None
    encoded_paths: list[bytes] = []
    for allowed in allowed_paths:
        values = (allowed, allowed.replace(os.sep, "/"))
        encoded_paths.extend(
            encoded
            for value in values
            for encoded in (
                value.encode("utf-8"),
                value.encode("utf-16-le"),
                value.encode("utf-16-be"),
            )
        )
    content_lower = content.lower()
    if any(encoded.lower() in content_lower for encoded in encoded_paths):
        return None
    return (
        "Python launcher does not reference the canonical venv "
        f"interpreter: {path.name}"
    )


def _normalise_launcher_path(value: str) -> str:
    """Normalize one launcher path using the host platform's path rules."""
    return os.path.normcase(os.path.normpath(value.strip().strip('"')))


def _validate_venv_launchers(
    venv_root: Path,
    expected_interpreter: Path,
) -> tuple[str, ...]:
    """Ensure Python launchers point at the published venv interpreter."""
    scripts_root = venv_root / ("Scripts" if os.name == "nt" else "bin")
    if not scripts_root.is_dir():
        return ("Venv scripts directory is missing",)
    expected_path = Path(expected_interpreter)
    allowed_paths = {
        _normalise_launcher_path(str(expected_path)),
    }
    try:
        allowed_paths.update(
            _normalise_launcher_path(
                str(expected_path.parent / candidate.name),
            )
            for candidate in scripts_root.iterdir()
            if candidate.name.lower().startswith("python")
            and (candidate.is_file() or candidate.is_symlink())
        )
    except OSError as exc:
        return (f"Venv launchers cannot be enumerated: {exc}",)
    reasons: list[str] = []
    try:
        paths = sorted(scripts_root.rglob("*"))
    except OSError as exc:
        return (f"Venv launchers cannot be enumerated: {exc}",)
    for path in paths:
        if path.is_symlink() or not path.is_file():
            continue
        launcher = _launcher_python_path(path)
        if launcher is not None:
            if _normalise_launcher_path(launcher) not in allowed_paths:
                reasons.append(
                    "Python launcher does not reference the canonical venv "
                    f"interpreter: {path.name}",
                )
            continue
        if os.name != "nt" or path.suffix.lower() != ".exe":
            continue
        if path.name.lower().startswith(("python", "venvlauncher")):
            continue
        reason = _windows_launcher_validation_reason(path, allowed_paths)
        if reason is not None:
            reasons.append(reason)
    return tuple(reasons)


def _validate_provenance(
    install_manifest: InstallManifest,
    expected_packages: Mapping[str, LockPackage],
) -> tuple[str, ...]:
    """Match installed wheel provenance to an active lock inventory."""
    reasons: list[str] = []
    provenance = {item.name: item for item in install_manifest.packages}
    if set(provenance) != set(expected_packages):
        reasons.append("Wheel provenance inventory mismatches dependency lock")
    for name, package in expected_packages.items():
        item = provenance.get(name)
        if item is None:
            continue
        wheel_candidates = {
            (wheel.filename, wheel.sha256) for wheel in package.wheels
        }
        selected_wheel = (item.wheel_filename, item.wheel_sha256)
        if (
            item.version != package.version
            or selected_wheel not in wheel_candidates
        ):
            reasons.append(f"Wheel provenance mismatches: {name}")
    return tuple(reasons)


def _validate_distributions(
    *,
    expected_packages: Mapping[str, LockPackage],
    expected_lock: LockFile,
    site_paths: tuple[Path, ...],
    venv_root: Path,
) -> tuple[str, ...]:
    """Validate actual distributions, direct URLs, and RECORD contents."""
    reasons: list[str] = []
    recorded_paths: set[Path] = set()
    inventory, inventory_reasons = _distribution_inventory(site_paths)
    reasons.extend(inventory_reasons)
    if set(inventory) != set(expected_packages):
        reasons.append("Installed distribution inventory mismatches lock")
    direct_urls = _direct_urls(expected_lock)
    for name, package in expected_packages.items():
        distribution = inventory.get(name)
        if distribution is None:
            continue
        try:
            installed_version = distribution.version
        except Exception as exc:
            reasons.append(
                f"Installed distribution metadata is invalid: {name}: {exc}",
            )
            continue
        if installed_version != package.version:
            reasons.append(
                f"Installed distribution version mismatches: {name}",
            )
        expected_url = direct_urls.get(name)
        if expected_url is not None:
            try:
                if _read_direct_url(distribution) != expected_url:
                    reasons.append(f"Installed direct URL mismatches: {name}")
            except EnvironmentManifestError as exc:
                reasons.append(
                    f"Installed direct URL is invalid: {name}: {exc}",
                )
        record_reasons, package_paths = _verify_record(
            distribution,
            venv_root,
        )
        reasons.extend(f"{name}: {reason}" for reason in record_reasons)
        recorded_paths.update(package_paths)
    reasons.extend(
        _validate_site_file_inventory(
            site_paths=site_paths,
            recorded_paths=recorded_paths,
        ),
    )
    return tuple(reasons)


def _validate_dependency_state(
    *,
    install_manifest: InstallManifest,
    expected_lock: LockFile,
    marker_environment: Mapping[str, str],
    site_paths: tuple[Path, ...],
    venv_root: Path,
) -> tuple[str, ...]:
    """Validate every active locked package against installed state."""
    expected_packages = _expected_packages(
        expected_lock,
        marker_environment,
    )
    provenance_reasons = _validate_provenance(
        install_manifest,
        expected_packages,
    )
    distribution_reasons = _validate_distributions(
        expected_packages=expected_packages,
        expected_lock=expected_lock,
        site_paths=site_paths,
        venv_root=venv_root,
    )
    return provenance_reasons + distribution_reasons


def validate_installed_environment(
    *,
    environment_directory: Path,
    interpreter: Path,
    expected_spec: EnvironmentSpecManifest,
    expected_environment_id: str,
    expected_lock: LockFile,
    allowed_platform_tags: Collection[str],
    allow_staging_directory: bool = False,
    expected_launcher_interpreter: Path | None = None,
) -> EnvironmentValidationResult:
    """Strictly validate one immutable dependency environment."""
    environment_directory = Path(environment_directory).resolve()
    interpreter = _resolve_parent(Path(interpreter))
    reasons = list(
        _validate_expected_identity(
            expected_environment_id,
            expected_spec.environment_spec_id,
        ),
    )
    install_manifest, manifest_reasons = _load_environment_manifests(
        environment_directory=environment_directory,
        expected_spec=expected_spec,
        expected_environment_id=expected_environment_id,
        expected_lock=expected_lock,
        allowed_platform_tags=allowed_platform_tags,
        allow_staging_directory=allow_staging_directory,
    )
    reasons.extend(manifest_reasons)
    venv_root = environment_directory / "venv"
    if (
        not _lexically_inside(interpreter, venv_root)
        or not interpreter.is_file()
    ):
        reasons.append("Interpreter is not inside the environment venv")
        return _repair_required(reasons)
    reasons.extend(_validate_pyvenv_config(venv_root))
    if install_manifest is None or reasons:
        return _repair_required(reasons)
    reasons.extend(
        _validate_venv_tree(
            venv_root,
            install_manifest.venv_tree_sha256,
        ),
    )
    if not reasons:
        launcher_interpreter = (
            expected_launcher_interpreter or _venv_interpreter(venv_root)
        )
        reasons.extend(
            _validate_venv_launchers(
                venv_root,
                launcher_interpreter,
            ),
        )
    if reasons:
        return _repair_required(reasons)
    site_paths, marker_environment, target_reasons = _probe_target(
        interpreter=interpreter,
        venv_root=venv_root,
        expected_python_abi=expected_spec.python_abi,
        expected_platform_tag=expected_spec.platform_tag,
    )
    reasons.extend(target_reasons)
    if reasons or not site_paths or not marker_environment:
        return _repair_required(reasons)
    reasons.extend(
        _validate_dependency_state(
            install_manifest=install_manifest,
            expected_lock=expected_lock,
            marker_environment=marker_environment,
            site_paths=site_paths,
            venv_root=venv_root,
        ),
    )
    if reasons:
        return _repair_required(reasons)
    reasons.extend(
        _probe_site_isolation(
            interpreter=interpreter,
            venv_root=venv_root,
            expected_site_paths=site_paths,
        ),
    )
    return (
        _repair_required(reasons)
        if reasons
        else EnvironmentValidationResult(status="installed")
    )
