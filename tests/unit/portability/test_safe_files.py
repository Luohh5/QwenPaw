# -*- coding: utf-8 -*-
"""Contracts for shared, link-free portability file reads."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from qwenpaw.portability.safe_files import (
    TreeBudget,
    TreeLimitError,
    TreeLimits,
    read_tree,
    write_tree_entry,
)


def test_tree_snapshot_preserves_empty_directories_and_executable_mode(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    empty = source / "assets" / "empty"
    empty.mkdir(parents=True)
    script = source / "scripts" / "run.sh"
    script.parent.mkdir()
    script.write_bytes(b"#!/bin/sh\necho safe\n")
    script.chmod(0o755)

    entries = list(
        read_tree(
            source,
            limits=TreeLimits(entries=10, files=5, bytes=1_024),
        ),
    )
    target = tmp_path / "target"
    for entry in entries:
        write_tree_entry(target, entry)

    assert (target / "assets/empty").is_dir()
    assert (target / "scripts/run.sh").read_bytes() == script.read_bytes()
    assert (target / "scripts/run.sh").stat().st_mode & stat.S_IXUSR


def test_tree_snapshot_rejects_symbolic_links(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("private", encoding="utf-8")
    (source / "escape.txt").symlink_to(outside)

    with pytest.raises(ValueError, match="symbolic link"):
        list(
            read_tree(
                source,
                limits=TreeLimits(entries=10, files=5, bytes=1_024),
            ),
        )


@pytest.mark.parametrize(
    ("limits", "message"),
    [
        (TreeLimits(entries=1, files=5, bytes=1_024), "entry.*limit"),
        (TreeLimits(entries=10, files=1, bytes=1_024), "file.*limit"),
        (TreeLimits(entries=10, files=5, bytes=1), "byte.*limit"),
    ],
)
def test_tree_snapshot_enforces_each_aggregate_limit(
    tmp_path: Path,
    limits: TreeLimits,
    message: str,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "one.txt").write_text("one", encoding="utf-8")
    (source / "two.txt").write_text("two", encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        list(read_tree(source, limits=limits))


def test_tree_budget_is_shared_across_multiple_roots(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "one.txt").write_bytes(b"123")
    (second / "two.txt").write_bytes(b"456")
    limits = TreeLimits(entries=10, files=10, bytes=5)
    budget = TreeBudget()

    list(read_tree(first, limits=limits, budget=budget))
    with pytest.raises(TreeLimitError) as caught:
        list(read_tree(second, limits=limits, budget=budget))

    assert caught.value.kind == "byte"
