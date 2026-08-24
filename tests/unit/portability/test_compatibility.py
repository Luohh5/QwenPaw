# -*- coding: utf-8 -*-
from pathlib import Path
from types import SimpleNamespace

import pytest

from qwenpaw.portability.compatibility import (
    AssetZone,
    CompatibilityStore,
    RunState,
    load_manifest,
    mcp_inline_secret_risks,
    redact_sensitive_text,
    write_summary,
)


def _skill(tmp_path: Path, name: str = "demo") -> SimpleNamespace:
    root = tmp_path / name
    root.mkdir()
    (root / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: demo\n---\n",
        encoding="utf-8",
    )
    return SimpleNamespace(
        source_id=name,
        name=name,
        description="demo",
        directory=root,
    )


def test_four_zone_workflow_requires_current_passing_test(
    tmp_path: Path,
) -> None:
    path = tmp_path / "manifest.json"
    store = CompatibilityStore(path)
    store.prepare(
        migration_id="migration-1",
        source="codex",
        skills=[_skill(tmp_path)],
    )

    with pytest.raises(RuntimeError, match="semantic triage"):
        store.classify("demo", AssetZone.MIGRATE, "works")

    store.record_inspection("demo")
    with pytest.raises(RuntimeError, match="semantic triage"):
        store.record_test("demo", passed=False, summary="invalid")
    store.classify("demo", AssetZone.REPAIR, "portable and not duplicated")
    store.record_test("demo", passed=False, summary="bound to codex")
    store.mark_changed("demo", "updated SKILL.md")
    with pytest.raises(RuntimeError, match="latest change"):
        store.classify("demo", AssetZone.MIGRATE, "works")

    store.record_test("demo", passed=True, summary="native loader passed")
    manifest = store.classify("demo", AssetZone.MIGRATE, "works")
    assert manifest.goal_complete
    assert manifest.by_zone(AssetZone.MIGRATE)[0].revision == 1


def test_semantic_triage_can_discard_without_static_test(
    tmp_path: Path,
) -> None:
    store = CompatibilityStore(tmp_path / "manifest.json")
    store.prepare(
        migration_id="migration-2",
        source="qoder",
        skills=[_skill(tmp_path)],
    )
    store.record_inspection("demo")
    manifest = store.classify("demo", AssetZone.DISCARD, "QwenPaw has it")
    assert manifest.goal_complete
    with pytest.raises(RuntimeError, match="repair zone"):
        store.record_test("demo", passed=True, summary="irrelevant")


def test_hard_stop_preserves_unreviewed_staging_items(
    tmp_path: Path,
) -> None:
    store = CompatibilityStore(tmp_path / "manifest.json")
    store.prepare(
        migration_id="migration-3",
        source="codex",
        skills=[_skill(tmp_path)],
    )
    manifest = store.finish(stopped=True, reason="goal limit")
    assert manifest.state is RunState.STOPPED_LIMIT
    assert manifest.assets[0].zone is AssetZone.STAGING
    assert manifest.stop_reason == "goal limit"


def test_exhausted_staging_asset_is_not_reclassified_as_repair(
    tmp_path: Path,
) -> None:
    store = CompatibilityStore(tmp_path / "manifest.json")
    manifest = store.prepare(
        migration_id="migration-budget",
        source="qoder",
        skills=[_skill(tmp_path)],
    )
    for _ in range(manifest.assets[0].tool_budget):
        store.consume("demo")
    asset = load_manifest(store.path).assets[0]
    assert asset.zone is AssetZone.STAGING
    assert load_manifest(store.path).next_asset is None


def test_asset_budget_reserves_final_classification_call(
    tmp_path: Path,
) -> None:
    store = CompatibilityStore(tmp_path / "manifest.json")
    manifest = store.prepare(
        migration_id="migration-budget-reserve",
        source="qoder",
        skills=[_skill(tmp_path)],
    )
    budget = manifest.assets[0].tool_budget
    store.record_inspection("demo")
    for _ in range(budget - 1):
        store.consume("demo", reserve=1)
    with pytest.raises(RuntimeError, match="tool-call budget"):
        store.consume("demo", reserve=1)

    store.consume("demo")
    result = store.classify("demo", AssetZone.DISCARD, "equivalent")
    assert result.assets[0].tool_calls == budget
    assert result.assets[0].zone is AssetZone.DISCARD


def test_manifest_and_summary_are_owner_only_and_secret_free(
    tmp_path: Path,
) -> None:
    secret = "correct-horse-battery-staple"
    server = SimpleNamespace(
        source_id="mcp",
        name="mcp",
        transport="stdio",
        command="npx",
        args=["--api-key", secret],
        env={"TOKEN": secret},
        headers={"Authorization": secret},
        cwd="",
        url="",
        metadata={},
    )
    path = tmp_path / "manifest.json"
    store = CompatibilityStore(path)
    manifest = store.prepare(
        migration_id="migration-4",
        source="codex",
        mcp_servers=[server],
        plugins=[
            SimpleNamespace(
                source_id="plugin",
                name="plugin",
                marketplace="local",
                version="1",
                install_source="/tmp/plugin",
                metadata={"password": secret},
            ),
        ],
    )
    summary = tmp_path / "summary.md"
    write_summary(summary, manifest)
    assert secret not in path.read_text(encoding="utf-8")
    assert all("metadata" not in item.snapshot for item in manifest.assets)
    assert "unsafe_bindings" not in manifest.assets[0].snapshot
    assert path.stat().st_mode & 0o077 == 0
    assert summary.stat().st_mode & 0o077 == 0
    assert load_manifest(path) == manifest


@pytest.mark.parametrize(
    "args,url",
    [
        (["-phunter2"], ""),
        (["--database-url", "postgresql://u:p@example/db"], ""),
        ([], "https://hooks.slack.com/services/T/B/secret"),
        (["X-Custom-Auth: secret-value"], ""),
    ],
)
def test_inline_mcp_credentials_fail_closed(args, url) -> None:
    assert mcp_inline_secret_risks("npx", args, url)


def test_standalone_token_is_redacted_and_rejected() -> None:
    secret = "sk-abcdefghijklmnopqrstuvwxyz"
    assert secret not in redact_sensitive_text(f"use {secret}")
    assert mcp_inline_secret_risks(f"runner {secret}", []) == ["command"]
