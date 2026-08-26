# -*- coding: utf-8 -*-
"""Contracts for safe local adaptation snapshots."""

from __future__ import annotations

from pathlib import Path

import pytest

from qwenpaw.portability.adaptation_staging import stage_local_assets
from qwenpaw.portability.models import ProviderInventory, SourcePlugin


def test_plugin_manifest_detection_does_not_use_following_path_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    manifest = source / ".qoder-plugin/plugin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}", encoding="utf-8")
    inventory = ProviderInventory(
        provider_id="qoder",
        provider_name="Qoder",
        detected=True,
        plugins=[
            SourcePlugin(
                source_id="demo",
                name="demo",
                marketplace="local",
                install_source=str(source),
            ),
        ],
    )

    def fail_is_file(_self):
        raise AssertionError("staging must use the shared no-follow check")

    monkeypatch.setattr(Path, "is_file", fail_is_file)

    warnings = stage_local_assets(inventory, tmp_path / "staging")

    assert not warnings
    staged = Path(inventory.plugins[0].install_source)
    assert (staged / ".qoder-plugin/plugin.json").read_text() == "{}"
