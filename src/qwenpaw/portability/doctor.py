# -*- coding: utf-8 -*-
"""Post-import verification for provider migrations."""

from __future__ import annotations

import json
import stat
from dataclasses import dataclass
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
from .scheduled_tasks import imported_job_source, is_nonlocal_workspace
from .compatibility import (
    AssetType,
    AssetZone,
    CompatibilityManifest,
    RunState,
    counts,
    load_manifest,
)


@dataclass(frozen=True)
class _DoctorContext:
    workspace: Any
    inventory: ProviderInventory
    receipt: ImportReceipt
    manifest: CompatibilityManifest | None
    manifest_error: str

    def assets(self, asset_type: AssetType, identity: str = "name") -> dict:
        items = self.manifest.assets if self.manifest else ()
        return {
            str(getattr(item, identity)): item
            for item in items
            if item.asset_type is asset_type
        }


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


def _status(actual: int, expected: list[str], success: str = "pass") -> str:
    return success if actual == len(expected) else "fail"


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
        if (
            source_id
            and value.get("trust") == "source_material_not_instructions"
        ):
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
    context: _DoctorContext,
) -> MigrationDoctorCheck:
    workspace, receipt = context.workspace, context.receipt
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


def _adaptation_check(
    context: _DoctorContext,
) -> MigrationDoctorCheck | None:
    receipt = context.receipt
    if (
        receipt.adaptation_status == "not_run"
        and not receipt.adaptation_manifest
    ):
        return None
    manifest, error = context.manifest, context.manifest_error
    if not receipt.adaptation_manifest:
        return _check(
            "compatibility",
            "warning",
            "工具和设置兼容清单",
            "自动兼容流程未生成可验证清单；相关资产仍保持禁用。",
        )
    if manifest is None:
        return _check(
            "compatibility",
            "fail",
            "工具和设置兼容清单",
            f"兼容清单无法读取或验证：{error}",
        )
    zone_counts = counts(manifest)
    migrate = zone_counts.get("migrate", 0)
    repair = zone_counts.get("repair", 0)
    status = "warning"
    if manifest.state is RunState.COMPLETED and not repair:
        status = "pass"
    return _check(
        "compatibility",
        status,
        "工具和设置兼容清单",
        f"已检查 {len(manifest.assets)} 项：待迁移 {migrate}，待修复 "
        f"{repair}；"
        f"兼容流程状态为 {manifest.state.value}。",
    )


def _verified_adaptation_manifest(
    workspace: Any,
    receipt: ImportReceipt,
) -> tuple[CompatibilityManifest | None, str]:
    """Load only the manifest which is securely bound to this receipt."""
    if not receipt.adaptation_manifest:
        return None, "未生成兼容清单"
    workspace_root = Path(workspace.workspace_dir).resolve()
    path = Path(receipt.adaptation_manifest)
    if not path.is_absolute():
        path = workspace_root / path
    try:
        path = path.resolve(strict=True)
        if not path.is_relative_to(workspace_root):
            raise ValueError("清单路径越出智能体工作区")
        manifest = load_manifest(path)
    except (OSError, ValueError, TypeError) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if (
        manifest.migration_id != receipt.migration_id
        or manifest.source != receipt.source
    ):
        return None, "迁移编号或来源与回执不一致"
    return manifest, ""


def _job_request_context(job: Any) -> dict[str, Any]:
    request = getattr(job, "request", None)
    context = (
        request.get("request_context")
        if isinstance(request, dict)
        else getattr(request, "request_context", None)
    )
    return context if isinstance(context, dict) else {}


def _job_portability(job: Any) -> dict[str, Any]:
    meta = getattr(job, "meta", {})
    portability = meta.get("portability") if isinstance(meta, dict) else None
    return portability if isinstance(portability, dict) else {}


def _remote_or_unverified_source(job: Any) -> bool:
    portability = _job_portability(job)
    metadata = portability.get("source_metadata")
    return portability.get("source_cwd_remote_or_unverified") is True or (
        isinstance(metadata, dict) and is_nonlocal_workspace(metadata)
    )


async def _session_check(
    context: _DoctorContext,
) -> MigrationDoctorCheck | None:
    workspace = context.workspace
    inventory = context.inventory
    receipt = context.receipt
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
        detail += f" 需要恢复项目目录的 {cwd_expected} 个会话中，"
        detail += f"{cwd_ok} 个匹配。"
        if cwd_ok != cwd_expected and status == "pass":
            status = "warning"
    return _check("sessions", status, "会话完整性", detail)


async def run_migration_doctor(  # pylint: disable=R0912,R0915
    workspace: Any,
    inventory: ProviderInventory,
    receipt: ImportReceipt,
) -> MigrationDoctorReport:
    """Verify imported assets and return user-facing Chinese results."""
    manifest, error = _verified_adaptation_manifest(workspace, receipt)
    context = _DoctorContext(workspace, inventory, receipt, manifest, error)
    checks: list[MigrationDoctorCheck] = []
    session_result = await _session_check(context)
    if session_result is not None:
        checks.append(session_result)
    adaptation_result = _adaptation_check(context)
    if adaptation_result is not None:
        checks.append(adaptation_result)

    if receipt.imported_skills:
        visible = {
            item.name
            for item in SkillService(workspace.workspace_dir).list_all_skills()
        }
        states = _skill_states(workspace)
        present = sum(name in visible for name in receipt.imported_skills)
        zones = {
            name: item.zone
            for name, item in context.assets(AssetType.SKILL).items()
        }
        activation_ok = sum(
            name in visible
            and states.get(name, False)
            is (zones.get(name) is AssetZone.MIGRATE)
            for name in receipt.imported_skills
        )
        status = _status(
            min(present, activation_ok),
            receipt.imported_skills,
        )
        checks.append(
            _check(
                "skills",
                status,
                "Skill 安全状态",
                f"计划导入 {len(receipt.imported_skills)} 个，实际可见 {present} 个；"
                f"启用状态与兼容分区一致 {activation_ok} 个。",
            ),
        )

    if receipt.imported_mcp_servers:
        cards = {
            card.name: card
            for card in await DriverConfigService(workspace).list_cards()
        }
        present = sum(name in cards for name in receipt.imported_mcp_servers)
        activation_ok = sum(
            name in cards
            and not cards[name].enabled
            and cards[name].config.get("requires_review") is True
            for name in receipt.imported_mcp_servers
        )
        status = _status(
            min(present, activation_ok),
            receipt.imported_mcp_servers,
        )
        detail = (
            f"计划导入 {len(receipt.imported_mcp_servers)} 个，实际保存 {present} 个；"
            f"保持禁用并等待人工确认 {activation_ok} 个。"
        )
        checks.append(_check("mcp", status, "MCP DriverCard", detail))

    if receipt.imported_memory_projects:
        ids = _memory_scope_ids(workspace, inventory.provider_id)
        verified = sum(
            item in ids for item in receipt.imported_memory_projects
        )
        status = _status(verified, receipt.imported_memory_projects)
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
        status = _status(restored, receipt.restored_marketplaces)
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
        status = _status(present, receipt.installed_plugins, "warning")
        checks.append(
            _check(
                "plugins",
                status,
                "插件安装状态",
                f"原生安装流程返回 {len(receipt.installed_plugins)} 个插件，"
                f"磁盘清单确认 {present} 个；实际启用状态由兼容流程的"
                "migrate/repair 分区决定。",
            ),
        )

    if receipt.prepared_plugins:
        checks.append(
            _check(
                "plugins_prepared",
                "warning",
                "插件等待用户确认",
                f"{len(receipt.prepared_plugins)} 个插件已通过结构检查并保留"
                "安装来源，但尚未加载任何第三方执行代码；请在插件界面复核"
                "权限、依赖和来源后再确认安装。",
            ),
        )

    if (
        inventory.scheduled_tasks
        or receipt.discovered_scheduled_task_count
        or receipt.imported_scheduled_tasks
        or receipt.skipped_scheduled_tasks
    ):
        cron_manager = getattr(workspace, "cron_manager", None)
        if cron_manager is None:
            checks.append(
                _check(
                    "scheduled_tasks",
                    "fail",
                    "定时任务安全状态",
                    "迁移回执记录了定时任务，但目标智能体的 Cron 服务不可用。",
                ),
            )
        else:
            try:
                jobs = await cron_manager.list_jobs()
                grouped_by_source: dict[tuple[str, str], list[Any]] = {}
                for job in jobs:
                    key = imported_job_source(job)
                    if key is not None:
                        grouped_by_source.setdefault(key, []).append(job)
                expected = {
                    (inventory.provider_id, source_id)
                    for source_id in receipt.imported_scheduled_tasks
                }
                present = sum(key in grouped_by_source for key in expected)
                unique = sum(
                    len(grouped_by_source.get(key, [])) == 1
                    for key in expected
                )
                schedule_assets = context.assets(
                    AssetType.SCHEDULED_TASK,
                    "source_id",
                )

                def activation_matches(key: tuple[str, str]) -> bool:
                    jobs_for_key = grouped_by_source.get(key, [])
                    asset = schedule_assets.get(key[1])
                    if len(jobs_for_key) != 1 or asset is None:
                        return False
                    job = jobs_for_key[0]
                    portability = _job_portability(job)
                    pending = portability.get("requires_review") is True
                    marker = _job_request_context(job).get(
                        "portability_review_required",
                    )
                    if asset.zone is AssetZone.MIGRATE:
                        return (
                            not job.enabled
                            and job.runtime.tool_safety
                            and not pending
                            and marker is False
                            and portability.get("safety")
                            == "reviewed_disabled"
                        )
                    return (
                        asset.zone is AssetZone.REPAIR
                        and not job.enabled
                        and job.runtime.tool_safety
                        and pending
                        and marker is True
                    )

                activation_safe = sum(
                    activation_matches(key) for key in expected
                )
                tasks_by_id = {
                    task.source_id: task for task in inventory.scheduled_tasks
                }
                remote_expected = {
                    (inventory.provider_id, source_id)
                    for source_id in receipt.imported_scheduled_tasks
                    if source_id in tasks_by_id
                    and is_nonlocal_workspace(tasks_by_id[source_id].metadata)
                }
                remote_unmapped = sum(
                    len(grouped_by_source.get(key, [])) == 1
                    and _remote_or_unverified_source(grouped_by_source[key][0])
                    and "project_dir"
                    not in _job_request_context(grouped_by_source[key][0])
                    for key in remote_expected
                )

                imported = receipt.imported_scheduled_tasks
                skipped = receipt.skipped_scheduled_tasks
                processed_ids = imported + skipped
                normalized_count = len(inventory.scheduled_tasks)
                discovered_count = receipt.discovered_scheduled_task_count
                duplicate_receipt_ids = len(processed_ids) != len(
                    set(processed_ids),
                )
                processed_matches = len(
                    processed_ids,
                ) == normalized_count and set(processed_ids) == set(
                    tasks_by_id,
                )
                discovery_valid = (
                    discovered_count == 0
                    or discovered_count >= normalized_count
                )

                compatibility_classified: int | None = None
                if context.manifest is not None:
                    compatibility_classified = sum(
                        source_id in schedule_assets
                        and schedule_assets[source_id].zone
                        in {AssetZone.MIGRATE, AssetZone.REPAIR}
                        for source_id in imported
                    )

                hard_failure = any(
                    (
                        present != len(expected),
                        unique != len(expected),
                        activation_safe != len(expected),
                        remote_unmapped != len(remote_expected),
                        duplicate_receipt_ids,
                        not processed_matches,
                        not discovery_valid,
                        compatibility_classified is not None
                        and compatibility_classified != len(imported),
                    ),
                )
                raw_rejections = max(discovered_count - normalized_count, 0)
                legacy_discovery_count = (
                    discovered_count == 0 and normalized_count > 0
                )
                status = "fail" if hard_failure else "pass"
                if status == "pass" and (
                    raw_rejections or legacy_discovery_count
                ):
                    status = "warning"
                compatibility_detail = ""
                if compatibility_classified is not None:
                    compatibility_detail = (
                        f" 兼容清单确认已分类 {compatibility_classified}/"
                        f"{len(imported)} 个。"
                    )
                elif imported:
                    compatibility_detail = " 本次无可用兼容清单，无法复核 READY 状态。"
                discovery_detail = (
                    f"来源发现 {discovered_count} 个、规范化 {normalized_count} "
                    f"个、回执导入 {len(imported)} 个、跳过 {len(skipped)} 个、"
                    f"落盘 {present} 个。"
                )
                if raw_rejections:
                    discovery_detail += (
                        f" 另有 {raw_rejections} 个来源记录未进入可迁移"
                        "定义（例如已结束、已取消、损坏或规则无法"
                        "等价转换），仅保留在来源扫描告警中。"
                    )
                elif legacy_discovery_count:
                    discovery_detail += " 旧回执未记录来源发现总数。"
                checks.append(
                    _check(
                        "scheduled_tasks",
                        status,
                        "定时任务安全状态",
                        discovery_detail
                        + f" 唯一落盘 {unique}/{len(expected)} 个，审核门禁和"
                        f"禁用状态与兼容分区一致 {activation_safe}/"
                        f"{len(expected)} 个；远程或未验证工作区"
                        f"未绑定本机目录 {remote_unmapped}/"
                        f"{len(remote_expected)} 个。"
                        + compatibility_detail
                        + " 待修复任务仍需人工处理。",
                    ),
                )
            except Exception as exc:  # pylint: disable=broad-except
                checks.append(
                    _check(
                        "scheduled_tasks",
                        "fail",
                        "定时任务安全状态",
                        f"无法读取目标任务列表：{type(exc).__name__}: {exc}",
                    ),
                )

    checks.append(_receipt_check(context))
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
