# -*- coding: utf-8 -*-
"""Built-in ``/import`` and ``/export`` portability commands."""

from __future__ import annotations

import shlex
from pathlib import Path

from ....backup import export_backup
from ....backup.models import (
    BackupConflictError,
    BackupTrustMode,
    BackupValidationError,
)
from ....portability import (
    ImportReceipt,
    MigrationPlan,
    ProviderImportService,
    ProviderInventory,
    SourceLocation,
    export_to_backup,
    export_trace,
    import_backup_path,
    provider_names,
    resolve_source_location,
)
from .base import BaseControlCommandHandler, ControlContext

IMPORT_HELP = """\
**Import**

Import a QwenPaw backup ZIP or copy supported third-party Agent data into the
current QwenPaw Agent. Import never changes the current runtime backend and
never modifies source data.

**Commands**
- `/import from codex`
- `/import from qoder`
- `/import from codex --dry-run` - 只清点并生成迁移计划
- `/import apply <plan-id>` - 来源未变化时执行已确认的计划
- `/import inspect` - 查看本机如何定位 Codex/Qoder 数据目录
- `/import inspect codex --source-home "/custom/path"`
- `/import from "/path/to/backup.zip"`

**Backup trust/conflict options**
- `--trust-foreign` - explicitly trust a backup signed by another QwenPaw
- `--trust-legacy` - explicitly trust an older unsigned backup
- `--overwrite` - replace an existing backup with the same backup ID

Imported backup ZIPs are placed in the existing Backup library. Use the
existing Backup restore flow to inspect the scope and apply a restore safely.\
"""

EXPORT_HELP = """\
**Export**

**Commands**
- `/export to backup` - create a restorable QwenPaw backup for this Agent
- `/export to trace` - export all this Agent's sessions as redacted PawTrace

Backup export reuses the existing signed backup system and excludes the
global secrets directory. Trace export always applies local secret/PII/path
redaction and does not include hidden chain-of-thought.\
"""


def _tokens(raw: str) -> list[str]:
    try:
        return shlex.split(raw or "")
    except ValueError as exc:
        raise ValueError(f"Invalid command quoting: {exc}") from exc


def _trust_mode(flags: set[str]) -> BackupTrustMode | None:
    selected = flags & {"--trust-foreign", "--trust-legacy"}
    if len(selected) > 1:
        raise ValueError("Choose only one backup trust option.")
    if "--trust-foreign" in selected:
        return "foreign"
    if "--trust-legacy" in selected:
        return "legacy"
    return None


def _require_local_console(context: ControlContext) -> None:
    """Keep workspace-wide portability operations on the local admin path."""
    payload = context.payload
    if isinstance(payload, dict):
        channel = payload.get("channel")
    else:
        channel = getattr(payload, "channel", None)
    if not channel and context.channel is not None:
        channel = getattr(context.channel, "channel", None)
    if str(channel or "console").lower() != "console":
        raise PermissionError(
            "Data portability commands are restricted to the local "
            "Console/ACP channel.",
        )


def _provider_options(values: list[str]) -> tuple[bool, Path | None]:
    """Parse provider-only options without confusing a path for a flag."""
    dry_run = False
    source_home: Path | None = None
    index = 0
    while index < len(values):
        item = values[index]
        if item == "--dry-run":
            dry_run = True
            index += 1
            continue
        if item == "--source-home":
            if index + 1 >= len(values):
                raise ValueError("`--source-home` 后面需要填写数据目录。")
            if source_home is not None:
                raise ValueError("`--source-home` 只能指定一次。")
            source_home = Path(values[index + 1]).expanduser()
            index += 2
            continue
        if item.startswith("--source-home="):
            if source_home is not None:
                raise ValueError("`--source-home` 只能指定一次。")
            value = item.partition("=")[2].strip()
            if not value:
                raise ValueError("`--source-home` 后面需要填写数据目录。")
            source_home = Path(value).expanduser()
            index += 1
            continue
        if not item.startswith("--"):
            raise ValueError(f"Unexpected import argument(s): {item}")
        raise ValueError(f"未知迁移选项：{item}")
    return dry_run, source_home


def _render_location(location: SourceLocation) -> str:
    detected = "是" if location.data_home_exists else "否"
    lines = [
        f"- **{location.provider_id}**",
        f"  - 数据目录：`{location.data_home}`",
        f"  - 定位依据：`{location.data_home_source}`",
        f"  - 目录存在：{detected}",
    ]
    if location.user_data_home:
        lines.extend(
            [
                f"  - 编辑器数据目录：`{location.user_data_home}`",
                "  - 编辑器目录存在："
                + ("是" if location.user_data_home_exists else "否"),
            ],
        )
    if location.runtime_path:
        lines.append(f"  - 可执行程序：`{location.runtime_path}`")
    return "\n".join(lines)


def _render_inventory(inventory: ProviderInventory) -> str:
    location = inventory.source_location
    location_text = (
        _render_location(location)
        if location
        else (f"- 来源位置：`{inventory.locator or '未知'}`")
    )
    warnings = ""
    if inventory.warnings:
        warnings = "\n\n**扫描提示**\n" + "\n".join(
            f"- {item}" for item in inventory.warnings
        )
    return (
        f"**{inventory.provider_name} 迁移来源检查**\n\n"
        f"{location_text}\n\n"
        "**发现的内容**\n"
        f"- 存在可迁移内容：{'是' if inventory.detected else '否'}\n"
        f"- 会话：{len(inventory.sessions)}\n"
        f"- 排除的内部/子会话：{len(inventory.ignored_session_ids)}\n"
        f"- Skill：{len(inventory.skills)}\n"
        f"- MCP：{len(inventory.mcp_servers)}\n"
        f"- 长期记忆作用域：{len(inventory.memory_projects)}\n"
        f"- Marketplace：{len(inventory.marketplaces)}\n"
        f"- 插件：{len(inventory.plugins)}\n"
        f"- 定时任务：{len(inventory.scheduled_tasks)}"
        f"（来源记录 {inventory.discovered_scheduled_task_count}）\n"
        "- 已修改 QwenPaw：否"
        f"{warnings}"
    )


def _render_plan(plan: MigrationPlan) -> str:
    by_action: dict[str, int] = {}
    for action in plan.actions:
        by_action[action.action] = by_action.get(action.action, 0) + 1
    action_labels = {
        "import_history": "导入历史会话",
        "import_disabled": "禁用状态导入",
        "import_scoped": "按来源作用域导入",
        "native_install": "原生安装",
        "native_install_review": "原生安装后复核",
        "install_safe_adapter_disabled": "安装安全适配器（默认禁用）",
        "prepare_native_install": "准备插件来源，等待用户确认安装",
        "restore_source": "恢复来源",
        "record_only": "只记录出处",
        "already_present": "已存在",
        "conflict_keep_target": "冲突时保留 QwenPaw 版本",
        "skip": "跳过",
    }
    actions = (
        "\n".join(
            f"- {action_labels.get(name, name)}：{count}"
            for name, count in sorted(by_action.items())
        )
        or "- 没有可执行项"
    )
    warnings = ""
    if plan.warnings:
        warnings = "\n\n**扫描提示**\n" + "\n".join(
            f"- {item}" for item in plan.warnings
        )
    return (
        "**迁移预演完成（尚未导入）**\n\n"
        f"- 来源：`{plan.source}`\n"
        f"- 计划编号：`{plan.plan_id}`\n"
        f"- 会话：{plan.inventory_counts.get('sessions', 0)}\n"
        "- 排除的内部/子会话："
        f"{plan.inventory_counts.get('ignored_source_sessions', 0)}\n"
        f"- Skill：{plan.inventory_counts.get('skills', 0)}\n"
        f"- MCP：{plan.inventory_counts.get('mcp_servers', 0)}\n"
        f"- 长期记忆：{plan.inventory_counts.get('memory_scopes', 0)}\n"
        f"- 插件：{plan.inventory_counts.get('plugins', 0)}\n"
        f"- 定时任务：{plan.inventory_counts.get('scheduled_tasks', 0)}\n"
        f"- 需要人工复核：{plan.inventory_counts.get('manual_review', 0)}\n"
        f"- 暂不支持：{plan.inventory_counts.get('unsupported', 0)}\n\n"
        f"**计划动作**\n{actions}\n\n"
        "确认后执行："
        f"`/import apply {plan.plan_id}`"
        f"{warnings}"
    )


def _render_receipt(receipt: ImportReceipt) -> str:
    warnings = ""
    if receipt.warnings:
        warnings = "\n\n**注意事项**\n" + "\n".join(
            f"- {item}" for item in receipt.warnings
        )
    doctor = ""
    if receipt.doctor_report is not None:
        icons = {"pass": "✅", "warning": "⚠️", "fail": "❌"}
        rows = "\n".join(
            f"- {icons.get(item.status, '•')} **{item.title_zh}**："
            f"{item.detail_zh}"
            for item in receipt.doctor_report.checks
        )
        doctor = (
            "\n\n**迁移后体检（中文）**\n"
            f"- 总结：{receipt.doctor_report.summary_zh}\n"
            f"{rows}"
        )
    adaptation = ""
    if receipt.adaptation_counts:
        adaptation = (
            "\n  - 待迁移区："
            f"{receipt.adaptation_counts.get('migrate', 0)}"
            "；待修复区："
            f"{receipt.adaptation_counts.get('repair', 0)}"
            "；丢弃区："
            f"{receipt.adaptation_counts.get('discard', 0)}"
            "；安全暂存区："
            f"{receipt.adaptation_counts.get('staging', 0)}"
        )
    return (
        f"**{receipt.source.title()} 导入完成**\n\n"
        f"- 新增会话：{len(receipt.imported_sessions)}\n"
        f"- 已存在或跳过的会话：{len(receipt.skipped_sessions)}\n"
        "- 来源中排除的内部/子会话："
        f"{len(receipt.ignored_source_sessions)}\n"
        f"- 已归档内部执行轨迹：{len(receipt.archived_internal_sessions)}\n"
        f"- 导入 Skill：{len(receipt.imported_skills)}\n"
        f"- 跳过或隔离 Skill：{len(receipt.skipped_skills)}\n"
        f"- 导入 MCP：{len(receipt.imported_mcp_servers)}\n"
        f"- 跳过或不兼容 MCP：{len(receipt.skipped_mcp_servers)}\n"
        f"- 导入长期记忆作用域：{len(receipt.imported_memory_projects)}\n"
        f"- 恢复 Marketplace 来源：{len(receipt.restored_marketplaces)}\n"
        f"- 已验证、待用户确认安装的插件：{len(receipt.prepared_plugins)}\n"
        f"- 原生安装插件：{len(receipt.installed_plugins)}\n"
        "- 导入定时任务："
        f"{len(receipt.imported_scheduled_tasks)}\n"
        "- 跳过或需人工处理的定时任务："
        f"{len(receipt.skipped_scheduled_tasks)}\n"
        "- 来源中发现的定时任务记录："
        f"{receipt.discovered_scheduled_task_count}\n"
        f"- 自动兼容检查：{receipt.adaptation_status}{adaptation}\n"
        f"- 兼容迁移记录：`{receipt.adaptation_summary or '未生成'}`\n"
        f"- 迁移计划：`{receipt.plan_id or '即时计划'}`\n"
        f"- 迁移回执：`{receipt.migration_id}`"
        f"{doctor}{warnings}"
    )


class ImportCommandHandler(BaseControlCommandHandler):
    """Import a local backup archive or a supported Harness source."""

    command_name = "/import"
    description = "Import Codex/Qoder data or a QwenPaw backup ZIP"

    # pylint: disable-next=too-many-return-statements,too-many-branches
    async def handle(self, context: ControlContext) -> str:
        _require_local_console(context)
        raw = str(context.args.get("_raw_args") or "").strip()
        if raw.lower() in {"", "help", "-h", "--help"}:
            return IMPORT_HELP
        tokens = _tokens(raw)
        operation = tokens[0].lower()
        service = ProviderImportService(context.workspace)

        if operation == "inspect":
            if len(tokens) == 1:
                return "**本机迁移来源定位**\n\n" + "\n".join(
                    _render_location(resolve_source_location(name))
                    for name in provider_names()
                )
            source = tokens[1]
            dry_run, source_home = _provider_options(tokens[2:])
            if dry_run:
                raise ValueError("`inspect` 不需要 `--dry-run`。")
            inventory = await service.inspect(
                source,
                source_home=source_home,
                progress=context.report_progress,
            )
            return _render_inventory(inventory)

        if operation == "apply":
            if len(tokens) != 2:
                raise ValueError("用法：`/import apply <plan-id>`")
            receipt = await service.apply_plan(
                tokens[1],
                progress=context.report_progress,
            )
            return _render_receipt(receipt)

        if len(tokens) < 2 or operation != "from":
            return f"Usage: `/import from <source>`\n\n{IMPORT_HELP}"
        source = tokens[1]
        path = Path(source).expanduser()
        looks_like_path = (
            path.suffix.lower() == ".zip"
            or path.is_absolute()
            or "/" in source
            or "\\" in source
        )
        if looks_like_path:
            unexpected = [
                item for item in tokens[2:] if not item.startswith("--")
            ]
            if unexpected:
                raise ValueError(
                    "Unexpected import argument(s): " + ", ".join(unexpected),
                )
            flags = {item for item in tokens[2:] if item.startswith("--")}
            unknown = flags - {
                "--overwrite",
                "--trust-foreign",
                "--trust-legacy",
            }
            if unknown:
                raise ValueError(
                    "Unknown import option(s): " + ", ".join(sorted(unknown)),
                )
            try:
                meta = await import_backup_path(
                    path,
                    overwrite="--overwrite" in flags,
                    trust_mode=_trust_mode(flags),
                )
            except BackupConflictError as exc:
                return (
                    "**Backup import conflict**\n\n"
                    f"Backup `{exc.existing_meta.id}` already exists. The "
                    "source file was not modified.\n\nRetry only if you want "
                    "to replace it: `"
                    f"/import from {shlex.quote(source)} --overwrite`"
                )
            except BackupValidationError as exc:
                trust_hint = ""
                if exc.code == "backup_signature_mismatch":
                    trust_hint = "\n\nIf you trust its origin, retry with "
                    trust_hint += "`--trust-foreign`."
                elif exc.code == "backup_legacy_unsigned":
                    trust_hint = (
                        "\n\nIf you trust this unsigned legacy archive, "
                        "retry with `--trust-legacy`."
                    )
                return (
                    f"**Backup validation failed** (`{exc.code}`)\n\n"
                    f"{exc}{trust_hint}"
                )
            stored_path, _name = await export_backup(meta.id)
            return (
                "**Backup imported**\n\n"
                f"- Name: `{meta.name}`\n"
                f"- Backup ID: `{meta.id}`\n"
                f"- Stored file: `{stored_path}`\n"
                "- Source file: unchanged\n"
                "- Workspace restored: no\n\n"
                "Open the existing Backup page to inspect the archive scope "
                "and perform a controlled restore."
            )

        dry_run, source_home = _provider_options(tokens[2:])
        if dry_run:
            plan = await service.plan_from(
                source,
                source_home=source_home,
                progress=context.report_progress,
            )
            return _render_plan(plan)
        receipt = await service.import_from(
            source,
            source_home=source_home,
            progress=context.report_progress,
        )
        return _render_receipt(receipt)


class ExportCommandHandler(BaseControlCommandHandler):
    """Export only the supported backup and trace profiles."""

    command_name = "/export"
    description = "Export a QwenPaw backup or redacted PawTrace archive"

    async def handle(self, context: ControlContext) -> str:
        _require_local_console(context)
        raw = str(context.args.get("_raw_args") or "").strip()
        if raw.lower() in {"", "help", "-h", "--help"}:
            return EXPORT_HELP
        tokens = _tokens(raw)
        if len(tokens) != 2 or tokens[0].lower() != "to":
            return f"Usage: `/export to <backup|trace>`\n\n{EXPORT_HELP}"
        target = tokens[1].lower()
        if target == "backup":
            meta, path, name = await export_to_backup(context.workspace)
            return (
                "**Backup export complete**\n\n"
                f"- Name: `{name}`\n"
                f"- Backup ID: `{meta['id']}`\n"
                f"- File: `{path}`\n"
                "- Agent workspaces: current Agent\n"
                "- Global config: included\n"
                "- Skill Pool: included\n"
                "- Global secrets directory: excluded\n"
                "- Signature: local HMAC"
            )
        if target == "trace":
            result = await export_trace(context.workspace)
            return (
                "**Trace export complete**\n\n"
                f"- File: `{result.path}`\n"
                f"- Sessions: {result.session_count}\n"
                f"- Events: {result.event_count}\n"
                f"- Redactions: {result.redaction_count}\n"
                f"- Skipped/truncated fields: {result.skipped_count}\n"
                f"- Trace SHA-256: `{result.sha256}`\n"
                "- Hidden chain-of-thought: excluded"
            )
        supported = ", ".join(provider_names())
        raise ValueError(
            f"Unsupported export target {target!r}. Only `backup` and "
            f"`trace` are available. Import providers: {supported}.",
        )


__all__ = ["ExportCommandHandler", "ImportCommandHandler"]
