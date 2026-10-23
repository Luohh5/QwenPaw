# -*- coding: utf-8 -*-
"""Offline, explicit migration of formerly built-in Harness users."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

import pytest

from qwenpaw.config.config import (
    AgentProfileConfig,
    AgentProfileRef,
    AgentsConfig,
    Config,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_SCRIPT = REPO_ROOT / "scripts/pack/migrate_legacy_harnesses.py"
APPLY_READY = os.environ.get("QWENPAW_A6_APPLY") == "1"


def _saved_agent(root: Path, agent_id: str, backend: str) -> Path:
    workspace = root / "workspaces" / agent_id
    workspace.mkdir(parents=True)
    agent = AgentProfileConfig(
        id=agent_id,
        name=agent_id,
        backend=backend,
        backend_settings={"model": "kept-model"},
    )
    path = workspace / "agent.json"
    path.write_text(agent.model_dump_json(), encoding="utf-8")
    return path


def _root_config(
    root: Path,
    agents: dict[str, str],
    *,
    plugins: dict | None = None,
) -> Path:
    config = Config(
        agents=AgentsConfig(
            active_agent=next(iter(agents)),
            agent_order=list(agents),
            profiles={
                agent_id: AgentProfileRef(
                    id=agent_id,
                    workspace_dir=str(root / "workspaces" / agent_id),
                )
                for agent_id in agents
            },
        ),
        plugins=plugins or {},
    )
    path = root / "config.json"
    path.write_text(config.model_dump_json(), encoding="utf-8")
    return path


def _migration(root: Path, bundles: Path, mode: str = "--dry-run") -> dict:
    env = {
        **os.environ,
        "QWENPAW_WORKING_DIR": str(root),
        "QWENPAW_SECRET_DIR": str(root / "secrets"),
        "PIP_NO_INDEX": "1",
        "UV_OFFLINE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    process = subprocess.run(
        [
            sys.executable,
            str(MIGRATION_SCRIPT),
            "--working-dir",
            str(root),
            "--plugin-dir",
            str(bundles),
            mode,
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert process.returncode == 0, process.stderr
    return json.loads(process.stdout)


def _release_bundle(bundles: Path, plugin_id: str) -> Path:
    process = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts/pack/generate_plugin_metadata.py"),
            "--plugins-root",
            str(REPO_ROOT / "plugins"),
            "--dist",
            str(bundles),
            "--only",
            plugin_id,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert process.returncode == 0, process.stderr
    archive = bundles / "harness" / plugin_id / f"{plugin_id}-1.0.0.zip"
    assert archive.is_file()
    return archive


def test_native_only_upgrade_installs_nothing(tmp_path: Path) -> None:
    root = tmp_path / "user"
    root.mkdir()
    bundles = tmp_path / "bundles"
    bundles.mkdir()
    agent_path = _saved_agent(root, "native", "qwenpaw")
    config_path = _root_config(root, {"native": "qwenpaw"})
    original = (config_path.read_bytes(), agent_path.read_bytes())

    assert _migration(root, bundles)["plugins"] == []
    assert _migration(root, bundles, "--apply")["plugins"] == []
    command = Path(sys.executable).with_name("qwenpaw")
    cli = subprocess.run(
        [
            str(command),
            "plugin",
            "migrate-legacy-harnesses",
            "--plugin-dir",
            str(bundles),
            "--dry-run",
        ],
        env={**os.environ, "QWENPAW_WORKING_DIR": str(root)},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert cli.returncode == 0, cli.stderr
    assert json.loads(cli.stdout)["plugins"] == []
    assert (config_path.read_bytes(), agent_path.read_bytes()) == original
    assert not (root / "plugins").exists()


def test_upgrade_prepares_only_used_backend(tmp_path: Path) -> None:
    root = tmp_path / "user"
    root.mkdir()
    bundles = tmp_path / "bundles"
    bundles.mkdir()
    agent_path = _saved_agent(root, "legacy", "codex")
    config_path = _root_config(root, {"legacy": "codex"})
    original = (config_path.read_bytes(), agent_path.read_bytes())

    plan = _migration(root, bundles)
    assert len(plan["plugins"]) == 1
    assert plan["plugins"][0]["plugin_id"] == "qwenpaw-harness-codex"
    assert plan["plugins"][0]["action"] == "provide-package"
    assert (config_path.read_bytes(), agent_path.read_bytes()) == original
    assert not (root / "plugins").exists()


@pytest.mark.skipif(
    importlib.util.find_spec("codex_cli_bin") is not None,
    reason="Run the missing-SDK gate in a vendor-free core interpreter",
)
def test_missing_sdk_stays_preparation_only_at_final_install_gate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A stale preflight cannot start the dependency installer."""
    from qwenpaw.plugins.loader import PluginLoader

    root = tmp_path / "user"
    root.mkdir()
    bundles = tmp_path / "bundles"
    bundles.mkdir()
    archive = _release_bundle(bundles, "qwenpaw-harness-codex")
    agent = _saved_agent(root, "legacy", "codex")
    config = _root_config(root, {"legacy": "codex"})
    account = root / "secrets" / "account.json"
    account.parent.mkdir()
    account.write_bytes(b'{"token":"unchanged"}')

    plan = _migration(root, bundles)
    assert plan["plugins"][0]["action"] == "prepare-dependencies"
    assert (
        "openai-codex==0.144.4" in plan["plugins"][0]["missing_dependencies"]
    )
    applied = _migration(root, bundles, "--apply")
    assert applied["plugins"][0]["result"] == "not-prepared"
    assert not (root / "plugins").exists()

    source = tmp_path / "source"
    source.mkdir()
    with zipfile.ZipFile(archive) as bundle:
        bundle.extractall(source)
    source = next(source.rglob("plugin.json")).parent
    installed = root / "plugins" / "qwenpaw-harness-codex"
    shutil.copytree(source, installed)
    empty_bundles = tmp_path / "no-bundles"
    empty_bundles.mkdir()
    original = (
        config.read_bytes(),
        agent.read_bytes(),
        account.read_bytes(),
        (installed / "plugin.json").read_bytes(),
    )
    retained = _migration(root, empty_bundles)
    assert retained["plugins"][0]["action"] == "prepare-dependencies"
    assert retained["plugins"][0]["bundle"] is None
    assert (
        "openai-codex==0.144.4"
        in retained["plugins"][0]["missing_dependencies"]
    )
    assert (
        _migration(root, empty_bundles, "--apply")["plugins"][0]["result"]
        == "not-prepared"
    )
    assert (
        config.read_bytes(),
        agent.read_bytes(),
        account.read_bytes(),
        (installed / "plugin.json").read_bytes(),
    ) == original
    target = tmp_path / "target"
    # This checkout retains stale egg-info at both roots; the installed
    # interpreter's metadata is the dependency environment being checked.
    stale_roots = {REPO_ROOT.resolve(), (REPO_ROOT / "src").resolve()}
    monkeypatch.setattr(
        sys,
        "path",
        [path for path in sys.path if Path(path).resolve() not in stale_roots],
    )
    loader = PluginLoader(plugin_dirs=[target])

    def forbidden_install(*_args, **_kwargs):
        raise AssertionError("Migration attempted dependency installation")

    monkeypatch.setattr(
        loader,
        "_install_requirements_locked",
        forbidden_install,
    )
    with pytest.raises(RuntimeError, match="openai-codex"):
        asyncio.run(
            loader.install_plugin_files(source, allow_install=False),
        )
    assert not target.exists()


@pytest.mark.skipif(
    not APPLY_READY,
    reason="Run with both pinned vendor SDKs and QWENPAW_A6_APPLY=1",
)
def test_upgrade_apply_is_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "user"
    root.mkdir()
    bundles = tmp_path / "bundles"
    bundles.mkdir()
    _release_bundle(bundles, "qwenpaw-harness-codex")
    agent_path = _saved_agent(root, "legacy", "codex")
    config_path = _root_config(root, {"legacy": "codex"})
    account = root / "secrets" / "account.json"
    account.parent.mkdir()
    account.write_bytes(b'{"token":"preserve-exactly"}')
    original = (agent_path.read_bytes(), account.read_bytes())

    planned = _migration(root, bundles)
    assert [row["plugin_id"] for row in planned["plugins"]] == [
        "qwenpaw-harness-codex",
    ]
    assert planned["plugins"][0]["bundle"]
    applied = _migration(root, bundles, "--apply")
    assert applied["plugins"][0]["result"] == "installed"
    installed = root / "plugins" / "qwenpaw-harness-codex"
    assert (installed / "plugin.json").is_file()
    first_bytes = (
        config_path.read_bytes(),
        (installed / "plugin.json").read_bytes(),
    )

    repeated = _migration(root, bundles, "--apply")
    assert repeated["plugins"][0]["result"] == "skipped"
    assert (
        config_path.read_bytes(),
        (installed / "plugin.json").read_bytes(),
    ) == first_bytes
    assert (agent_path.read_bytes(), account.read_bytes()) == original


@pytest.mark.skipif(
    not APPLY_READY,
    reason="Run with both pinned vendor SDKs and QWENPAW_A6_APPLY=1",
)
def test_upgrade_respects_explicit_disable(tmp_path: Path) -> None:
    root = tmp_path / "user"
    root.mkdir()
    bundles = tmp_path / "bundles"
    bundles.mkdir()
    _release_bundle(bundles, "qwenpaw-harness-qoder")
    agent_path = _saved_agent(root, "legacy", "qoder")
    config_path = _root_config(
        root,
        {"legacy": "qoder"},
        plugins={"qwenpaw-harness-qoder": {"enabled": False}},
    )
    before = agent_path.read_bytes()

    plan = _migration(root, bundles)
    assert [row["plugin_id"] for row in plan["plugins"]] == [
        "qwenpaw-harness-qoder",
    ]
    assert plan["plugins"][0]["enabled"] is False
    _migration(root, bundles, "--apply")
    assert (
        json.loads(config_path.read_text())["plugins"][
            "qwenpaw-harness-qoder"
        ]["enabled"]
        is False
    )
    assert agent_path.read_bytes() == before
