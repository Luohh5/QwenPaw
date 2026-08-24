# -*- coding: utf-8 -*-
"""Install targets must always remain child directories of the plugin root."""

from __future__ import annotations

from pathlib import Path

import pytest

from qwenpaw.plugins.architecture import PluginManifest
from qwenpaw.plugins.loader import PluginLoader


@pytest.mark.asyncio
async def test_dot_plugin_id_cannot_replace_the_plugin_root(
    tmp_path: Path,
) -> None:
    install_root = tmp_path / "plugins"
    source = tmp_path / "source"
    install_root.mkdir()
    source.mkdir()
    marker = install_root / "keep.txt"
    marker.write_text("must survive", encoding="utf-8")
    loader = PluginLoader([install_root])

    with pytest.raises(ValueError, match="safe child"):
        # pylint: disable-next=protected-access
        await loader._load_plugin_from_path_unlocked(
            source,
            PluginManifest(id=".", version="0.1.0"),
            install_dir=install_root,
        )

    assert marker.read_text(encoding="utf-8") == "must survive"
