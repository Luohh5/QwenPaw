#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stage readable channel support from real wheels, without downloading.

The supplied wheelhouse is resolved by pip at release-build time. Existing
native child providers are reused; missing/conflicting dependencies fail the
build. Neither editable environments nor PyInstaller archives are copied.
"""

from __future__ import annotations

import argparse
from email.parser import BytesParser
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import tempfile
import zipfile

from packaging.requirements import Requirement
from packaging.tags import parse_tag
from packaging.utils import canonicalize_name
from packaging.version import Version


_SNAPSHOT = r"""
import importlib.metadata as metadata
import json, platform, sys
if sys.argv[1]:
    sys.path.insert(0, sys.argv[1])
rows = []
for dist in metadata.distributions():
    direct = json.loads(dist.read_text("direct_url.json") or "{}")
    rows.append({
        "name": dist.metadata["Name"], "version": dist.version,
        "requires": list(dist.requires or []),
        "path": str(dist._path),
        "editable": direct.get("dir_info", {}).get("editable", False),
        "wheel": dist.read_text("WHEEL") or "",
    })
try:
    from packaging.tags import sys_tags
    from packaging.markers import default_environment
    tags = [str(tag) for tag in sys_tags()]
    environment = default_environment()
except ImportError:
    tags, environment = [], {}
print(json.dumps({
    "python": {
        "version": list(sys.version_info[:2]),
        "implementation": sys.implementation.name,
        "cache_tag": sys.implementation.cache_tag,
        "sys_platform": sys.platform, "system": platform.system(),
        "machine": platform.machine(), "supported_tags": tags,
    },
    "environment": environment, "distributions": rows,
}))
"""


def _snapshot(python: Path, site: Path | None = None) -> dict:
    result = subprocess.run(
        [str(python), "-I", "-c", _SNAPSHOT, str(site) if site else ""],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
        cwd=python.parent,
    )
    return json.loads(result.stdout)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _dependency_wheels(wheelhouse: Path | None):
    if wheelhouse is None:
        return ()
    if not wheelhouse.is_dir():
        raise ValueError("Wheelhouse must be a directory")
    return sorted(wheelhouse.glob("*.whl"))


def _wheel_metadata(path: Path):
    with zipfile.ZipFile(path) as archive:
        names = [
            n for n in archive.namelist() if n.endswith(".dist-info/METADATA")
        ]
        if len(names) != 1:
            raise ValueError(f"Wheel must have one METADATA: {path.name}")
        message = BytesParser().parsebytes(archive.read(names[0]))
        if any(
            len(message.get_all(key, [])) != 1 for key in ("Name", "Version")
        ):
            raise ValueError(f"Invalid wheel identity: {path.name}")
        return canonicalize_name(message["Name"]), message["Version"]


def _extract_wheel(path: Path, site: Path) -> None:
    """Extract only wheel library payloads, keeping genuine dist-info/RECORD."""
    with zipfile.ZipFile(path) as archive:
        seen = set()
        for info in archive.infolist():
            relative = PurePosixPath(info.filename)
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or any(char in info.filename for char in "\\:")
                or stat.S_ISLNK(info.external_attr >> 16)
                or info.filename in seen
            ):
                raise ValueError(f"Unsafe wheel member: {path.name}")
            seen.add(info.filename)
            if not relative.parts or info.is_dir():
                continue
            parts = relative.parts
            if parts[0].endswith(".data"):
                if len(parts) >= 3 and parts[1] in {"purelib", "platlib"}:
                    raise ValueError(
                        f"Unsupported wheel library relocation: {path.name}",
                    )
                # Channel support needs libraries, not entrypoint scripts.
                continue
            # Keep ordinary .pth bytes for RECORD integrity. This explicit
            # site is added only to sys.path, never via site.addsitedir, so
            # startup hooks stay inert. Editable providers still fail audit.
            target = site.joinpath(*parts)
            if target.exists():
                raise ValueError(f"Overlapping wheel payload: {target.name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(info))


def _validate_closure(snapshot: dict, host_version: str) -> None:
    """Validate installed metadata, using pip's already-resolved wheel set."""
    rows = {}
    for row in snapshot["distributions"]:
        if row["name"]:
            rows.setdefault(canonicalize_name(row["name"]), []).append(row)
    tags = set(snapshot["python"]["supported_tags"])
    if not tags:
        raise ValueError("Target Python requires packaging metadata/tags")
    pending = [Requirement(f"qwenpaw[channels]=={host_version}")]
    visited = set()
    while pending:
        requirement = pending.pop()
        matches = rows.get(canonicalize_name(requirement.name), [])
        if len(matches) != 1:
            raise ValueError(
                f"Missing or duplicate dependency: {requirement.name}",
            )
        row = matches[0]
        if (
            row["editable"]
            or Version(row["version"]) not in requirement.specifier
        ):
            raise ValueError(f"Invalid dependency provider: {requirement}")
        wheel_tags = {
            str(tag)
            for line in row["wheel"].splitlines()
            if line.startswith("Tag:")
            for tag in parse_tag(line.removeprefix("Tag:").strip())
        }
        if not wheel_tags or not tags.intersection(wheel_tags):
            raise ValueError(
                f"Incompatible/missing wheel tags: {requirement.name}",
            )
        key = (
            canonicalize_name(requirement.name),
            frozenset(requirement.extras),
        )
        if key in visited:
            continue
        visited.add(key)
        for value in row["requires"]:
            child = Requirement(value)
            if child.marker is None or any(
                child.marker.evaluate(
                    {**snapshot["environment"], "extra": extra},
                )
                for extra in ({""} | set(requirement.extras))
            ):
                pending.append(child)


def stage_channel_runtime(
    *,
    python: Path,
    wheel: Path,
    resource_dir: Path,
    wheelhouse: Path | None = None,
    python_runtime_release: str = "local-verification",
) -> Path:
    """Return a validated runtime manifest; publish nothing on failure."""
    root = resource_dir.resolve(strict=True)
    python = python.resolve(strict=True)
    executable = python.relative_to(root).as_posix()
    wheel = wheel.resolve(strict=True)
    name, version = _wheel_metadata(wheel)
    if name != "qwenpaw":
        raise ValueError("Expected a real qwenpaw host wheel")
    native = _snapshot(python)
    native_names = {
        canonicalize_name(row["name"])
        for row in native["distributions"]
        if row["name"]
    }
    if "qwenpaw" in native_names:
        raise ValueError("Target Python already contains a host distribution")
    destination = root / "binaries/channel-runtime"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise ValueError("Channel runtime destination must be absent")
    staged = Path(
        tempfile.mkdtemp(prefix=".channel-runtime-", dir=destination.parent),
    )
    try:
        site = staged / "site"
        site.mkdir()
        _extract_wheel(wheel, site)
        selected = {"qwenpaw"}
        for artifact in _dependency_wheels(wheelhouse):
            dependency, _version = _wheel_metadata(artifact)
            if dependency in native_names or dependency == "qwenpaw":
                continue
            if dependency in selected:
                raise ValueError(f"Duplicate wheel provider: {dependency}")
            selected.add(dependency)
            _extract_wheel(artifact, site)
        snapshot = _snapshot(python, site)
        _validate_closure(snapshot, version)
        index = site / "qwenpaw/official_channels/index.json"
        data = json.loads(index.read_text("utf-8"))
        if data.get("host_version") != version:
            raise ValueError("Bundled index host version mismatch")
        for row in data["plugins"]:
            archive = index.parent / row["filename"]
            if (
                archive.parent != index.parent
                or _digest(archive) != row["sha256"]
            ):
                raise ValueError("Bundled channel archive digest mismatch")
        support = "binaries/channel-runtime/site"
        manifest = {
            "schema_version": 1,
            "host_version": version,
            "preset": "channels-full",
            "python_executable": executable,
            "support_root": support,
            "dependency_paths": [support],
            "python": snapshot["python"],
            "python_runtime_release": python_runtime_release,
            "host_wheel_sha256": _digest(wheel),
            "official_index_sha256": _digest(index),
        }
        (staged / "runtime.json").write_text(
            json.dumps(manifest, indent=2) + "\n",
            encoding="utf-8",
        )
        staged.rename(destination)
        return destination / "runtime.json"
    finally:
        if staged.exists():
            shutil.rmtree(staged)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", required=True, type=Path)
    parser.add_argument("--wheel", required=True, type=Path)
    parser.add_argument("--resource-dir", required=True, type=Path)
    parser.add_argument("--wheelhouse", type=Path)
    parser.add_argument(
        "--python-runtime-release",
        default="local-verification",
    )
    print(stage_channel_runtime(**vars(parser.parse_args())))


if __name__ == "__main__":
    main()
