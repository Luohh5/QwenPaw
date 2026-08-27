# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from qwenpaw.portability.compatibility import (
    AssetType,
    AssetZone,
    CompatibilityAsset,
    CompatibilityManifest,
)
from qwenpaw.portability.import_status import project_asset_results
from qwenpaw.portability.models import (
    ImportAssetState,
    ImportReceipt,
    MigrationAssetPlan,
    MigrationPlan,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _plan(*actions: MigrationAssetPlan) -> MigrationPlan:
    return MigrationPlan(
        plan_id="plan-" + "a" * 32,
        source="codex",
        agent_id="agent-1",
        created_at=_now(),
        inventory_fingerprint="fingerprint",
        actions=list(actions),
    )


def _action(
    asset_type: str,
    source_id: str,
    name: str,
    *,
    action: str = "agent_mission_test_and_adapt",
    reason: str = "进入兼容流程",
) -> MigrationAssetPlan:
    return MigrationAssetPlan(
        asset_type=asset_type,
        source_id=source_id,
        name=name,
        action=action,
        fidelity="agent_decision",
        reason_zh=reason,
    )


def _manifest(
    asset_type: AssetType,
    source_id: str,
    name: str,
    zone: AssetZone,
    reason: str = "",
) -> CompatibilityManifest:
    now = _now()
    return CompatibilityManifest(
        migration_id="migration-1",
        source="codex",
        created_at=now,
        updated_at=now,
        assets=[
            CompatibilityAsset(
                asset_key=f"{asset_type.value}:{source_id}",
                asset_type=asset_type,
                source_id=source_id,
                name=name,
                zone=zone,
                reason=reason,
                updated_at=now,
            ),
        ],
    )


def _receipt(**values) -> ImportReceipt:
    now = _now()
    return ImportReceipt(
        migration_id="migration-1",
        source="codex",
        agent_id="agent-1",
        started_at=now,
        completed_at=now,
        **values,
    )


@pytest.mark.parametrize(
    ("manifest", "receipt", "expected"),
    [
        (None, None, ImportAssetState.PENDING),
        (
            _manifest(AssetType.SKILL, "skill-1", "Skill", AssetZone.REPAIR),
            None,
            ImportAssetState.REPAIRING,
        ),
        (
            _manifest(AssetType.SKILL, "skill-1", "Skill", AssetZone.MIGRATE),
            None,
            ImportAssetState.REPAIRING,
        ),
        (
            _manifest(AssetType.SKILL, "skill-1", "Skill", AssetZone.REPAIR),
            _receipt(imported_skills=["Skill"]),
            ImportAssetState.SUCCEEDED,
        ),
        (
            _manifest(AssetType.SKILL, "skill-1", "Skill", AssetZone.REPAIR),
            _receipt(skipped_skills=["Skill"]),
            ImportAssetState.FAILED,
        ),
    ],
)
def test_projects_five_state_lifecycle(manifest, receipt, expected) -> None:
    results = project_asset_results(
        _plan(_action("skill", "skill-1", "Skill")),
        {"skill:skill-1"},
        manifest=manifest,
        receipt=receipt,
    )

    assert results[0].state is expected


def test_discard_and_existing_assets_are_not_needed() -> None:
    plan = _plan(
        _action("skill", "bound", "Bound"),
        _action(
            "memory",
            "memory-1",
            "Memory",
            action="already_present",
            reason="QwenPaw 中已经存在",
        ),
    )
    manifest = _manifest(
        AssetType.SKILL,
        "bound",
        "Bound",
        AssetZone.DISCARD,
        "绑定 Codex 生态",
    )

    results = project_asset_results(
        plan,
        {"skill:bound", "memory:memory-1"},
        manifest=manifest,
    )

    assert [item.state for item in results] == [
        ImportAssetState.NOT_NEEDED,
        ImportAssetState.NOT_NEEDED,
    ]
    assert results[0].message == "绑定 Codex 生态"
    assert results[1].message == "QwenPaw 中已经存在"


def test_succeeded_but_disabled_is_tooltip_metadata() -> None:
    result = project_asset_results(
        _plan(_action("mcp", "mcp-1", "MCP")),
        {"mcp:mcp-1"},
        manifest=_manifest(
            AssetType.MCP,
            "mcp-1",
            "MCP",
            AssetZone.REPAIR,
        ),
        receipt=_receipt(imported_mcp_servers=["MCP"]),
    )[0]

    assert result.state is ImportAssetState.SUCCEEDED
    assert result.enabled is False
    assert result.reason_code == "imported_disabled"
    assert "保持禁用" in result.message


def test_migrated_asset_is_enabled_except_review_gated_cron() -> None:
    skill = project_asset_results(
        _plan(_action("skill", "skill-1", "Skill")),
        {"skill:skill-1"},
        manifest=_manifest(
            AssetType.SKILL,
            "skill-1",
            "Skill",
            AssetZone.MIGRATE,
        ),
        receipt=_receipt(imported_skills=["Skill"]),
    )[0]
    cron = project_asset_results(
        _plan(_action("scheduled_task", "cron-1", "Cron")),
        {"cron:cron-1"},
        manifest=_manifest(
            AssetType.SCHEDULED_TASK,
            "cron-1",
            "Cron",
            AssetZone.MIGRATE,
        ),
        receipt=_receipt(imported_scheduled_tasks=["cron-1"]),
    )[0]

    assert skill.enabled is True
    assert cron.enabled is False
    assert cron.reason_code == "imported_disabled"


def test_unselected_and_hidden_marketplace_actions_are_omitted() -> None:
    results = project_asset_results(
        _plan(
            _action("skill", "skill-1", "Skill"),
            _action("plugin", "plugin-1", "Plugin"),
            _action("marketplace", "market-1", "Market"),
        ),
        {"plugin:plugin-1"},
    )

    assert [(item.asset_type, item.source_id) for item in results] == [
        ("plugin", "plugin-1"),
    ]
