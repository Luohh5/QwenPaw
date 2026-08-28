#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build the reproducible dependency-free CH-1-001 Channel artifact."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import tempfile

from qwenpaw.channel_protocol import (
    ArtifactManifest,
    ArtifactRecord,
    ChannelDescriptor,
    LockFile,
    LockManifest,
    ProtocolRange,
    VersionRange,
    build_reproducible_zip,
    code_root_digest,
    condition_domain,
    write_canonical_json,
)
from qwenpaw.channel_protocol.identifiers import condition_set_sha256


_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_FIXTURE_ROOT = (
    _REPOSITORY_ROOT / "tests" / "fixtures" / "channel_isolation" / "ch_1_001"
)
_PLATFORM_TAGS = (
    "macosx_11_0_arm64",
    "manylinux_2_28_x86_64",
    "win_amd64",
)
_PYTHON_ABIS = ("cp311-cp311", "cp312-cp312", "cp313-cp313")


def _descriptor_mapping() -> dict[str, object]:
    """Return the closed descriptor used by the fixture artifact."""
    return {
        "schema_version": 1,
        "channel_key": "fixture",
        "source_kind": "builtin",
        "process_mode": "runner_process",
        "dispatch_mode": "manager_queue",
        "ingress_owner": "none",
        "label": {"en": "Fixture Channel", "zh": "Fixture Channel"},
        "description": {"en": "", "zh": ""},
        "icon": "",
        "doc_url": "",
        "plugin_metadata": None,
        "entrypoint": {
            "scope": "runner",
            "module": "empty_runner",
            "qualname": "EmptyDriver",
        },
        "config_fields": [],
        "core_requirements": [],
        "isolated_requirements": [],
        "condition_fields": [],
        "supported_python_abis": list(_PYTHON_ABIS),
        "supported_platform_tags": list(_PLATFORM_TAGS),
        "capabilities": [],
        "bot_identity_fields": [],
        "environment_passthrough_allowlist": [],
    }


def _lock_file(
    descriptor: ChannelDescriptor,
    *,
    python_abi: str,
    platform_tag: str,
    condition_digest: str,
) -> LockFile:
    """Build one empty lock for a supported target."""
    return LockFile(
        channel_key=descriptor.channel_key,
        python_abi=python_abi,
        platform_tag=platform_tag,
        condition_set_sha256=condition_digest,
        direct_requirements=(),
        packages=(),
    )


def build_fixture(output_directory: Path) -> dict[str, object]:
    """Build all fixture files and return their release metadata."""
    output = Path(output_directory).resolve()
    output.mkdir(parents=True, exist_ok=True)
    descriptor_mapping = _descriptor_mapping()
    descriptor = ChannelDescriptor.from_mapping(
        descriptor_mapping,
        allowed_platform_tags=set(_PLATFORM_TAGS),
    )
    conditions, condition_digest = condition_domain(descriptor, {})
    del conditions
    assert condition_digest == condition_set_sha256({})
    lock_files = tuple(
        _lock_file(
            descriptor,
            python_abi=python_abi,
            platform_tag=platform_tag,
            condition_digest=condition_digest,
        )
        for python_abi in _PYTHON_ABIS
        for platform_tag in _PLATFORM_TAGS
    )
    lock_manifest = LockManifest.from_lock_files(descriptor, lock_files)

    with tempfile.TemporaryDirectory(
        prefix="ch-1-001-",
        dir=output,
    ) as temporary:
        code_root = Path(temporary) / "code"
        code_root.mkdir()
        shutil.copy2(
            _FIXTURE_ROOT / "empty_runner.py",
            code_root / "empty_runner.py",
        )
        write_canonical_json(
            code_root / "channel.json",
            descriptor.to_mapping(),
        )
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
            lock_path = code_root / entry.lock_path
            write_canonical_json(lock_path, lock_file.to_mapping())

        archive_path = output / "fixture-channel-1.0.0.zip"
        artifact_sha256 = build_reproducible_zip(code_root, archive_path)
        source_revision = code_root_digest(code_root)
        descriptor_sha256 = descriptor.digest()
        record = ArtifactRecord(
            channel_key=descriptor.channel_key,
            source_kind="builtin",
            release_version="1.0.0",
            qwenpaw_compatibility=VersionRange("2.1.0", None),
            protocol_compatibility=ProtocolRange(1, 1),
            download_url="https://artifacts.example.invalid/fixture-channel-1.0.0.zip",
            artifact_sha256=artifact_sha256,
        )
        manifest = ArtifactManifest(
            channel_key=descriptor.channel_key,
            source_kind="builtin",
            release_version=record.release_version,
            qwenpaw_compatibility=record.qwenpaw_compatibility,
            protocol_compatibility=record.protocol_compatibility,
            source_kind_url=record.download_url,
            artifact_sha256=artifact_sha256,
            source_revision=source_revision,
            descriptor_sha256=descriptor_sha256,
            installed_at="2026-01-01T00:00:00+00:00",
        )
        write_canonical_json(
            output / "artifact-record.json",
            record.to_mapping(),
        )
        write_canonical_json(output / "artifact.json", manifest.to_mapping())
        result = {
            "archive": str(archive_path),
            "artifact_sha256": artifact_sha256,
            "source_revision": source_revision,
            "descriptor_sha256": descriptor_sha256,
            "lock_count": len(lock_files),
            "condition_set_sha256": condition_digest,
        }
    return result


def main() -> int:
    """Run the fixture artifact builder."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    result = build_fixture(args.output)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
