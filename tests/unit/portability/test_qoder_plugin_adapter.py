# -*- coding: utf-8 -*-
"""Security boundaries for generated Qoder Skill-only plugin wrappers."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from qwenpaw.portability.models import SourcePlugin
from qwenpaw.portability.qoder_plugin_adapter import (
    stage_qoder_skill_plugin,
)


def _skill_plugin(source: Path) -> SourcePlugin:
    return SourcePlugin(
        source_id="demo@local-custom",
        name="demo",
        marketplace="local-custom",
        install_source=str(source),
        metadata={
            "adapter": "qoder_skill_only_v1",
            "canonical_custom_root": str(source.parent.resolve()),
        },
    )


def test_adapter_preserves_the_complete_safe_skill_tree(
    tmp_path: Path,
) -> None:
    source = tmp_path / "plugins/custom/demo"
    manifest = source / ".qoder-plugin/plugin.json"
    script = source / "skills/demo/scripts/run.sh"
    empty = source / "skills/demo/assets/empty"
    manifest.parent.mkdir(parents=True)
    script.parent.mkdir(parents=True)
    empty.mkdir(parents=True)
    manifest.write_text(
        json.dumps(
            {"name": "demo", "version": "1.0.0", "skills": "skills"},
        ),
        encoding="utf-8",
    )
    script.write_text("#!/bin/sh\necho safe\n", encoding="utf-8")
    script.chmod(0o755)
    (script.parents[1] / "SKILL.md").write_text("Demo", encoding="utf-8")

    staged = stage_qoder_skill_plugin(_skill_plugin(source))

    assert (staged / "skills/demo/assets/empty").is_dir()
    copied = staged / "skills/demo/scripts/run.sh"
    assert copied.read_bytes() == script.read_bytes()
    assert copied.stat().st_mode & stat.S_IXUSR


def test_adapter_rejects_plugin_id_that_resolves_to_plugin_root(
    tmp_path: Path,
) -> None:
    custom_root = tmp_path / ".qoder" / "plugins" / "custom"
    source = custom_root / "malicious"
    manifest_dir = source / ".qoder-plugin"
    skill_dir = source / "skills" / "demo"
    manifest_dir.mkdir(parents=True)
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo\n---\nSafe content.\n",
        encoding="utf-8",
    )
    (manifest_dir / "plugin.json").write_text(
        json.dumps(
            {
                "name": ".",
                "version": "0.1.0",
                "skills": "./skills/",
            },
        ),
        encoding="utf-8",
    )
    plugin = SourcePlugin(
        source_id="malicious@local-custom",
        name="malicious",
        marketplace="local-custom",
        install_source=str(source),
        metadata={
            "adapter": "qoder_skill_only_v1",
            "canonical_custom_root": str(custom_root.resolve()),
        },
    )

    with pytest.raises(ValueError, match="name is unsafe"):
        stage_qoder_skill_plugin(plugin)
