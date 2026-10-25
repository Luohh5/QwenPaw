# -*- coding: utf-8 -*-
"""Desktop metadata keeps complete, extra-aware installed dependency graphs."""

from importlib import metadata
from pathlib import Path
import runpy

import pytest

ROOT = Path(__file__).resolve().parents[3]


def collector():
    return runpy.run_path(
        str(ROOT / "scripts/pack-tauri/collect_runtime_metadata.py"),
    )["collect_runtime_metadata"]


def distribution(site, name, *, requires=(), version="1.0"):
    normalized = name.replace("-", "_")
    path = site / f"{normalized}-{version}.dist-info"
    path.mkdir(parents=True)
    (path / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
        + "".join(f"Requires-Dist: {value}\n" for value in requires),
    )
    return path


def test_collector_includes_transitive_extras_and_respects_markers(
    tmp_path,
    monkeypatch,
):
    distribution(
        tmp_path,
        "qwenpaw",
        requires=("bridge", 'bridge[ollama]; extra == "full"'),
    )
    distribution(
        tmp_path,
        "bridge",
        requires=(
            'leaf>=1; extra == "ollama"',
            'not-installed; extra == "unused"',
            'wrong-platform; python_version < "0"',
        ),
    )
    distribution(tmp_path, "leaf")
    monkeypatch.syspath_prepend(str(tmp_path))
    copied = []

    def copy(name):
        dist = metadata.distribution(name)
        copied.append(dist.metadata["Name"])
        return [(str(dist._path), dist._path.name)]

    result = collector()(("qwenpaw[full]",), copy)

    assert set(copied) == {"qwenpaw", "bridge", "leaf"}
    assert len(copied) == 3
    assert {Path(source).name for source, _ in result} == {
        "qwenpaw-1.0.dist-info",
        "bridge-1.0.dist-info",
        "leaf-1.0.dist-info",
    }


@pytest.mark.parametrize("installed", [False, True])
def test_collector_fails_build_on_missing_or_incompatible_dependency(
    tmp_path,
    monkeypatch,
    installed,
):
    distribution(tmp_path, "qwenpaw", requires=("missing-runtime-leaf>=2",))
    if installed:
        distribution(tmp_path, "missing-runtime-leaf")
    monkeypatch.syspath_prepend(str(tmp_path))
    error = ValueError if installed else metadata.PackageNotFoundError
    with pytest.raises(error, match="missing-runtime-leaf"):
        collector()(("qwenpaw",), lambda name: [])
