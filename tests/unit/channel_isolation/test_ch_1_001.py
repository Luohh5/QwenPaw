# -*- coding: utf-8 -*-
"""Tests for CH-1-001 lock and Channel artifact release models."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import zipfile

import pytest

from qwenpaw.channel_protocol import (
    ArtifactManifest,
    ArtifactRecord,
    ArtifactValidationError,
    ChannelDescriptor,
    LockFile,
    LockManifest,
    LockManifestEntry,
    LockPackage,
    ProtocolRange,
    VersionRange,
    build_reproducible_zip,
    code_root_digest,
    condition_domain,
    read_json,
    sha256_file,
    write_canonical_json,
)
from scripts.channel_isolation.audit_channel_dependencies import (
    audit_channel,
    direct_dependencies,
    import_roots,
    main as audit_main,
)
from scripts.channel_isolation.build_fixture_artifact import build_fixture


PLATFORMS = {
    "macosx_11_0_arm64",
    "manylinux_2_28_x86_64",
    "win_amd64",
}
EMPTY_CONDITION_DIGEST = (
    "dc4e5b494b66d21b82ac92cf406a37d007c80b7d5b986203d5b8d3094d1d051f"
)


def _descriptor() -> ChannelDescriptor:
    """Build one descriptor with a finite condition domain."""
    return ChannelDescriptor.from_mapping(
        {
            "schema_version": 1,
            "channel_key": "fixture",
            "source_kind": "builtin",
            "process_mode": "runner_process",
            "dispatch_mode": "manager_queue",
            "ingress_owner": "none",
            "label": {"en": "Fixture", "zh": "Fixture"},
            "description": {"en": "", "zh": ""},
            "icon": "",
            "doc_url": "",
            "plugin_metadata": None,
            "entrypoint": {
                "scope": "runner",
                "module": "fixture_runner",
                "qualname": "EmptyDriver",
            },
            "config_fields": [
                {
                    "name": "region",
                    "label": "Region",
                    "help": "",
                    "placeholder": "",
                    "type": "select",
                    "required": True,
                    "nullable": False,
                    "default": "asia",
                    "allowed_values": ["asia", "eu"],
                    "secret": False,
                    "condition": True,
                },
            ],
            "core_requirements": [],
            "isolated_requirements": ["Requests >= 2"],
            "condition_fields": ["region"],
            "supported_python_abis": ["cp313-cp313"],
            "supported_platform_tags": ["macosx_11_0_arm64"],
            "capabilities": [],
            "bot_identity_fields": [],
            "environment_passthrough_allowlist": [],
        },
        allowed_platform_tags=PLATFORMS,
    )


def _empty_lock(condition_digest: str = EMPTY_CONDITION_DIGEST) -> LockFile:
    """Build one valid empty lock."""
    return LockFile(
        channel_key="fixture",
        python_abi="cp313-cp313",
        platform_tag="macosx_11_0_arm64",
        condition_set_sha256=condition_digest,
        direct_requirements=(),
        packages=(),
    )


def _requests_lock(condition_digest: str) -> LockFile:
    """Build a lock satisfying the descriptor's direct requirement."""
    package = LockPackage.from_mapping(
        {
            "name": "requests",
            "version": "2.31.0",
            "marker": None,
            "direct": True,
            "wheels": [
                {
                    "filename": "requests-2.31.0-py3-none-any.whl",
                    "url": None,
                    "sha256": "b" * 64,
                },
            ],
        },
    )
    return LockFile(
        channel_key="fixture",
        python_abi="cp313-cp313",
        platform_tag="macosx_11_0_arm64",
        condition_set_sha256=condition_digest,
        direct_requirements=("requests>=2",),
        packages=(package,),
    )


def test_condition_domain_uses_descriptor_defaults() -> None:
    """The effective default is the only input to the condition digest."""
    descriptor = _descriptor()
    conditions, digest = condition_domain(descriptor, {"region": "asia"})

    assert conditions == {"region": "asia"}
    assert digest != EMPTY_CONDITION_DIGEST


def test_lock_canonicalizes_requirements_and_wheel_inventory() -> None:
    """Locks retain exact packages and canonical direct requirements."""
    package = LockPackage.from_mapping(
        {
            "name": "demo-package",
            "version": "1.0",
            "marker": 'python_version >= "3.11"',
            "direct": True,
            "wheels": [
                {
                    "filename": "demo_package-1.0-py3-none-any.whl",
                    "url": "https://example.invalid/demo.whl",
                    "sha256": "a" * 64,
                },
            ],
        },
    )
    lock = LockFile.from_mapping(
        {
            "schema_version": 1,
            "channel_key": "fixture",
            "python_abi": "cp313-cp313",
            "platform_tag": "macosx_11_0_arm64",
            "condition_set_sha256": EMPTY_CONDITION_DIGEST,
            "direct_requirements": ["Demo_Package >= 1"],
            "packages": [package.to_mapping()],
        },
        allowed_platform_tags=PLATFORMS,
    )

    assert lock.direct_requirements == ("demo-package>=1",)
    assert lock.packages[0].version == "1.0"
    assert len(lock.sha256()) == 64


def test_lock_rejects_noncanonical_package_name_and_invalid_marker() -> None:
    """Package names and environment markers use strict release syntax."""
    with pytest.raises(ArtifactValidationError):
        LockPackage.from_mapping(
            {
                "name": "demo__package",
                "version": "1.0",
                "marker": None,
                "direct": True,
                "wheels": [
                    {
                        "filename": "demo_package-1.0-py3-none-any.whl",
                        "url": None,
                        "sha256": "a" * 64,
                    },
                ],
            },
        )

    with pytest.raises(ArtifactValidationError):
        LockPackage.from_mapping(
            {
                "name": "demo-package",
                "version": "1.0",
                "marker": "python_version >>> '3.11'",
                "direct": True,
                "wheels": [
                    {
                        "filename": "demo_package-1.0-py3-none-any.whl",
                        "url": None,
                        "sha256": "a" * 64,
                    },
                ],
            },
        )


def test_lock_requires_direct_flags_to_match_requirements() -> None:
    """A direct requirement must resolve to a package marked direct."""
    package = LockPackage.from_mapping(
        {
            "name": "demo-package",
            "version": "1.0",
            "marker": None,
            "direct": False,
            "wheels": [
                {
                    "filename": "demo_package-1.0-py3-none-any.whl",
                    "url": None,
                    "sha256": "a" * 64,
                },
            ],
        },
    )
    with pytest.raises(ArtifactValidationError):
        LockFile.from_mapping(
            {
                "schema_version": 1,
                "channel_key": "fixture",
                "python_abi": "cp313-cp313",
                "platform_tag": "macosx_11_0_arm64",
                "condition_set_sha256": EMPTY_CONDITION_DIGEST,
                "direct_requirements": ["demo-package>=1"],
                "packages": [package.to_mapping()],
            },
            allowed_platform_tags=PLATFORMS,
        )


def test_lock_rejects_direct_version_or_marker_mismatch() -> None:
    """Direct requirements must match the selected package metadata."""
    package = LockPackage.from_mapping(
        {
            "name": "demo-package",
            "version": "1.0",
            "marker": None,
            "direct": True,
            "wheels": [
                {
                    "filename": "demo_package-1.0-py3-none-any.whl",
                    "url": None,
                    "sha256": "a" * 64,
                },
            ],
        },
    )
    for requirement in (
        "demo-package>=2",
        'demo-package>=1; python_version >= "3.11"',
    ):
        with pytest.raises(ArtifactValidationError):
            LockFile.from_mapping(
                {
                    "schema_version": 1,
                    "channel_key": "fixture",
                    "python_abi": "cp313-cp313",
                    "platform_tag": "macosx_11_0_arm64",
                    "condition_set_sha256": EMPTY_CONDITION_DIGEST,
                    "direct_requirements": [requirement],
                    "packages": [package.to_mapping()],
                },
                allowed_platform_tags=PLATFORMS,
            )


def test_lock_manifest_rejects_duplicate_target() -> None:
    """One target key cannot silently select multiple lock files."""
    entry = LockManifestEntry(
        python_abi="cp313-cp313",
        platform_tag="macosx_11_0_arm64",
        condition_set_sha256=EMPTY_CONDITION_DIGEST,
        lock_path=(
            "locks/cp313-cp313/macosx_11_0_arm64/"
            f"{EMPTY_CONDITION_DIGEST}.json"
        ),
        lock_sha256="a" * 64,
    )
    value = {
        "schema_version": 1,
        "channel_key": "fixture",
        "locks": [entry.to_mapping(), entry.to_mapping()],
    }
    with pytest.raises(ArtifactValidationError):
        LockManifest.from_mapping(value, allowed_platform_tags=PLATFORMS)


@pytest.mark.parametrize(
    "lock_path",
    [
        (
            "locks/./cp313-cp313/macosx_11_0_arm64/"
            f"{EMPTY_CONDITION_DIGEST}.json"
        ),
        (
            "locks//cp313-cp313/macosx_11_0_arm64/"
            f"{EMPTY_CONDITION_DIGEST}.json"
        ),
        (
            "locks/cp313-cp313/macosx_11_0_arm64/"
            f"{EMPTY_CONDITION_DIGEST.upper()}.json"
        ),
    ],
)
def test_lock_manifest_rejects_noncanonical_lock_path(lock_path: str) -> None:
    """Manifest paths must be the generated canonical target paths."""
    value = {
        "schema_version": 1,
        "channel_key": "fixture",
        "locks": [
            {
                "python_abi": "cp313-cp313",
                "platform_tag": "macosx_11_0_arm64",
                "condition_set_sha256": EMPTY_CONDITION_DIGEST,
                "lock_path": lock_path,
                "lock_sha256": "a" * 64,
            },
        ],
    }
    with pytest.raises(ArtifactValidationError):
        LockManifest.from_mapping(value, allowed_platform_tags=PLATFORMS)


def test_lock_manifest_materializes_platform_registry_generator() -> None:
    """A one-shot platform iterable must support every manifest entry."""
    value = {
        "schema_version": 1,
        "channel_key": "fixture",
        "locks": [
            {
                "python_abi": "cp313-cp313",
                "platform_tag": platform,
                "condition_set_sha256": EMPTY_CONDITION_DIGEST,
                "lock_path": (
                    f"locks/cp313-cp313/{platform}/"
                    f"{EMPTY_CONDITION_DIGEST}.json"
                ),
                "lock_sha256": "a" * 64,
            }
            for platform in ("macosx_11_0_arm64", "win_amd64")
        ],
    }
    manifest = LockManifest.from_mapping(
        value,
        allowed_platform_tags=(tag for tag in PLATFORMS),
    )
    assert len(manifest.locks) == 2


def test_lock_manifest_requires_every_condition_target() -> None:
    """A release manifest cannot omit one finite condition combination."""
    descriptor = _descriptor()
    _, asia_digest = condition_domain(descriptor, {"region": "asia"})
    lock = _empty_lock(asia_digest)

    with pytest.raises(ArtifactValidationError):
        LockManifest.from_lock_files(descriptor, [lock])


def test_artifact_record_and_manifest_keep_metadata_separate() -> None:
    """Catalog records and local manifests expose their distinct fields."""
    record = ArtifactRecord(
        channel_key="fixture",
        source_kind="builtin",
        release_version="1.0.0",
        qwenpaw_compatibility=VersionRange("2.1.0", None),
        protocol_compatibility=ProtocolRange(1, 1),
        download_url="https://example.invalid/fixture.zip",
        artifact_sha256="a" * 64,
    )
    parsed_record = ArtifactRecord.from_mapping(record.to_mapping())
    manifest = ArtifactManifest(
        channel_key=parsed_record.channel_key,
        source_kind=parsed_record.source_kind,
        release_version=parsed_record.release_version,
        qwenpaw_compatibility=parsed_record.qwenpaw_compatibility,
        protocol_compatibility=parsed_record.protocol_compatibility,
        source_kind_url=parsed_record.download_url,
        artifact_sha256="a" * 64,
        source_revision="b" * 64,
        descriptor_sha256="c" * 64,
        installed_at="2026-01-01T00:00:00+00:00",
    )

    assert "download_url" in parsed_record.to_mapping()
    assert "source" in manifest.to_mapping()
    assert "download_url" not in manifest.to_mapping()
    assert ArtifactManifest.from_mapping(manifest.to_mapping()) == manifest


@pytest.mark.parametrize(
    "download_url",
    [
        "https://",
        "https:///x",
        "https://host:99999/a",
        "https://host/%zz",
        "https://host/\x01",
    ],
)
def test_artifact_rejects_invalid_download_url(download_url: str) -> None:
    """Artifact records reject malformed absolute HTTP(S) URLs."""
    record = ArtifactRecord(
        channel_key="fixture",
        source_kind="builtin",
        release_version="1.0.0",
        qwenpaw_compatibility=VersionRange("2.1.0", None),
        protocol_compatibility=ProtocolRange(1, 1),
        download_url="https://example.invalid/fixture.zip",
        artifact_sha256="a" * 64,
    )
    value = record.to_mapping()
    value["download_url"] = download_url
    with pytest.raises(ArtifactValidationError):
        ArtifactRecord.from_mapping(value)


@pytest.mark.parametrize(
    "content",
    [
        '{"schema_version":1,"schema_version":2}',
        '{"value":NaN}',
    ],
)
def test_read_json_uses_strict_decoder(
    tmp_path: Path,
    content: str,
) -> None:
    """Release JSON rejects duplicate keys and non-finite numbers."""
    path = tmp_path / "metadata.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ArtifactValidationError):
        read_json(path)


def test_read_json_rejects_invalid_utf8(tmp_path: Path) -> None:
    """Release JSON must be valid UTF-8."""
    path = tmp_path / "metadata.json"
    path.write_bytes(b"\xff")
    with pytest.raises(ArtifactValidationError):
        read_json(path)


def _write_valid_archive_root(code_root: Path) -> None:
    """Write complete descriptor, manifest, and lock metadata."""
    descriptor = _descriptor()
    lock_files = tuple(
        _requests_lock(condition_domain(descriptor, {"region": region})[1])
        for region in ("asia", "eu")
    )
    lock_manifest = LockManifest.from_lock_files(descriptor, lock_files)
    code_root.mkdir(parents=True)
    write_canonical_json(code_root / "channel.json", descriptor.to_mapping())
    write_canonical_json(
        code_root / "config.schema.json",
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
        },
    )
    write_canonical_json(
        code_root / "release-manifest.json",
        lock_manifest.to_mapping(),
    )
    for lock_file, entry in zip(lock_files, lock_manifest.locks):
        write_canonical_json(
            code_root / entry.lock_path,
            lock_file.to_mapping(),
        )
    (code_root / "fixture_runner.py").write_text(
        "class EmptyDriver: pass\n",
        encoding="utf-8",
    )


def test_reproducible_fixture_archive_and_source_revision(
    tmp_path: Path,
) -> None:
    """Archive bytes and source revision are stable across rebuilds."""
    code_root = tmp_path / "code"
    _write_valid_archive_root(code_root)

    first = tmp_path / "first.zip"
    second = tmp_path / "second.zip"
    first_digest = build_reproducible_zip(code_root, first)
    second_digest = build_reproducible_zip(code_root, second)

    assert first.read_bytes() == second.read_bytes()
    assert first_digest == second_digest
    assert code_root_digest(code_root) == code_root_digest(code_root)
    with zipfile.ZipFile(first) as archive:
        names = archive.namelist()
    assert names == sorted(names)
    assert all(not name.endswith(".whl") for name in names)


def test_archive_rejects_semantically_invalid_metadata(tmp_path: Path) -> None:
    """Required archive files must contain valid release metadata."""
    code_root = tmp_path / "code"
    (code_root / "locks").mkdir(parents=True)
    for relative in (
        "channel.json",
        "config.schema.json",
        "release-manifest.json",
    ):
        (code_root / relative).write_text("{}", encoding="utf-8")
    (code_root / "locks" / "empty.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ArtifactValidationError):
        build_reproducible_zip(code_root, tmp_path / "artifact.zip")


def test_fixture_builder_emits_a_complete_parseable_artifact(
    tmp_path: Path,
) -> None:
    """The fixture builder emits metadata and the complete target matrix."""
    first = tmp_path / "first"
    second = tmp_path / "second"
    first_result = build_fixture(first)
    second_result = build_fixture(second)

    assert first_result["lock_count"] == 9
    assert first_result["artifact_sha256"] == second_result["artifact_sha256"]
    assert first_result["source_revision"] == second_result["source_revision"]
    assert (
        first_result["descriptor_sha256"] == second_result["descriptor_sha256"]
    )
    first_archive = Path(str(first_result["archive"]))
    second_archive = Path(str(second_result["archive"]))
    assert first_archive.read_bytes() == second_archive.read_bytes()
    assert sha256_file(first_archive) == first_result["artifact_sha256"]

    record = ArtifactRecord.from_mapping(
        read_json(first / "artifact-record.json"),
    )
    manifest = ArtifactManifest.from_mapping(
        read_json(first / "artifact.json"),
    )
    assert record.artifact_sha256 == manifest.artifact_sha256
    with zipfile.ZipFile(first_archive) as archive:
        descriptor = ChannelDescriptor.from_json(
            archive.read("channel.json"),
            allowed_platform_tags=PLATFORMS,
        )
        lock_manifest = LockManifest.from_mapping(
            json.loads(archive.read("release-manifest.json")),
            allowed_platform_tags=PLATFORMS,
            descriptor=descriptor,
        )
        assert len(lock_manifest.locks) == 9
        assert descriptor.digest() == manifest.descriptor_sha256
        assert all(
            LockFile.from_mapping(
                json.loads(archive.read(entry.lock_path)),
                allowed_platform_tags=PLATFORMS,
            ).sha256()
            == entry.lock_sha256
            for entry in lock_manifest.locks
        )


def test_runner_dependency_audit_exposes_transitive_channel_imports() -> None:
    """Runner imports are not allowed to rely on the Core dependency set."""
    repository_root = Path(__file__).parents[3]
    declared = direct_dependencies(repository_root / "pyproject.toml")
    feishu = audit_channel(
        "feishu",
        repository_root / "src" / "qwenpaw" / "app" / "channels" / "feishu",
        declared,
    )
    onebot = audit_channel(
        "onebot",
        repository_root / "src" / "qwenpaw" / "app" / "channels" / "onebot",
        declared,
    )
    voice = audit_channel(
        "voice",
        repository_root / "src" / "qwenpaw" / "app" / "channels" / "voice",
        declared,
    )

    assert feishu.undeclared_distributions == ()
    assert feishu.optional_compat_imports == ("pkg_resources",)
    assert "aiohttp" in onebot.undeclared_distributions
    assert "aiohttp" in voice.undeclared_distributions
    assert "fastapi" in voice.undeclared_distributions


def test_runner_dependency_audit_rejects_missing_channel_root(
    tmp_path: Path,
) -> None:
    """A missing Channel source root must fail the audit."""
    with pytest.raises(ValueError, match="not a directory"):
        import_roots(tmp_path / "missing")


def test_runner_dependency_audit_cli_rejects_missing_channel_root(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The audit CLI must return non-zero for a missing source root."""
    repository_root = Path(__file__).parents[3]
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "audit_channel_dependencies.py",
            str(repository_root / "pyproject.toml"),
            "--channel",
            "missing",
            str(repository_root / "missing-channel-root"),
        ],
    )
    assert audit_main() == 2
    assert "not a directory" in capsys.readouterr().err


@pytest.mark.parametrize(
    "relative",
    ["artifact.json", "venv/python", "qwenpaw/channel_protocol/sdk.py"],
)
def test_archive_rejects_forbidden_content(
    tmp_path: Path,
    relative: str,
) -> None:
    """Channel archives cannot ship installation or Protocol SDK content."""
    code_root = tmp_path / "code"
    code_root.mkdir()
    for required in (
        "channel.json",
        "config.schema.json",
        "release-manifest.json",
    ):
        (code_root / required).write_text("{}", encoding="utf-8")
    forbidden = code_root / relative
    forbidden.parent.mkdir(parents=True, exist_ok=True)
    forbidden.write_text("bad", encoding="utf-8")
    (code_root / "locks").mkdir()
    (code_root / "locks" / "empty.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ArtifactValidationError):
        build_reproducible_zip(code_root, tmp_path / "artifact.zip")
