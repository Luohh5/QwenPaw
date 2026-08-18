# -*- coding: utf-8 -*-
"""Post-import verification for provider migrations."""

from __future__ import annotations

import json
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..agents.skill_system import SkillService
from ..app.driver_config_service import DriverConfigService
from ..config.utils import get_plugins_dir
from ..plugins.marketplace_registry import ExternalMarketplaceRegistry
from .models import (
    ImportReceipt,
    MigrationDoctorCheck,
    MigrationDoctorReport,
    ProviderInventory,
)


def _check(
    category: str,
    status: str,
    title: str,
    detail: str,
) -> MigrationDoctorCheck:
    return MigrationDoctorCheck(
        category=category,
        status=status,
        title_zh=title,
        detail_zh=detail,
    )


def _installed_plugin_ids() -> set[str]:
    installed: set[str] = set()
    root = get_plugins_dir()
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


def _memory_scope_ids(workspace: Any, provider_id: str) -> set[str]:
    root = Path(workspace.workspace_dir)
    ids: set[str] = set()
    for path in root.glob(f"**/imports/{provider_id}/*/_scope.json"):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        if not isinstance(value, dict):
            continue
        source_id = str(value.get("source_id") or "")
        trusted_source = (
            value.get("trust") == "source_material_not_instructions"
        )
        if source_id and trusted_source:
            ids.add(source_id)
    return ids


def _skill_states(workspace: Any) -> dict[str, bool]:
    path = Path(workspace.workspace_dir) / "skill.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    skills = value.get("skills") if isinstance(value, dict) else None
    if not isinstance(skills, dict):
        return {}
    return {
        str(name): bool(entry.get("enabled", False))
        for name, entry in skills.items()
        if isinstance(entry, dict)
    }


def _receipt_check(
    workspace: Any,
    receipt: ImportReceipt,
) -> MigrationDoctorCheck:
    path = (
        Path(workspace.workspace_dir)
        / ".qwenpaw"
        / "imports"
        / f"{receipt.migration_id}.json"
    )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        mode = stat.S_IMODE(path.stat().st_mode)
    except (OSError, ValueError, TypeError) as exc:
        return _check(
            "receipt",
            "fail",
            "迁移审计回执",
            f"回执无法读取或解析：{type(exc).__name__}: {exc}",
        )
    identity_ok = (
        isinstance(value, dict)
        and value.get("migration_id") == receipt.migration_id
        and value.get("source") == receipt.source
        and value.get("agent_id") == receipt.agent_id
    )
    if not identity_ok:
        return _check(
            "receipt",
            "fail",
            "迁移审计回执",
            "回执存在，但迁移编号、来源或目标智能体与本次迁移不一致。",
        )
    if mode & 0o077:
        return _check(
            "receipt",
            "warning",
            "迁移审计回执",
            f"回执内容一致，但文件权限为 {mode:o}，建议收紧为仅当前用户可读写。",
        )
    return _check(
        "receipt",
        "pass",
        "迁移审计回执",
        "回执已落盘且身份信息一致，文件权限仅允许当前用户访问。",
    )


async def _session_check(
    workspace: Any,
    inventory: ProviderInventory,
    receipt: ImportReceipt,
) -> MigrationDoctorCheck | None:
    expected = set(receipt.imported_sessions)
    if not expected:
        return None
    chats = await workspace.chat_manager.list_chats(archived=None)
    by_source = {
        str((chat.meta.get("portability") or {}).get("source_id")): chat
        for chat in chats
        if (chat.meta.get("portability") or {}).get("source")
        == inventory.provider_id
    }
    readable = 0
    cwd_ok = 0
    cwd_expected = 0
    for source_id in expected:
        chat = by_source.get(source_id)
        if chat is None:
            continue
        state = await workspace.session.get_session_state_dict(
            chat.session_id,
            chat.user_id,
            chat.channel,
        )
        context = ((state.get("agent") or {}).get("state") or {}).get(
            "context",
        )
        if isinstance(context, list) and context:
            readable += 1
        session = next(
            (
                item
                for item in inventory.sessions
                if item.source_id == source_id
            ),
            None,
        )
        if session is not None and session.cwd and Path(session.cwd).is_dir():
            cwd_expected += 1
            runtime_context = chat.meta.get("runtime_context") or {}
            if str(runtime_context.get("project_dir") or "") == str(
                Path(session.cwd).resolve(),
            ):
                cwd_ok += 1
    status = "pass" if readable == len(expected) else "fail"
    detail = f"已导入 {len(expected)} 个会话，其中 {readable} 个可以读取历史。"
    if cwd_expected:
        detail += f" 需要恢复项目目录的 {cwd_expected} 个会话中，{cwd_ok} 个匹配。"
        if cwd_ok != cwd_expected and status == "pass":
            status = "warning"
    return _check("sessions", status, "会话完整性", detail)


async def run_migration_doctor(
    workspace: Any,
    inventory: ProviderInventory,
    receipt: ImportReceipt,
) -> MigrationDoctorReport:
    """Verify imported assets and return user-facing Chinese results."""
    checks: list[MigrationDoctorCheck] = []
    session_result = await _session_check(workspace, inventory, receipt)
    if session_result is not None:
        checks.append(session_result)

    if receipt.imported_skills:
        visible = {
            item.name
            for item in SkillService(workspace.workspace_dir).list_all_skills()
        }
        states = _skill_states(workspace)
        present = sum(name in visible for name in receipt.imported_skills)
        disabled = sum(
            name in visible and not states.get(name, False)
            for name in receipt.imported_skills
        )
        status = (
            "pass"
            if present == len(receipt.imported_skills)
            and disabled == len(receipt.imported_skills)
            else "fail"
        )
        checks.append(
            _check(
                "skills",
                status,
                "Skill 安全状态",
                f"计划导入 {len(receipt.imported_skills)} 个，实际可见 {present} 个；"
                f"保持禁用 {disabled} 个。",
            ),
        )

    if receipt.imported_mcp_servers:
        cards = {
            card.name: card
            for card in await DriverConfigService(workspace).list_cards()
        }
        present = sum(name in cards for name in receipt.imported_mcp_servers)
        disabled = sum(
            name in cards and not cards[name].enabled
            for name in receipt.imported_mcp_servers
        )
        status = (
            "pass"
            if present == len(receipt.imported_mcp_servers)
            and disabled == len(receipt.imported_mcp_servers)
            else "fail"
        )
        detail = (
            f"计划导入 {len(receipt.imported_mcp_servers)} 个，实际保存 {present} 个；"
            f"保持禁用 {disabled} 个。启用前仍需检查依赖并重新授权。"
        )
        checks.append(_check("mcp", status, "MCP DriverCard", detail))

    if receipt.imported_memory_projects:
        ids = _memory_scope_ids(workspace, inventory.provider_id)
        verified = sum(
            item in ids for item in receipt.imported_memory_projects
        )
        status = (
            "pass"
            if verified == len(receipt.imported_memory_projects)
            else "fail"
        )
        checks.append(
            _check(
                "memory",
                status,
                "长期记忆来源与信任边界",
                f"导入 {len(receipt.imported_memory_projects)} 个作用域，"
                f"其中 {verified} 个具有来源记录和“参考资料而非指令”标记。",
            ),
        )

    if receipt.restored_marketplaces:
        registry_path = getattr(workspace, "marketplace_registry_path", None)
        registry = await ExternalMarketplaceRegistry(registry_path).read()
        sources = registry.get("sources") or {}
        restored = sum(
            any(
                isinstance(item, dict)
                and item.get("provider") == inventory.provider_id
                and item.get("name") == name
                for item in sources.values()
            )
            for name in receipt.restored_marketplaces
        )
        status = (
            "pass"
            if restored == len(receipt.restored_marketplaces)
            else "fail"
        )
        checks.append(
            _check(
                "marketplaces",
                status,
                "Marketplace 来源",
                f"需要恢复 {len(receipt.restored_marketplaces)} 个来源，"
                f"注册表中确认 {restored} 个。",
            ),
        )

    if receipt.installed_plugins:
        installed = _installed_plugin_ids()
        present = sum(item in installed for item in receipt.installed_plugins)
        status = (
            "pass" if present == len(receipt.installed_plugins) else "warning"
        )
        checks.append(
            _check(
                "plugins",
                status,
                "插件安装状态",
                f"原生安装流程返回 {len(receipt.installed_plugins)} 个插件，"
                f"磁盘清单确认 {present} 个。若当前智能体尚未出现能力，请重载智能体。",
            ),
        )

    checks.append(_receipt_check(workspace, receipt))
    if any(item.status == "fail" for item in checks):
        status = "fail"
        summary = "迁移完成，但体检发现失败项，请先处理失败项再继续使用。"
    elif any(item.status == "warning" for item in checks):
        status = "warning"
        summary = "迁移完成，核心数据可用，但仍有需要人工确认的项目。"
    else:
        status = "pass"
        summary = "迁移完成，已检查的项目全部通过。"
    return MigrationDoctorReport(
        status=status,
        summary_zh=summary,
        checked_at=datetime.now(timezone.utc),
        checks=checks,
    )


__all__ = ["run_migration_doctor"]
