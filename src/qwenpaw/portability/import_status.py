# -*- coding: utf-8 -*-
"""Project existing migration artifacts onto the Console's five states."""

from __future__ import annotations

from .compatibility import AssetZone, CompatibilityManifest
from .models import (
    ImportAssetResult,
    ImportAssetState,
    ImportReceipt,
    MigrationPlan,
)

_TYPES = {
    "memory": (
        "memory",
        "imported_memory_projects",
        "skipped_memory_projects",
    ),
    "scheduled_task": (
        "cron",
        "imported_scheduled_tasks",
        "skipped_scheduled_tasks",
    ),
    "skill": ("skill", "imported_skills", "skipped_skills"),
    "mcp": ("mcp", "imported_mcp_servers", "skipped_mcp_servers"),
    "plugin": ("plugin", "installed_plugins", "skipped_plugins"),
}
_COMPATIBILITY_KEYS = {
    "scheduled_task": "scheduled_tasks",
    "skill": "skills",
    "mcp": "mcp",
    "plugin": "plugins",
}
_NOT_NEEDED_ACTIONS = {
    "already_present",
    "conflict_keep_target",
    "record_only",
    "skip",
}


def _matches(
    values: list[str],
    source_id: str,
    name: str,
    canonical_id: str = "",
) -> bool:
    return source_id in values or name in values or canonical_id in values


def project_asset_results(
    plan: MigrationPlan,
    selected_keys: set[str],
    *,
    manifest: CompatibilityManifest | None = None,
    receipt: ImportReceipt | None = None,
    force_retry: bool = False,
) -> list[ImportAssetResult]:
    """Return selected visible assets without mutating migration artifacts."""
    assets = manifest.assets if manifest else ()
    zones = {item.asset_key: item for item in assets}
    results: list[ImportAssetResult] = []
    for action in plan.actions:
        mapping = _TYPES.get(action.asset_type)
        if mapping is None:
            continue
        public_type, imported_field, skipped_field = mapping
        if f"{public_type}:{action.source_id}" not in selected_keys:
            continue
        result = ImportAssetResult(
            asset_type=public_type,
            source_id=action.source_id,
            name=action.name,
            requires_sessions=action.requires_sessions,
        )
        canonical_id = ""
        if public_type == "plugin":
            canonical_id = action.source_id.partition("@")[0]
        if action.action in _NOT_NEEDED_ACTIONS and not force_retry:
            result.state = ImportAssetState.NOT_NEEDED
            result.reason_code = action.action
            result.message = action.reason_zh
            results.append(result)
            continue

        compatibility_type = _COMPATIBILITY_KEYS.get(action.asset_type)
        asset = zones.get(f"{compatibility_type}:{action.source_id}")
        if receipt is None:
            if asset and asset.zone in {AssetZone.REPAIR, AssetZone.MIGRATE}:
                result.state = ImportAssetState.REPAIRING
        elif _matches(
            getattr(receipt, imported_field),
            action.source_id,
            action.name,
            canonical_id,
        ):
            result.state = ImportAssetState.SUCCEEDED
            result.enabled = (
                None
                if public_type == "memory"
                else public_type not in {"cron", "mcp"}
                and bool(asset and asset.zone is AssetZone.MIGRATE)
            )
            if result.enabled is False:
                result.reason_code = "imported_disabled"
                result.message = "已导入且保持禁用，请确认后手动启用。"
        else:
            result.state = ImportAssetState.FAILED
            result.reason_code = "not_materialized"
            result.message = "未能写入 QwenPaw，请手动修改相关配置后重试。"
            if not _matches(
                getattr(receipt, skipped_field),
                action.source_id,
                action.name,
                canonical_id,
            ):
                result.reason_code = "missing_result"
        results.append(result)
    return results


__all__ = ["project_asset_results"]
