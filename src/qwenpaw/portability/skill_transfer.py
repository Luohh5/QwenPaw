# -*- coding: utf-8 -*-
"""Shared, bounded traversal for imported Skill trees."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

MAX_SKILL_FILES = 5_000
MAX_SKILL_ENTRIES = 6_000
MAX_SKILL_BYTES = 64 * 1024 * 1024
EDITABLE_SKILL_DIRS = frozenset({"assets", "references", "scripts"})
SKILL_TEXT_SUFFIXES = frozenset(
    {
        ".cfg",
        ".ini",
        ".js",
        ".json",
        ".md",
        ".py",
        ".sh",
        ".toml",
        ".ts",
        ".txt",
        ".yaml",
        ".yml",
    },
)


@dataclass(frozen=True)
class SkillTreeEntry:
    relative: Path
    mode: int
    data: bytes | None = None

    @property
    def is_dir(self) -> bool:
        return self.data is None


def read_bounded_tree(  # pylint: disable=too-many-branches,too-many-statements
    source: Path,
    *,
    required_file: str = "",
) -> Iterator[SkillTreeEntry]:
    """Yield one link-free tree with stable, bounded file snapshots."""
    source = source.expanduser()
    if source.is_symlink():
        raise ValueError("portable source is a symbolic link")
    root = source.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("portable source is not a directory")
    if required_file and not (root / required_file).is_file():
        raise ValueError(f"portable source has no {required_file}")

    entries = files = total = 0
    pending = [root]
    while pending:
        directory = pending.pop()
        if directory.is_symlink() or not directory.resolve().is_relative_to(
            root,
        ):
            raise ValueError("portable directory escapes its source root")
        with os.scandir(directory) as iterator:
            for item in iterator:
                entries += 1
                if entries > MAX_SKILL_ENTRIES:
                    raise ValueError("source exceeds the entry safety limit")
                path = Path(item.path)
                if item.is_symlink():
                    raise ValueError("source contains a symbolic link")
                if not path.resolve(strict=True).is_relative_to(root):
                    raise ValueError("source entry escapes its root")
                relative = path.relative_to(root)
                if item.is_dir(follow_symlinks=False):
                    pending.append(path)
                    yield SkillTreeEntry(relative, 0o700)
                    continue
                if not item.is_file(follow_symlinks=False):
                    raise ValueError("source contains a non-regular entry")

                before = item.stat(follow_symlinks=False)
                files += 1
                total += before.st_size
                if files > MAX_SKILL_FILES or total > MAX_SKILL_BYTES:
                    raise ValueError("source exceeds file or byte limits")
                descriptor = os.open(
                    path,
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                )
                try:
                    opened = os.fstat(descriptor)
                    identity = (opened.st_dev, opened.st_ino, opened.st_size)
                    expected = (before.st_dev, before.st_ino, before.st_size)
                    if (
                        not stat.S_ISREG(opened.st_mode)
                        or identity != expected
                    ):
                        raise ValueError("source file changed during import")
                    with os.fdopen(descriptor, "rb") as stream:
                        descriptor = -1
                        data = stream.read(before.st_size + 1)
                        after = os.fstat(stream.fileno())
                    if (
                        len(data) != before.st_size
                        or (
                            after.st_dev,
                            after.st_ino,
                            after.st_size,
                        )
                        != expected
                    ):
                        raise ValueError("source file changed during import")
                finally:
                    if descriptor >= 0:
                        os.close(descriptor)
                yield SkillTreeEntry(
                    relative,
                    opened.st_mode,
                    data,
                )


def read_bounded_skill_tree(source: Path) -> Iterator[SkillTreeEntry]:
    """Yield one link-free Skill tree with stable, bounded snapshots."""
    yield from read_bounded_tree(source, required_file="SKILL.md")


__all__ = [
    "EDITABLE_SKILL_DIRS",
    "SKILL_TEXT_SUFFIXES",
    "SkillTreeEntry",
    "read_bounded_tree",
    "read_bounded_skill_tree",
]
