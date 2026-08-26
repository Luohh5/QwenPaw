# -*- coding: utf-8 -*-
"""Small, shared primitives for bounded no-follow file reads."""

from __future__ import annotations

import os
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterator, Literal


@dataclass(frozen=True)
class TreeLimits:
    """Aggregate limits applied to one or more source trees."""

    entries: int
    files: int
    bytes: int


@dataclass
class TreeBudget:
    """Mutable counters that may be shared across several tree walks."""

    entries: int = 0
    files: int = 0
    bytes: int = 0


@dataclass(frozen=True)
class TreeSourceEntry:
    """One lstat-backed source entry; links are never followed."""

    path: Path
    relative: Path
    info: os.stat_result

    @property
    def is_dir(self) -> bool:
        return stat.S_ISDIR(self.info.st_mode)

    @property
    def is_file(self) -> bool:
        return stat.S_ISREG(self.info.st_mode)


@dataclass(frozen=True)
class TreeEntry:
    """One stable in-memory tree snapshot entry."""

    relative: Path
    mode: int
    data: bytes | None = None

    @property
    def is_dir(self) -> bool:
        return self.data is None


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _unsafe(path: Path, reason: str) -> ValueError:
    return ValueError(f"unsafe portable file {path}: {reason}")


def _add_entry(
    budget: TreeBudget,
    limits: TreeLimits,
    path: Path,
    info: os.stat_result,
) -> None:
    budget.entries += 1
    if budget.entries > limits.entries:
        raise ValueError(f"source exceeds the entry safety limit: {path}")
    if not stat.S_ISREG(info.st_mode):
        return
    budget.files += 1
    budget.bytes += info.st_size
    if budget.files > limits.files:
        raise ValueError(f"source exceeds the file safety limit: {path}")
    if info.st_size < 0 or budget.bytes > limits.bytes:
        raise ValueError(f"source exceeds the byte safety limit: {path}")


def _within(path: Path, root: Path) -> None:
    try:
        path.resolve(strict=True).relative_to(root)
    except (OSError, ValueError) as exc:
        raise _unsafe(path, "entry escapes its source root") from exc


@contextmanager
def open_regular_file(
    path: Path,
    *,
    expected: os.stat_result | None = None,
    max_bytes: int | None = None,
) -> Iterator[tuple[BinaryIO, os.stat_result]]:
    """Open one stable regular file without following a replaced link."""
    before = expected or path.lstat()
    if stat.S_ISLNK(before.st_mode):
        raise _unsafe(path, "symbolic links are not allowed")
    if not stat.S_ISREG(before.st_mode):
        raise _unsafe(path, "entry is not a regular file")
    if max_bytes is not None and before.st_size > max_bytes:
        raise ValueError(f"source exceeds the byte safety limit: {path}")

    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or _identity(opened) != _identity(
            before,
        ):
            raise _unsafe(path, "file changed while being opened")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            try:
                yield stream, opened
            finally:
                if _identity(os.fstat(stream.fileno())) != _identity(opened):
                    raise _unsafe(path, "file changed while being read")
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def read_regular_file(
    path: Path,
    *,
    expected: os.stat_result | None = None,
    max_bytes: int | None = None,
) -> bytes:
    """Read a bounded regular file and reject size changes."""
    with open_regular_file(
        path,
        expected=expected,
        max_bytes=max_bytes,
    ) as (stream, info):
        data = stream.read(info.st_size + 1)
    if len(data) != info.st_size:
        raise _unsafe(path, "file changed while being read")
    return data


def walk_tree(  # pylint: disable=too-many-arguments
    source: Path,
    *,
    limits: TreeLimits,
    budget: TreeBudget | None = None,
    include_root: bool = False,
    unsafe: Literal["raise", "yield"] = "raise",
    excluded_dirs: frozenset[str] = frozenset(),
) -> Iterator[TreeSourceEntry]:
    """Walk a bounded tree deterministically without following links."""
    source = source.expanduser()
    root_info = source.lstat()
    if stat.S_ISLNK(root_info.st_mode):
        raise _unsafe(source, "symbolic links are not allowed")
    if not stat.S_ISDIR(root_info.st_mode):
        raise _unsafe(source, "source is not a directory")
    root = source.resolve(strict=True)
    counters = budget or TreeBudget()
    if include_root:
        _add_entry(counters, limits, root, root_info)
        yield TreeSourceEntry(root, Path(), root_info)

    pending = [root]
    while pending:
        directory = pending.pop()
        _within(directory, root)
        children: list[TreeSourceEntry] = []
        with os.scandir(directory) as iterator:
            for item in iterator:
                path = Path(item.path)
                info = item.stat(follow_symlinks=False)
                _add_entry(counters, limits, path, info)
                entry = TreeSourceEntry(path, path.relative_to(root), info)
                if stat.S_ISLNK(info.st_mode):
                    if unsafe == "raise":
                        raise _unsafe(path, "symbolic links are not allowed")
                    children.append(entry)
                    continue
                if not (entry.is_dir or entry.is_file):
                    if unsafe == "raise":
                        raise _unsafe(path, "entry is not a regular file")
                    children.append(entry)
                    continue
                _within(path, root)
                children.append(entry)
        children.sort(key=lambda entry: entry.relative.as_posix())
        for entry in children:
            if entry.is_dir and entry.path.name not in excluded_dirs:
                pending.append(entry.path)
            yield entry


def read_tree(
    source: Path,
    *,
    limits: TreeLimits,
    required_file: str = "",
) -> Iterator[TreeEntry]:
    """Snapshot one bounded, link-free source tree."""
    if required_file:
        relative = Path(required_file)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("required file path escapes its source root")
        try:
            info = (source / relative).lstat()
        except FileNotFoundError as exc:
            raise ValueError(
                f"portable source has no {required_file}",
            ) from exc
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"portable source has no {required_file}")
    for entry in walk_tree(source, limits=limits):
        if entry.is_dir:
            yield TreeEntry(entry.relative, 0o700)
        else:
            yield TreeEntry(
                entry.relative,
                entry.info.st_mode,
                read_regular_file(entry.path, expected=entry.info),
            )


def write_tree_entry(root: Path, entry: TreeEntry) -> None:
    """Write one trusted snapshot entry below a private target root."""
    if entry.relative.is_absolute() or ".." in entry.relative.parts:
        raise ValueError("tree snapshot path escapes its target root")
    output = root / entry.relative
    if entry.is_dir:
        output.mkdir(parents=True, mode=0o700, exist_ok=True)
        return
    output.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    output.write_bytes(entry.data or b"")
    os.chmod(output, 0o700 if entry.mode & stat.S_IXUSR else 0o600)


__all__ = [
    "TreeBudget",
    "TreeEntry",
    "TreeLimits",
    "TreeSourceEntry",
    "open_regular_file",
    "read_regular_file",
    "read_tree",
    "walk_tree",
    "write_tree_entry",
]
