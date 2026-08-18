# -*- coding: utf-8 -*-
"""Read-only inventory planning and per-asset fidelity classification."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..app.driver_config_service import DriverConfigService
from ..config.utils import get_plugins_dir
from .models import (
    MigrationAssetPlan,
    MigrationPlan,
    ProviderInventory,
)

_SUPPORTED_MCP_TRANSPORTS = {"stdio", "streamable_http", "sse"}


def _hash_file(hasher: Any, path: Path) -> None:
    hasher.update(str(path).encode("utf-8", errors="replace"))
    try:
        if path.is_symlink() or not path.is_file():
            hasher.update(b"[unavailable]")
            return
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                hasher.update(chunk)
    except OSError as exc:
        hasher.update(f"[error:{type(exc).__name__}]".encode())


def inventory_fingerprint(inventory: ProviderInventory) -> str:
    """Fingerprint normalized objects plus referenced portable file bytes."""
    hasher = hashlib.sha256()
    payload = inventory.model_dump(
        mode="json",
        exclude={"warnings", "source_location"},
    )
    hasher.update(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8"),
    )
    referenced: set[Path] = set()
    for project in inventory.memory_projects:
        referenced.update(item.source_path for item in project.files)
    for skill in inventory.skills:
        root = skill.directory.expanduser()
        if root.is_dir() and not root.is_symlink():
            try:
                referenced.update(
                    path for path in root.rglob("*") if path.is_file()
                )
            except OSError:
                referenced.add(root)
        else:
            referenced.add(root)
    for plugin in inventory.plugins:
        if not plugin.install_source:
            continue
        root = Path(plugin.install_source).expanduser()
        if root.is_dir() and not root.is_symlink():
            try:
                referenced.update(
                    path for path in root.rglob("*") if path.is_file()
                )
            except OSError:
                referenced.add(root)
        else:
            referenced.add(root)
    for path in sorted(referenced, key=str):
        _hash_file(hasher, path)
    return hasher.hexdigest()


def _installed_skill_names(workspace: Any) -> set[str]:
    path = Path(workspace.workspace_dir) / "skill.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return set()
    skills = value.get("skills") if isinstance(value, dict) else None
    return set(skills) if isinstance(skills, dict) else set()


def _imported_memory_ids(workspace: Any, provider_id: str) -> set[str]:
    root = Path(workspace.workspace_dir)
    source_ids: set[str] = set()
    if not root.is_dir() or root.is_symlink():
        return source_ids
    for path in root.glob(f"**/imports/{provider_id}/*/_scope.json"):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        source_id = value.get("source_id") if isinstance(value, dict) else None
        if source_id:
            source_ids.add(str(source_id))
    return source_ids


def _installed_plugin_ids() -> set[str]:
    root = get_plugins_dir()
    installed: set[str] = set()
    if not root.is_dir():
        return installed
    try:
        children = list(root.iterdir())
    except OSError:
        return installed
    for child in children:
        manifest = child / "plugin.json"
        if child.is_symlink() or not manifest.is_file():
            continue
        try:
            value = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        if isinstance(value, dict):
            installed.add(str(value.get("id") or child.name))
    return installed


def _asset(
    asset_type: str,
    source_id: str,
    name: str,
    action: str,
    fidelity: str,
    reason_zh: str,
    *,
    default_enabled: bool = False,
) -> MigrationAssetPlan:
    return MigrationAssetPlan(
        asset_type=asset_type,
        source_id=source_id,
        name=name,
        action=action,
        fidelity=fidelity,
        default_enabled=default_enabled,
        reason_zh=reason_zh,
    )


# pylint: disable-next=too-many-branches,too-many-statements
async def build_migration_plan(
    workspace: Any,
    inventory: ProviderInventory,
    *,
    source_home: str = "",
) -> MigrationPlan:
    """Build a reviewable plan without changing runtime assets."""
    resolved_source_home = source_home
    if not resolved_source_home and inventory.source_location is not None:
        resolved_source_home = inventory.source_location.data_home
    chats = await workspace.chat_manager.list_chats(archived=None)
    existing_sessions = {
        (
            str((chat.meta.get("portability") or {}).get("source")),
            str((chat.meta.get("portability") or {}).get("source_id")),
        )
        for chat in chats
    }
    existing_skills = _installed_skill_names(workspace)
    existing_mcp = {
        card.name for card in await DriverConfigService(workspace).list_cards()
    }
    existing_memory = _imported_memory_ids(
        workspace,
        inventory.provider_id,
    )
    installed_plugins = _installed_plugin_ids()
    actions: list[MigrationAssetPlan] = []

    for session in inventory.sessions:
        key = (inventory.provider_id, session.source_id)
        if key in existing_sessions:
            actions.append(
                _asset(
                    "session",
                    session.source_id,
                    session.title,
                    "already_present",
                    "converted_with_loss",
                    "目标智能体中已存在同一来源会话，将保留现有副本。",
                ),
            )
        elif not session.history:
            actions.append(
                _asset(
                    "session",
                    session.source_id,
                    session.title,
                    "skip",
                    "unsupported",
                    "没有可读取的对话历史，无法生成可浏览会话。",
                ),
            )
        else:
            actions.append(
                _asset(
                    "session",
                    session.source_id,
                    session.title,
                    "import_history",
                    "converted_with_loss",
                    "会保留可见消息和历史工具轨迹；原 Harness 中继续执行的语义不保证完全一致。",
                ),
            )

    for skill in inventory.skills:
        if skill.name in existing_skills:
            actions.append(
                _asset(
                    "skill",
                    skill.source_id,
                    skill.name,
                    "conflict_keep_target",
                    "manual_review",
                    "QwenPaw 已有同名 Skill，将保留现有版本。",
                ),
            )
        else:
            actions.append(
                _asset(
                    "skill",
                    skill.source_id,
                    skill.name,
                    "import_disabled",
                    "manual_review",
                    "先经过结构与安全检查并保持禁用，确认命令和工具依赖后再启用。",
                ),
            )

    for server in inventory.mcp_servers:
        if server.name in existing_mcp:
            action = "conflict_keep_target"
            fidelity = "manual_review"
            reason = "QwenPaw 已有同名 DriverCard，将保留现有配置。"
        elif server.transport not in _SUPPORTED_MCP_TRANSPORTS:
            action = "skip"
            fidelity = "unsupported"
            reason = f"暂不支持 MCP transport：{server.transport}。"
        else:
            action = "import_disabled"
            fidelity = "converted"
            reason = "转换成禁用的 DriverCard；凭据和 OAuth 需要重新授权。"
        actions.append(
            _asset(
                "mcp",
                server.source_id,
                server.name,
                action,
                fidelity,
                reason,
            ),
        )

    for project in inventory.memory_projects:
        already = project.source_id in existing_memory
        actions.append(
            _asset(
                "memory",
                project.source_id,
                project.project_key,
                "already_present" if already else "import_scoped",
                "lossless",
                (
                    "相同来源的记忆作用域已经存在，将按内容哈希判断是否需要更新。"
                    if already
                    else "按原始字节保存到 imported memory 区，并标记为参考资料而不是指令。"
                ),
            ),
        )

    for marketplace in inventory.marketplaces:
        actions.append(
            _asset(
                "marketplace",
                marketplace.source_id,
                marketplace.name,
                "restore_source" if marketplace.source else "record_only",
                "converted" if marketplace.source else "archive_only",
                (
                    "恢复独立来源信息，凭据和 URL 查询参数不会复制。"
                    if marketplace.source
                    else "没有独立来源，只记录出处，不复制已安装缓存。"
                ),
            ),
        )

    for plugin in inventory.plugins:
        if not plugin.install_source:
            action = "skip"
            fidelity = "unsupported"
            reason = "没有独立且兼容的安装来源，不会复制其他 Harness 的缓存。"
        elif plugin.name in installed_plugins:
            action = "already_present"
            fidelity = "manual_review"
            reason = "QwenPaw 已有可能同名的插件，将由原生安装器处理版本冲突。"
        elif plugin.metadata.get("harness_bound"):
            action = "native_install_review"
            fidelity = "manual_review"
            reason = "可走原生安装流程，但包含来源 Harness 绑定，相关 Skill 默认禁用。"
        else:
            action = "native_install"
            fidelity = "converted"
            reason = "通过 QwenPaw 原生插件安装和加载流程处理。"
        actions.append(
            _asset(
                "plugin",
                plugin.source_id,
                plugin.name,
                action,
                fidelity,
                reason,
            ),
        )

    counts = {
        "sessions": len(inventory.sessions),
        "skills": len(inventory.skills),
        "mcp_servers": len(inventory.mcp_servers),
        "memory_scopes": len(inventory.memory_projects),
        "marketplaces": len(inventory.marketplaces),
        "plugins": len(inventory.plugins),
        "actions": len(actions),
        "manual_review": sum(
            item.fidelity == "manual_review" for item in actions
        ),
        "unsupported": sum(item.fidelity == "unsupported" for item in actions),
    }
    return MigrationPlan(
        plan_id=f"plan-{uuid4().hex}",
        source=inventory.provider_id,
        source_home=resolved_source_home,
        source_location=inventory.source_location,
        agent_id=workspace.agent_id,
        created_at=datetime.now(timezone.utc),
        inventory_fingerprint=inventory_fingerprint(inventory),
        inventory_counts=counts,
        actions=actions,
        warnings=list(inventory.warnings),
    )


__all__ = ["build_migration_plan", "inventory_fingerprint"]
