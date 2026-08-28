# -*- coding: utf-8 -*-
"""Pure models and reproducible builders for Channel release artifacts."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
import hashlib
import itertools
import json
from pathlib import Path, PurePosixPath
import re
import zipfile

from packaging.requirements import Requirement
from packaging.markers import InvalidMarker, Marker
from packaging.utils import InvalidName, canonicalize_name
from packaging.version import InvalidVersion, Version

from .canonical import canonical_json, normalize_string
from .descriptor import ChannelDescriptor
from .errors import ArtifactValidationError
from .identifiers import (
    validate_channel_key,
    validate_platform_tag,
    validate_python_abi,
)
from .requirements import canonicalize_requirements


_HEX_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_WHEEL_NAME = re.compile(r"^[^/\\]+\.whl$")
_VERSION_RANGE_FIELDS = frozenset({"min", "max"})
_SOURCE_FIELDS = frozenset({"kind", "url"})
_RECORD_FIELDS = frozenset(
    {
        "schema_version",
        "channel_key",
        "source_kind",
        "release_version",
        "qwenpaw_compatibility",
        "protocol_compatibility",
        "download_url",
        "artifact_sha256",
    },
)
_LOCAL_MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "channel_key",
        "source_kind",
        "release_version",
        "qwenpaw_compatibility",
        "protocol_compatibility",
        "source",
        "artifact_sha256",
        "source_revision",
        "descriptor_sha256",
        "installed_at",
    },
)
_WHEEL_FIELDS = frozenset({"filename", "url", "sha256"})
_PACKAGE_FIELDS = frozenset(
    {"name", "version", "marker", "direct", "wheels"},
)
_LOCK_FIELDS = frozenset(
    {
        "schema_version",
        "channel_key",
        "python_abi",
        "platform_tag",
        "condition_set_sha256",
        "direct_requirements",
        "packages",
    },
)
_LOCK_ENTRY_FIELDS = frozenset(
    {
        "python_abi",
        "platform_tag",
        "condition_set_sha256",
        "lock_path",
        "lock_sha256",
    },
)
_LOCK_MANIFEST_FIELDS = frozenset(
    {"schema_version", "channel_key", "locks"},
)
_ARCHIVE_FORBIDDEN_PARTS = frozenset(
    {
        "artifact.json",
        "bootstrap",
        "venv",
        "site-packages",
    },
)


def _error(message: str, *path: str | int) -> ArtifactValidationError:
    return ArtifactValidationError(message, path=path)


def _mapping(value: object, *, path: tuple[str | int, ...] = ()) -> Mapping:
    if not isinstance(value, Mapping):
        raise _error("Expected an object", *path)
    if any(not isinstance(key, str) for key in value):
        raise _error("Object field names must be strings", *path)
    return value


def _closed(
    value: object,
    fields: frozenset[str],
    *,
    path: tuple[str | int, ...] = (),
) -> Mapping:
    mapping = _mapping(value, path=path)
    keys = set(mapping)
    if keys != fields:
        missing = sorted(fields - keys)
        unknown = sorted(keys - fields)
        raise _error(
            f"Object fields do not match v1 shape: "
            f"missing={missing}, unknown={unknown}",
            *path,
        )
    return mapping


def _string(
    value: object,
    name: str,
    *,
    path: tuple[str | int, ...] = (),
    nonempty: bool = True,
) -> str:
    if not isinstance(value, str):
        raise _error("Expected a string", *(path + (name,)))
    result = normalize_string(value)
    if nonempty and not result:
        raise _error("String must not be empty", *(path + (name,)))
    return result


def _integer(
    value: object,
    name: str,
    *,
    path: tuple[str | int, ...] = (),
    minimum: int | None = None,
) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise _error("Expected an integer", *(path + (name,)))
    if minimum is not None and value < minimum:
        raise _error("Integer is below the minimum", *(path + (name,)))
    return value


def _version(value: object, name: str, *, path: tuple[str | int, ...]) -> str:
    result = _string(value, name, path=path)
    try:
        return str(Version(result))
    except InvalidVersion as exc:
        raise _error("Version is invalid", *(path + (name,))) from exc


def _url(value: object, name: str, *, path: tuple[str | int, ...]) -> str:
    result = _string(value, name, path=path)
    if not result.startswith(("http://", "https://")):
        raise _error("URL must be absolute HTTP(S)", *(path + (name,)))
    if any(character.isspace() for character in result):
        raise _error("URL must not contain whitespace", *(path + (name,)))
    if "@" in result.split("//", 1)[-1].split("/", 1)[0]:
        raise _error("URL must not contain user info", *(path + (name,)))
    return result


def _digest(value: object, name: str, *, path: tuple[str | int, ...]) -> str:
    result = _string(value, name, path=path)
    if not _HEX_DIGEST.fullmatch(result):
        raise _error("Digest must be 64 lowercase hex characters", *path, name)
    return result


def _package_name(value: object, name: str) -> str:
    """Validate and return a PEP 503 canonical distribution name."""
    result = _string(value, name)
    try:
        canonical = canonicalize_name(result, validate=True)
    except InvalidName as exc:
        raise _error("Package name is invalid", name) from exc
    if canonical != result:
        raise _error("Package name must be canonical", name)
    return canonical


def _marker(value: object, name: str) -> str:
    """Validate one lock environment marker without changing its meaning."""
    result = _string(value, name, path=())
    try:
        Marker(result)
    except InvalidMarker as exc:
        raise _error("Environment marker is invalid", name) from exc
    return result


@dataclass(frozen=True)
class VersionRange:
    """Inclusive release compatibility range for one product version."""

    minimum: str
    maximum: str | None

    @classmethod
    def from_mapping(
        cls,
        value: object,
        *,
        path: tuple[str | int, ...],
    ) -> "VersionRange":
        mapping = _closed(value, _VERSION_RANGE_FIELDS, path=path)
        minimum = _version(mapping["min"], "min", path=path)
        maximum_raw = mapping["max"]
        maximum = (
            None
            if maximum_raw is None
            else _version(maximum_raw, "max", path=path)
        )
        if maximum is not None and Version(maximum) < Version(minimum):
            raise _error("Compatibility range is inverted", *path)
        return cls(minimum=minimum, maximum=maximum)

    def to_mapping(self) -> dict[str, str | None]:
        """Return the closed JSON representation."""
        return {"min": self.minimum, "max": self.maximum}


@dataclass(frozen=True)
class ProtocolRange:
    """Inclusive protocol version range for an artifact release."""

    minimum: int
    maximum: int

    @classmethod
    def from_mapping(
        cls,
        value: object,
        *,
        path: tuple[str | int, ...],
    ) -> "ProtocolRange":
        mapping = _closed(value, _VERSION_RANGE_FIELDS, path=path)
        minimum = _integer(mapping["min"], "min", path=path, minimum=1)
        maximum = _integer(mapping["max"], "max", path=path, minimum=1)
        if maximum < minimum:
            raise _error("Protocol range is inverted", *path)
        return cls(minimum=minimum, maximum=maximum)

    def to_mapping(self) -> dict[str, int]:
        """Return the closed JSON representation."""
        return {"min": self.minimum, "max": self.maximum}


@dataclass(frozen=True)
class ArtifactRecord:
    """Catalog release metadata for one official Channel artifact."""

    channel_key: str
    source_kind: str
    release_version: str
    qwenpaw_compatibility: VersionRange
    protocol_compatibility: ProtocolRange
    download_url: str
    artifact_sha256: str
    schema_version: int = 1

    @classmethod
    def from_mapping(cls, value: object) -> "ArtifactRecord":
        mapping = _closed(value, _RECORD_FIELDS)
        schema_version = _integer(mapping["schema_version"], "schema_version")
        if schema_version != 1:
            raise _error("schema_version must be integer 1", "schema_version")
        channel_key = _string(mapping["channel_key"], "channel_key")
        validate_channel_key(channel_key)
        source_kind = _string(mapping["source_kind"], "source_kind")
        if source_kind != "builtin":
            raise _error("Artifact records must describe builtin Channels")
        release_version = _version(
            mapping["release_version"],
            "release_version",
            path=(),
        )
        qwenpaw = VersionRange.from_mapping(
            mapping["qwenpaw_compatibility"],
            path=("qwenpaw_compatibility",),
        )
        protocol = ProtocolRange.from_mapping(
            mapping["protocol_compatibility"],
            path=("protocol_compatibility",),
        )
        download_url = _url(mapping["download_url"], "download_url", path=())
        artifact_sha256 = _digest(
            mapping["artifact_sha256"],
            "artifact_sha256",
            path=(),
        )
        return cls(
            channel_key=channel_key,
            source_kind=source_kind,
            release_version=release_version,
            qwenpaw_compatibility=qwenpaw,
            protocol_compatibility=protocol,
            download_url=download_url,
            artifact_sha256=artifact_sha256,
        )

    def to_mapping(self) -> dict[str, object]:
        """Return the closed JSON representation."""
        return {
            "schema_version": self.schema_version,
            "channel_key": self.channel_key,
            "source_kind": self.source_kind,
            "release_version": self.release_version,
            "qwenpaw_compatibility": self.qwenpaw_compatibility.to_mapping(),
            "protocol_compatibility": self.protocol_compatibility.to_mapping(),
            "download_url": self.download_url,
            "artifact_sha256": self.artifact_sha256,
        }


@dataclass(frozen=True)
class ArtifactManifest:
    """Immutable local metadata stored beside an installed code root."""

    channel_key: str
    source_kind: str
    release_version: str
    qwenpaw_compatibility: VersionRange
    protocol_compatibility: ProtocolRange
    source_kind_url: str
    artifact_sha256: str
    source_revision: str
    descriptor_sha256: str
    installed_at: str
    schema_version: int = 1

    @classmethod
    def from_mapping(cls, value: object) -> "ArtifactManifest":
        mapping = _closed(value, _LOCAL_MANIFEST_FIELDS)
        schema_version = _integer(mapping["schema_version"], "schema_version")
        if schema_version != 1:
            raise _error("schema_version must be integer 1", "schema_version")
        channel_key = _string(mapping["channel_key"], "channel_key")
        validate_channel_key(channel_key)
        source_kind = _string(mapping["source_kind"], "source_kind")
        if source_kind not in {"builtin", "plugin"}:
            raise _error("Source kind is invalid", "source_kind")
        release_version = _version(
            mapping["release_version"],
            "release_version",
            path=(),
        )
        qwenpaw = VersionRange.from_mapping(
            mapping["qwenpaw_compatibility"],
            path=("qwenpaw_compatibility",),
        )
        protocol = ProtocolRange.from_mapping(
            mapping["protocol_compatibility"],
            path=("protocol_compatibility",),
        )
        source = _closed(mapping["source"], _SOURCE_FIELDS, path=("source",))
        source_kind_url = _url(source["url"], "url", path=("source",))
        recorded_kind = _string(source["kind"], "kind", path=("source",))
        if recorded_kind != source_kind:
            raise _error(
                "Source kind does not match artifact",
                "source",
                "kind",
            )
        artifact_sha256 = _digest(
            mapping["artifact_sha256"],
            "artifact_sha256",
            path=(),
        )
        source_revision = _digest(
            mapping["source_revision"],
            "source_revision",
            path=(),
        )
        descriptor_sha256 = _digest(
            mapping["descriptor_sha256"],
            "descriptor_sha256",
            path=(),
        )
        installed_at = _string(mapping["installed_at"], "installed_at")
        return cls(
            channel_key=channel_key,
            source_kind=source_kind,
            release_version=release_version,
            qwenpaw_compatibility=qwenpaw,
            protocol_compatibility=protocol,
            source_kind_url=source_kind_url,
            artifact_sha256=artifact_sha256,
            source_revision=source_revision,
            descriptor_sha256=descriptor_sha256,
            installed_at=installed_at,
        )

    def to_mapping(self) -> dict[str, object]:
        """Return the closed JSON representation."""
        return {
            "schema_version": self.schema_version,
            "channel_key": self.channel_key,
            "source_kind": self.source_kind,
            "release_version": self.release_version,
            "qwenpaw_compatibility": self.qwenpaw_compatibility.to_mapping(),
            "protocol_compatibility": self.protocol_compatibility.to_mapping(),
            "source": {"kind": self.source_kind, "url": self.source_kind_url},
            "artifact_sha256": self.artifact_sha256,
            "source_revision": self.source_revision,
            "descriptor_sha256": self.descriptor_sha256,
            "installed_at": self.installed_at,
        }


@dataclass(frozen=True)
class WheelFile:
    """One hash-pinned wheel candidate; ``url=None`` supports offline cache."""

    filename: str
    url: str | None
    sha256: str

    @classmethod
    def from_mapping(cls, value: object) -> "WheelFile":
        mapping = _closed(value, _WHEEL_FIELDS)
        filename = _string(mapping["filename"], "filename")
        if not _WHEEL_NAME.fullmatch(filename):
            raise _error("Wheel filename is invalid", "filename")
        url_raw = mapping["url"]
        url = None if url_raw is None else _url(url_raw, "url", path=())
        sha256 = _digest(mapping["sha256"], "sha256", path=())
        return cls(filename=filename, url=url, sha256=sha256)

    def to_mapping(self) -> dict[str, str | None]:
        """Return the closed JSON representation."""
        return {
            "filename": self.filename,
            "url": self.url,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class LockPackage:
    """One exact distribution entry, including transitive dependencies."""

    name: str
    version: str
    marker: str | None
    direct: bool
    wheels: tuple[WheelFile, ...]

    @classmethod
    def from_mapping(cls, value: object) -> "LockPackage":
        mapping = _closed(value, _PACKAGE_FIELDS)
        name = _package_name(mapping["name"], "name")
        version = _version(mapping["version"], "version", path=())
        marker_raw = mapping["marker"]
        marker = None if marker_raw is None else _marker(marker_raw, "marker")
        direct = mapping["direct"]
        if not isinstance(direct, bool):
            raise _error("direct must be boolean", "direct")
        wheels_raw = mapping["wheels"]
        if not isinstance(wheels_raw, list) or not wheels_raw:
            raise _error("packages must contain at least one wheel", "wheels")
        wheels = tuple(
            sorted(
                (WheelFile.from_mapping(item) for item in wheels_raw),
                key=lambda item: item.filename,
            ),
        )
        filenames = [wheel.filename for wheel in wheels]
        if len(filenames) != len(set(filenames)):
            raise _error("Wheel filenames must be unique", "wheels")
        return cls(
            name=name,
            version=version,
            marker=marker,
            direct=direct,
            wheels=wheels,
        )

    def to_mapping(self) -> dict[str, object]:
        """Return the closed JSON representation."""
        return {
            "name": self.name,
            "version": self.version,
            "marker": self.marker,
            "direct": self.direct,
            "wheels": [wheel.to_mapping() for wheel in self.wheels],
        }


@dataclass(frozen=True)
class LockFile:
    """Hash-pinned lock for one exact target and condition set."""

    channel_key: str
    python_abi: str
    platform_tag: str
    condition_set_sha256: str
    direct_requirements: tuple[str, ...]
    packages: tuple[LockPackage, ...]
    schema_version: int = 1

    @classmethod
    def from_mapping(
        cls,
        value: object,
        *,
        allowed_platform_tags: Iterable[str],
    ) -> "LockFile":
        mapping = _closed(value, _LOCK_FIELDS)
        schema_version = _integer(mapping["schema_version"], "schema_version")
        if schema_version != 1:
            raise _error("schema_version must be integer 1", "schema_version")
        channel_key = _string(mapping["channel_key"], "channel_key")
        validate_channel_key(channel_key)
        python_abi = _string(mapping["python_abi"], "python_abi")
        validate_python_abi(python_abi)
        platform_tag = _string(mapping["platform_tag"], "platform_tag")
        validate_platform_tag(
            platform_tag,
            allowed_platform_tags=set(allowed_platform_tags),
        )
        condition_digest = _digest(
            mapping["condition_set_sha256"],
            "condition_set_sha256",
            path=(),
        )
        direct_raw = mapping["direct_requirements"]
        if not isinstance(direct_raw, list):
            raise _error(
                "direct_requirements must be an array",
                "direct_requirements",
            )
        direct_requirements = canonicalize_requirements(direct_raw)
        packages_raw = mapping["packages"]
        if not isinstance(packages_raw, list):
            raise _error("packages must be an array", "packages")
        packages = tuple(
            LockPackage.from_mapping(item) for item in packages_raw
        )
        names = [package.name for package in packages]
        if len(names) != len(set(names)):
            raise _error("Package names must be unique", "packages")
        direct_names = {
            canonicalize_name(Requirement(item).name)
            for item in direct_requirements
        }
        missing = sorted(direct_names - set(names))
        if missing:
            raise _error(f"Direct requirements are not locked: {missing}")
        package_direct_names = {
            package.name for package in packages if package.direct
        }
        if package_direct_names != direct_names:
            raise _error(
                "Package direct flags do not match direct requirements",
                "packages",
            )
        return cls(
            channel_key=channel_key,
            python_abi=python_abi,
            platform_tag=platform_tag,
            condition_set_sha256=condition_digest,
            direct_requirements=direct_requirements,
            packages=tuple(sorted(packages, key=lambda item: item.name)),
        )

    def to_mapping(self) -> dict[str, object]:
        """Return the closed JSON representation."""
        return {
            "schema_version": self.schema_version,
            "channel_key": self.channel_key,
            "python_abi": self.python_abi,
            "platform_tag": self.platform_tag,
            "condition_set_sha256": self.condition_set_sha256,
            "direct_requirements": list(self.direct_requirements),
            "packages": [package.to_mapping() for package in self.packages],
        }

    def canonical_bytes(self) -> bytes:
        """Return deterministic lock bytes."""
        return canonical_json(self.to_mapping())

    def sha256(self) -> str:
        """Return the exact lock file digest."""
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


@dataclass(frozen=True)
class LockManifestEntry:
    """One unique target-to-lock mapping."""

    python_abi: str
    platform_tag: str
    condition_set_sha256: str
    lock_path: str
    lock_sha256: str

    @classmethod
    def from_mapping(
        cls,
        value: object,
        *,
        allowed_platform_tags: Iterable[str],
    ) -> "LockManifestEntry":
        mapping = _closed(value, _LOCK_ENTRY_FIELDS)
        python_abi = _string(mapping["python_abi"], "python_abi")
        validate_python_abi(python_abi)
        platform_tag = _string(mapping["platform_tag"], "platform_tag")
        validate_platform_tag(
            platform_tag,
            allowed_platform_tags=set(allowed_platform_tags),
        )
        condition_digest = _digest(
            mapping["condition_set_sha256"],
            "condition_set_sha256",
            path=(),
        )
        lock_path = _string(mapping["lock_path"], "lock_path")
        path = PurePosixPath(lock_path)
        if path.is_absolute() or ".." in path.parts or "\\" in lock_path:
            raise _error(
                "lock_path must be a safe relative POSIX path",
                "lock_path",
            )
        if not lock_path.startswith("locks/") or not lock_path.endswith(
            ".json",
        ):
            raise _error("lock_path must point into locks/", "lock_path")
        lock_sha256 = _digest(mapping["lock_sha256"], "lock_sha256", path=())
        return cls(
            python_abi=python_abi,
            platform_tag=platform_tag,
            condition_set_sha256=condition_digest,
            lock_path=lock_path,
            lock_sha256=lock_sha256,
        )

    @property
    def key(self) -> tuple[str, str, str]:
        """Return the manifest uniqueness key."""
        return (
            self.python_abi,
            self.platform_tag,
            self.condition_set_sha256,
        )

    def to_mapping(self) -> dict[str, str]:
        """Return the closed JSON representation."""
        return {
            "python_abi": self.python_abi,
            "platform_tag": self.platform_tag,
            "condition_set_sha256": self.condition_set_sha256,
            "lock_path": self.lock_path,
            "lock_sha256": self.lock_sha256,
        }


@dataclass(frozen=True)
class LockManifest:
    """Complete release matrix; no matching target is unsupported_platform."""

    channel_key: str
    locks: tuple[LockManifestEntry, ...]
    schema_version: int = 1

    @classmethod
    def from_mapping(
        cls,
        value: object,
        *,
        allowed_platform_tags: Iterable[str],
        descriptor: ChannelDescriptor | None = None,
    ) -> "LockManifest":
        mapping = _closed(value, _LOCK_MANIFEST_FIELDS)
        schema_version = _integer(mapping["schema_version"], "schema_version")
        if schema_version != 1:
            raise _error("schema_version must be integer 1", "schema_version")
        channel_key = _string(mapping["channel_key"], "channel_key")
        validate_channel_key(channel_key)
        entries_raw = mapping["locks"]
        if not isinstance(entries_raw, list):
            raise _error("locks must be an array", "locks")
        locks = tuple(
            LockManifestEntry.from_mapping(
                item,
                allowed_platform_tags=allowed_platform_tags,
            )
            for item in entries_raw
        )
        keys = [entry.key for entry in locks]
        if len(keys) != len(set(keys)):
            raise _error("Each target key must map to one lock", "locks")
        paths = [entry.lock_path for entry in locks]
        if len(paths) != len(set(paths)):
            raise _error("Each lock path must be unique", "locks")
        manifest = cls(
            channel_key=channel_key,
            locks=tuple(sorted(locks, key=lambda entry: entry.key)),
        )
        if descriptor is not None:
            manifest.validate_for_descriptor(descriptor)
        return manifest

    @classmethod
    def from_lock_files(
        cls,
        descriptor: ChannelDescriptor,
        lock_files: Sequence[LockFile],
    ) -> "LockManifest":
        """Build a manifest from already validated exact lock files."""
        if descriptor.process_mode != "runner_process":
            raise _error("Only runner_process descriptors have lock matrices")
        entries: list[LockManifestEntry] = []
        seen: set[tuple[str, str, str]] = set()
        expected_requirements = descriptor.isolated_requirements
        for lock_file in lock_files:
            if lock_file.channel_key != descriptor.channel_key:
                raise _error("Lock channel_key does not match descriptor")
            if lock_file.direct_requirements != expected_requirements:
                raise _error(
                    "Lock direct requirements do not match descriptor",
                )
            if descriptor.supported_python_abis and (
                lock_file.python_abi not in descriptor.supported_python_abis
            ):
                raise _error("Lock Python ABI is not supported by descriptor")
            if descriptor.supported_platform_tags and (
                lock_file.platform_tag
                not in descriptor.supported_platform_tags
            ):
                raise _error(
                    "Lock platform tag is not supported by descriptor",
                )
            key = (
                lock_file.python_abi,
                lock_file.platform_tag,
                lock_file.condition_set_sha256,
            )
            if key in seen:
                raise _error("Each target key must map to one lock")
            seen.add(key)
            path = (
                f"locks/{lock_file.python_abi}/"
                f"{lock_file.platform_tag}/"
                f"{lock_file.condition_set_sha256}.json"
            )
            entries.append(
                LockManifestEntry(
                    python_abi=lock_file.python_abi,
                    platform_tag=lock_file.platform_tag,
                    condition_set_sha256=lock_file.condition_set_sha256,
                    lock_path=path,
                    lock_sha256=lock_file.sha256(),
                ),
            )
        manifest = cls(
            channel_key=descriptor.channel_key,
            locks=tuple(entries),
        )
        manifest.validate_for_descriptor(descriptor)
        return manifest

    def validate_for_descriptor(self, descriptor: ChannelDescriptor) -> None:
        """Require a complete lock matrix for one validated descriptor."""
        if self.channel_key != descriptor.channel_key:
            raise _error("Lock manifest channel_key does not match descriptor")
        expected = _expected_lock_keys(descriptor)
        actual = {entry.key for entry in self.locks}
        if actual != expected:
            missing = sorted(expected - actual)
            extra = sorted(actual - expected)
            raise _error(
                "Lock manifest does not cover descriptor targets: "
                f"missing={missing}, extra={extra}",
                "locks",
            )

    def to_mapping(self) -> dict[str, object]:
        """Return the closed JSON representation."""
        return {
            "schema_version": self.schema_version,
            "channel_key": self.channel_key,
            "locks": [entry.to_mapping() for entry in self.locks],
        }

    def canonical_bytes(self) -> bytes:
        """Return deterministic manifest bytes."""
        return canonical_json(self.to_mapping())


def condition_domain(
    descriptor: ChannelDescriptor,
    effective_config: Mapping[str, object],
) -> tuple[dict[str, object], str]:
    """Resolve and hash the descriptor-declared conditional config domain."""
    conditions = descriptor.condition_set(effective_config)
    return conditions, _condition_digest(conditions)


def _condition_digest(value: Mapping[str, object]) -> str:
    from .identifiers import condition_set_sha256

    return condition_set_sha256(value)


def _expected_lock_keys(
    descriptor: ChannelDescriptor,
) -> set[tuple[str, str, str]]:
    """Enumerate every target declared by a descriptor."""
    fields = {field.name: field for field in descriptor.config_fields}
    condition_names = descriptor.condition_fields
    choices = [fields[name].allowed_values for name in condition_names]
    combinations = itertools.product(*choices) if choices else [()]
    condition_digests: set[str] = set()
    for values in combinations:
        effective = dict(zip(condition_names, values))
        _, digest = condition_domain(descriptor, effective)
        condition_digests.add(digest)
    return {
        (python_abi, platform_tag, condition_digest)
        for python_abi in descriptor.supported_python_abis
        for platform_tag in descriptor.supported_platform_tags
        for condition_digest in condition_digests
    }


def code_root_digest(code_root: Path) -> str:
    """Hash a Channel code root using the bootstrap source algorithm."""
    root = Path(code_root).resolve()
    if not root.is_dir():
        raise _error("code_root must be a directory", "code_root")
    digest = hashlib.sha256()
    files: list[Path] = []
    for path in root.rglob("*"):
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        if path.is_symlink():
            raise _error("code_root cannot contain symlinks", "code_root")
        if path.is_file():
            files.append(path)
    for path in sorted(
        files,
        key=lambda item: item.relative_to(root).as_posix(),
    ):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        content = path.read_bytes()
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def sha256_file(path: Path) -> str:
    """Hash one archive or wheel file exactly."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _archive_paths(code_root: Path) -> list[tuple[str, bytes]]:
    root = Path(code_root).resolve()
    if not root.is_dir():
        raise _error("code_root must be a directory", "code_root")
    members: list[tuple[str, bytes]] = []
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        if path.is_symlink():
            raise _error("Archive cannot contain symlinks", relative)
        if not path.is_file():
            continue
        parts = PurePosixPath(relative).parts
        if any(part in _ARCHIVE_FORBIDDEN_PARTS for part in parts):
            raise _error(
                "Archive contains a forbidden artifact member",
                relative,
            )
        if relative.endswith(".whl"):
            raise _error("Archive cannot contain dependency wheels", relative)
        if relative.startswith("qwenpaw/channel_protocol/"):
            raise _error("Archive cannot contain Protocol SDK", relative)
        members.append((relative, path.read_bytes()))
    names = [name for name, _ in members]
    if "channel.json" not in names:
        raise _error("Archive must contain channel.json")
    if "config.schema.json" not in names:
        raise _error("Archive must contain config.schema.json")
    if "release-manifest.json" not in names:
        raise _error("Archive must contain release-manifest.json")
    if not any(name.startswith("locks/") for name in names):
        raise _error("Archive must contain release locks")
    return sorted(members, key=lambda item: item[0])


def build_reproducible_zip(code_root: Path, output: Path) -> str:
    """Build a deterministic ZIP archive and return its SHA-256 digest."""
    members = _archive_paths(Path(code_root))
    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(
        destination,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as archive:
        for relative, content in members:
            info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, content)
    return sha256_file(destination)


def write_canonical_json(path: Path, value: Mapping[str, object]) -> None:
    """Write one canonical JSON value without a trailing newline."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(canonical_json(value))


def read_json(path: Path) -> object:
    """Read strict UTF-8 JSON for release tooling."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _error("JSON file is invalid", str(path)) from exc
