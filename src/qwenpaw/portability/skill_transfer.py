# -*- coding: utf-8 -*-
"""Skill-specific limits over the shared safe file primitives."""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

from .safe_files import (
    TreeEntry,
    TreeLimits,
    read_tree,
    write_tree_entry,
)

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


SkillTreeEntry = TreeEntry
_SKILL_LIMITS = TreeLimits(
    entries=MAX_SKILL_ENTRIES,
    files=MAX_SKILL_FILES,
    bytes=MAX_SKILL_BYTES,
)


def read_bounded_tree(
    source: Path,
    *,
    required_file: str = "",
) -> Iterator[SkillTreeEntry]:
    """Yield one link-free tree with stable, bounded file snapshots."""
    yield from read_tree(
        source,
        limits=_SKILL_LIMITS,
        required_file=required_file,
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
    "write_tree_entry",
]
