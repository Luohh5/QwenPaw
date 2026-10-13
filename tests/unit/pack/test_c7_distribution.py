# -*- coding: utf-8 -*-
"""C7 release artifacts: inspect products of the real offline build backend."""

from __future__ import annotations

import email
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


ROOT = Path(__file__).resolve().parents[3]
OFFICIAL_IDS = ("dingtalk", "feishu")
EXCLUSIVE_SDKS = {
    "lark-oapi",
    "dingtalk-stream",
    "alibabacloud-dingtalk",
}


def _copy_release_inputs(source: Path, destination: Path) -> None:
    """Stage only the normal backend inputs, outside the checkout."""
    destination.mkdir()
    for name in ("pyproject.toml", "setup.py", "README.md", "LICENSE"):
        shutil.copy2(source / name, destination / name)
    if (source / "MANIFEST.in").exists():
        shutil.copy2(source / "MANIFEST.in", destination / "MANIFEST.in")
    ignored = shutil.ignore_patterns(
        "__pycache__",
        "*.pyc",
        "*.egg-info",
        ".pytest_cache",
        ".DS_Store",
    )
    shutil.copytree(source / "src", destination / "src", ignore=ignored)
    shutil.copytree(
        source / "packages" / "qwenpawmail-mcp" / "src",
        destination / "packages" / "qwenpawmail-mcp" / "src",
        ignore=ignored,
    )
    shutil.copytree(
        source / "plugins" / "channel",
        destination / "plugins" / "channel",
        ignore=ignored,
    )
    shutil.copytree(
        source / "scripts" / "pack",
        destination / "scripts" / "pack",
        ignore=ignored,
    )


def _build(stage: Path, output: Path, method: str) -> Path:
    output.mkdir(parents=True)
    script = (
        "import os, setuptools.build_meta as backend; "
        f"os.chdir({str(stage)!r}); "
        f"print('C7_ARTIFACT=' + backend.{method}({str(output)!r}))"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", script],
        cwd=stage,
        env={**os.environ, "PIP_NO_INDEX": "1", "PYTHONNOUSERSITE": "1"},
        text=True,
        capture_output=True,
        check=False,
        timeout=180,
    )
    assert result.returncode == 0, (
        result.stdout[-4000:] + result.stderr[-4000:]
    )
    names = [
        line.removeprefix("C7_ARTIFACT=")
        for line in result.stdout.splitlines()
        if line.startswith("C7_ARTIFACT=")
    ]
    assert len(names) == 1, result.stdout[-4000:]
    artifact = output / names[0]
    assert artifact.is_file()
    return artifact


@pytest.fixture(name="c7_artifacts", scope="module")
def _c7_artifacts(tmp_path_factory):
    root = tmp_path_factory.mktemp("c7-release")
    stage = root / "stage"
    _copy_release_inputs(ROOT, stage)
    wheel = _build(stage, root / "wheel", "build_wheel")
    sdist = _build(stage, root / "sdist", "build_sdist")
    return stage, wheel, sdist


def _assert_official_bundle(wheel: Path, source: Path) -> dict:
    with zipfile.ZipFile(wheel) as host:
        prefix = "qwenpaw/official_channels/"
        index = json.loads(host.read(prefix + "index.json"))
        assert set(index) == {"schema_version", "host_version", "plugins"}
        assert index["schema_version"] == 1
        assert [item["plugin_id"] for item in index["plugins"]] == list(
            OFFICIAL_IDS,
        )
        for item in index["plugins"]:
            channel = item["plugin_id"]
            manifest = json.loads(
                (
                    source / "plugins" / "channel" / channel / "plugin.json"
                ).read_text(),
            )
            assert set(item) == {
                "plugin_id",
                "version",
                "filename",
                "sha256",
                "descriptor_sha256",
                "requirements_sha256",
                "qwenpaw_version",
            }
            assert item["version"] == manifest["version"]
            assert item["qwenpaw_version"] == manifest["qwenpaw_version"]
            assert item["filename"] == f"{channel}-{manifest['version']}.zip"
            archive_bytes = host.read(prefix + item["filename"])
            assert hashlib.sha256(archive_bytes).hexdigest() == item["sha256"]
            with zipfile.ZipFile(io.BytesIO(archive_bytes)) as plugin:
                assert plugin.testzip() is None
                names = {
                    name
                    for name in plugin.namelist()
                    if not name.endswith("/")
                }
                source_dir = source / "plugins" / "channel" / channel
                expected = {
                    f"{channel}/{path.relative_to(source_dir).as_posix()}"
                    for path in source_dir.rglob("*")
                    if path.is_file()
                    and "__pycache__" not in path.parts
                    and path.suffix != ".pyc"
                }
                assert names == expected
                assert (
                    plugin.read(f"{channel}/plugin.json")
                    == (source_dir / "plugin.json").read_bytes()
                )
                for rel, key in (
                    ("channel.json", "descriptor_sha256"),
                    ("requirements.txt", "requirements_sha256"),
                ):
                    digest = hashlib.sha256(
                        plugin.read(f"{channel}/{rel}"),
                    ).hexdigest()
                    assert digest == item[key]
        return index


def test_c7_wheel_has_closed_private_official_bundle(c7_artifacts) -> None:
    stage, wheel, _ = c7_artifacts
    _assert_official_bundle(wheel, stage)


def test_c7_metadata_moves_exclusive_sdks_to_channels(
    c7_artifacts,
) -> None:
    stage, wheel, _ = c7_artifacts
    project = tomllib.loads((stage / "pyproject.toml").read_text())["project"]
    base = {
        canonicalize_name(Requirement(item).name)
        for item in project["dependencies"]
    }
    assert base.isdisjoint(EXCLUSIVE_SDKS)
    shared_alibaba = {
        "alibabacloud-credentials",
        "alibabacloud-tea-openapi",
        "alibabacloud-tea-util",
    }
    assert shared_alibaba <= base

    declared = {
        str(Requirement(line.strip()))
        for channel in OFFICIAL_IDS
        for line in (
            stage / "plugins" / "channel" / channel / "requirements.txt"
        )
        .read_text()
        .splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    extras = project["optional-dependencies"]
    assert {str(Requirement(item)) for item in extras["channels"]} == declared
    assert any("qwenpaw[channels]" in item for item in extras["full"])

    with zipfile.ZipFile(wheel) as host:
        metadata_path = next(
            name
            for name in host.namelist()
            if name.endswith(".dist-info/METADATA")
        )
        metadata = email.message_from_bytes(host.read(metadata_path))
    requires = [
        Requirement(value) for value in metadata.get_all("Requires-Dist", [])
    ]
    wheel_base = {
        canonicalize_name(item.name)
        for item in requires
        if item.marker is None or item.marker.evaluate({"extra": ""})
    }
    assert wheel_base.isdisjoint(EXCLUSIVE_SDKS)
    assert "channels" in metadata.get_all("Provides-Extra", [])
    wheel_channels = {
        str(item).split(";", 1)[0].strip()
        for item in requires
        if item.marker is not None
        and item.marker.evaluate({"extra": "channels"})
        and not item.marker.evaluate({"extra": ""})
    }
    assert wheel_channels == declared


def test_c7_sdist_rebuilds_the_same_official_resources(
    c7_artifacts,
    tmp_path,
) -> None:
    _, wheel, sdist = c7_artifacts
    with tarfile.open(sdist, "r:gz") as archive:
        names = archive.getnames()
        assert any(
            name.endswith("/scripts/pack/generate_plugin_metadata.py")
            for name in names
        )
        for channel in OFFICIAL_IDS:
            assert any(
                name.endswith(f"/plugins/channel/{channel}/plugin.json")
                for name in names
            )
        archive.extractall(tmp_path, filter="data")
    extracted = next(path for path in tmp_path.iterdir() if path.is_dir())
    rebuilt = _build(extracted, tmp_path / "rebuilt", "build_wheel")
    first = _assert_official_bundle(wheel, c7_artifacts[0])
    second = _assert_official_bundle(rebuilt, extracted)
    assert second == first
