# -*- coding: utf-8 -*-
"""Validate a bounded, complete set of plain local wheel additions."""

from __future__ import annotations

import base64
import csv
from dataclasses import dataclass
from email.parser import BytesParser
import hashlib
import io
import os
from pathlib import Path, PurePosixPath
import stat
from typing import Mapping
import zipfile

from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.specifiers import SpecifierSet
from packaging.version import Version

from .execution.dependencies import (
    DependencySnapshot,
    dependency_error,
    valid_metadata_headers,
)


@dataclass(frozen=True)
class WheelAddition:
    """A trusted local wheel and its independently supplied expected hash."""

    path: Path
    sha256: str


# lark-oapi 1.7.1 has over 11,000 installed non-pyc files.
# Retain expanded byte limits while allowing ordinary large SDK wheels.
def _members(addition: WheelAddition) -> dict[str, bytes]:
    path = Path(addition.path)
    if (
        path.is_symlink()
        or not path.is_file()
        or path.stat().st_size > 128 * 1024 * 1024
    ):
        raise dependency_error("DEPENDENCY_PLAN_UNVERIFIED")
    # Validate and publish these immutable bytes; never reopen the path.
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != addition.sha256:
        raise dependency_error("DEPENDENCY_PLAN_UNVERIFIED")
    result = {}
    normalized = set()
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        if (
            len(archive.infolist()) > 50000
            or sum(info.file_size for info in archive.infolist())
            > 256 * 1024 * 1024
        ):
            raise dependency_error("DEPENDENCY_PLAN_UNVERIFIED")
        for info in archive.infolist():
            name = info.filename
            relative = PurePosixPath(name)
            # Reject every unsafe archive path form before reading members.
            # pylint: disable=too-many-boolean-expressions
            if (
                not name
                or "\\" in name
                or ":" in name
                or relative.is_absolute()
                or any(
                    part in {"", ".", ".."}
                    for part in name.rstrip("/").split("/")
                )
                or stat.S_ISLNK(info.external_attr >> 16)
            ):
                raise dependency_error("DEPENDENCY_PLAN_UNVERIFIED")
            if info.is_dir():
                continue
            key = os.path.normcase(name).casefold()
            if (
                key in normalized
                or name.endswith(".pth")
                or any(part.endswith(".data") for part in relative.parts)
                or relative.name == "entry_points.txt"
            ):
                raise dependency_error("DEPENDENCY_PLAN_UNVERIFIED")
            normalized.add(key)
            result[name] = archive.read(info)
    if any(
        str(parent) in result
        for name in result
        for parent in PurePosixPath(name).parents
        if str(parent) != "."
    ):
        raise dependency_error("DEPENDENCY_PLAN_UNVERIFIED")
    return result


def _validate_record(files: Mapping[str, bytes], record: str) -> None:
    rows = list(csv.reader(io.StringIO(files[record].decode("utf-8"))))
    seen = set()
    for row in rows:
        if len(row) != 3 or row[0] in seen or row[0] not in files:
            raise dependency_error("DEPENDENCY_PLAN_UNVERIFIED")
        name, digest, length = row
        seen.add(name)
        if name == record:
            if digest or length:
                raise dependency_error("DEPENDENCY_PLAN_UNVERIFIED")
            continue
        expected = (
            base64.urlsafe_b64encode(hashlib.sha256(files[name]).digest())
            .decode()
            .rstrip("=")
        )
        if digest != "sha256=" + expected or length != str(len(files[name])):
            raise dependency_error("DEPENDENCY_PLAN_UNVERIFIED")
    if seen != set(files):
        raise dependency_error("DEPENDENCY_PLAN_UNVERIFIED")


# Whole-set archive and collision validation is deliberately fail-closed.
# pylint: disable=too-many-branches,too-many-boolean-expressions
def validate_additions(
    additions: tuple[WheelAddition, ...],
    snapshot: DependencySnapshot,
    target: Path,
) -> tuple[dict[str, bytes], DependencySnapshot]:
    """Return verified bytes and proposed metadata for closure checks."""
    files: dict[str, bytes] = {}
    distributions = dict(snapshot.distributions)
    occupied = set()
    try:
        for addition in additions:
            name, version, _, tags = parse_wheel_filename(
                Path(addition.path).name,
            )
            name = str(name)
            if name in distributions:
                raise dependency_error("DEPENDENCY_RESTART_REQUIRED")
            if not {str(tag) for tag in tags}.intersection(
                snapshot.supported_tags,
            ):
                raise dependency_error("DEPENDENCY_PLAN_UNVERIFIED")
            members = _members(addition)
            infos = {
                path.split("/", 1)[0]
                for path in members
                if path.split("/", 1)[0].endswith(".dist-info")
            }
            if len(infos) != 1:
                raise dependency_error("DEPENDENCY_PLAN_UNVERIFIED")
            info = infos.pop()
            metadata = BytesParser().parsebytes(members[f"{info}/METADATA"])
            wheel = BytesParser().parsebytes(members[f"{info}/WHEEL"])
            if not valid_metadata_headers(
                metadata,
                ("Name", "Version"),
                ("Requires-Python",),
            ) or not valid_metadata_headers(
                wheel,
                ("Wheel-Version", "Root-Is-Purelib"),
                ("Build", "Generator"),
            ):
                raise dependency_error("DEPENDENCY_PLAN_UNVERIFIED")
            if (
                canonicalize_name(metadata["Name"]) != name
                or Version(metadata["Version"]) != version
                or info != f"{name.replace('-', '_')}-{version}.dist-info"
                or wheel["Wheel-Version"] != "1.0"
                or wheel["Root-Is-Purelib"] not in {"true", "false"}
                or set(wheel.get_all("Tag", [])) != {str(tag) for tag in tags}
            ):
                raise dependency_error("DEPENDENCY_PLAN_UNVERIFIED")
            requires_python = metadata.get("Requires-Python")
            if requires_python and not SpecifierSet(requires_python).contains(
                snapshot.marker_environment["python_full_version"],
                prereleases=True,
            ):
                raise dependency_error("DEPENDENCY_PLAN_UNVERIFIED")
            _validate_record(members, f"{info}/RECORD")
            tops = {path.split("/", 1)[0] for path in members}
            stems = {top.split(".", 1)[0] for top in tops}
            if stems.intersection(occupied):
                raise dependency_error("DEPENDENCY_PLAN_UNVERIFIED")
            for top in tops:
                for site in snapshot.sites:
                    destination = Path(site) / top
                    if destination.exists() or destination.is_symlink():
                        raise dependency_error("DEPENDENCY_RESTART_REQUIRED")
                    # Module and package suffix differences still shadow.
                    stem = top.split(".", 1)[0]
                    if (
                        any(Path(site).glob(stem + ".*"))
                        or (Path(site) / stem).exists()
                    ):
                        raise dependency_error("DEPENDENCY_RESTART_REQUIRED")
            for site in snapshot.sites:
                for origin in Path(site).glob("*.dist-info"):
                    text = origin / "METADATA"
                    installed = (
                        BytesParser().parsebytes(text.read_bytes())
                        if text.is_file()
                        else {}
                    )
                    claimed = installed.get("Name")
                    if (
                        claimed and canonicalize_name(claimed) == name
                    ) or origin.name.lower().replace("_", "-").startswith(
                        name + "-",
                    ):
                        raise dependency_error("DEPENDENCY_RESTART_REQUIRED")
            occupied.update(stems)
            files.update(members)
            distributions[name] = {
                "version": str(version),
                "metadata_path": str(target / info),
                "requires_dist": tuple(metadata.get_all("Requires-Dist", [])),
                "top_level": tuple(
                    sorted(
                        top.split(".", 1)[0] for top in tops if top != info
                    ),
                ),
            }
    except (
        KeyError,
        TypeError,
        ValueError,
        OSError,
        zipfile.BadZipFile,
    ) as exc:
        if hasattr(exc, "reason_code"):
            raise
        raise dependency_error("DEPENDENCY_PLAN_UNVERIFIED") from None
    return files, DependencySnapshot(
        snapshot.executable,
        snapshot.prefix,
        snapshot.sites,
        snapshot.marker_environment,
        snapshot.supported_tags,
        distributions,
    )
