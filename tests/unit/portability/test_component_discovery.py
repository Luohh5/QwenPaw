# -*- coding: utf-8 -*-
"""Behavioral tests for bounded compatibility component discovery."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from qwenpaw.portability.compatibility import AssetType
from qwenpaw.portability.component_discovery import discover_components


def test_component_discovery_uses_the_shared_bounded_walker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill = tmp_path / "skills" / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("Demo", encoding="utf-8")

    def fail_rglob(_self, _pattern):
        raise AssertionError("component discovery must not use Path.rglob")

    monkeypatch.setattr(Path, "rglob", fail_rglob)

    components = discover_components(
        AssetType.PLUGIN,
        SimpleNamespace(install_source=str(tmp_path)),
    )

    assert components[0].paths == ["skills/demo/SKILL.md"]
