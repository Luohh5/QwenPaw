# -*- coding: utf-8 -*-
from __future__ import annotations

import os
from pathlib import Path

import pytest

from qwenpaw.portability import planner
from qwenpaw.portability.models import (
    ProviderInventory,
    SourceMemoryFile,
    SourceMemoryProject,
    SourcePlugin,
    SourceSkill,
)
from qwenpaw.portability.planner import inventory_fingerprint


def _skill_inventory(root: Path) -> ProviderInventory:
    return ProviderInventory(
        provider_id="codex",
        provider_name="Codex",
        detected=True,
        skills=[
            SourceSkill(
                source_id="skill-1",
                name="test-skill",
                directory=root,
            ),
        ],
    )


def test_tree_fingerprint_is_stable_and_detects_source_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "skill"
    nested = root / "scripts"
    nested.mkdir(parents=True)
    skill_file = root / "SKILL.md"
    skill_file.write_text("original instructions", encoding="utf-8")
    (nested / "run.sh").write_text("echo safe", encoding="utf-8")
    inventory = _skill_inventory(root)

    def fail_rglob(_self, _pattern):
        raise AssertionError("inventory_fingerprint must not use Path.rglob")

    monkeypatch.setattr(Path, "rglob", fail_rglob)
    first = inventory_fingerprint(inventory)

    assert inventory_fingerprint(inventory) == first
    skill_file.write_text("changed instructions", encoding="utf-8")
    assert inventory_fingerprint(inventory) != first


def test_tree_fingerprint_detects_added_empty_directory(
    tmp_path: Path,
) -> None:
    root = tmp_path / "skill"
    root.mkdir()
    (root / "SKILL.md").write_text("instructions", encoding="utf-8")
    inventory = _skill_inventory(root)
    before = inventory_fingerprint(inventory)

    (root / "empty-assets").mkdir()

    assert inventory_fingerprint(inventory) != before


@pytest.mark.parametrize(
    ("limit_name", "limit", "files", "message"),
    [
        (
            "_MAX_FINGERPRINT_ENTRIES",
            3,
            {f"file-{index}.txt": "x" for index in range(3)},
            "fingerprint entry limit",
        ),
        (
            "_MAX_FINGERPRINT_FILES",
            1,
            {"one.txt": "one", "two.txt": "two"},
            "fingerprint file limit",
        ),
        (
            "_MAX_FINGERPRINT_BYTES",
            4,
            {"large.bin": "12345"},
            "fingerprint byte limit",
        ),
    ],
    ids=["entries", "files", "bytes"],
)
def test_tree_fingerprint_limits_fail_closed(
    write_tree,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit_name: str,
    limit: int,
    files: dict[str, str],
    message: str,
) -> None:
    root = tmp_path / "skill"
    root.mkdir()
    write_tree(root, files)
    monkeypatch.setattr(planner, limit_name, limit)

    with pytest.raises(ValueError, match=message):
        inventory_fingerprint(_skill_inventory(root))


def test_byte_limit_is_cumulative_across_multiple_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "first-skill"
    second = tmp_path / "second-skill"
    first.mkdir()
    second.mkdir()
    (first / "one.txt").write_bytes(b"123")
    (second / "two.txt").write_bytes(b"456")
    inventory = _skill_inventory(first)
    inventory.skills.append(
        SourceSkill(
            source_id="skill-2",
            name="second-skill",
            directory=second,
        ),
    )
    monkeypatch.setattr(planner, "_MAX_FINGERPRINT_BYTES", 5)

    with pytest.raises(ValueError, match="fingerprint byte limit"):
        inventory_fingerprint(inventory)


def test_tree_rejects_symbolic_link_escape(tmp_path: Path) -> None:
    root = tmp_path / "skill"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("do not read", encoding="utf-8")
    (root / "escape.txt").symlink_to(outside)
    inventory = _skill_inventory(root)

    fingerprint = inventory_fingerprint(inventory)
    outside.write_text("changed outside content", encoding="utf-8")

    # The link is represented only as a rejected marker; its target is never
    # read into the fingerprint.
    assert inventory_fingerprint(inventory) == fingerprint


def test_tree_rejects_non_regular_entry(tmp_path: Path) -> None:
    root = tmp_path / "skill"
    root.mkdir()
    os.mkfifo(root / "named-pipe")

    # In particular, fingerprinting must not open the FIFO and block.
    assert inventory_fingerprint(_skill_inventory(root))


def test_memory_relative_path_must_stay_in_declared_scope(
    tmp_path: Path,
) -> None:
    source = tmp_path / "memory.md"
    source.write_text("memory", encoding="utf-8")
    inventory = ProviderInventory(
        provider_id="codex",
        provider_name="Codex",
        detected=True,
        memory_projects=[
            SourceMemoryProject(
                source_id="memory-1",
                project_key="project",
                files=[
                    SourceMemoryFile(
                        source_path=source,
                        relative_path=Path("../escape.md"),
                    ),
                ],
            ),
        ],
    )

    with pytest.raises(ValueError, match="relative path escapes"):
        inventory_fingerprint(inventory)


def test_missing_remote_plugin_source_remains_fingerprintable() -> None:
    inventory = ProviderInventory(
        provider_id="codex",
        provider_name="Codex",
        detected=True,
        plugins=[
            SourcePlugin(
                source_id="plugin-1",
                name="remote-plugin",
                marketplace="community",
                install_source="https://example.invalid/plugin.git",
            ),
        ],
    )

    assert inventory_fingerprint(inventory) == inventory_fingerprint(inventory)
