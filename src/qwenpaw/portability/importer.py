# -*- coding: utf-8 -*-
# pylint: disable=cell-var-from-loop
"""Additive, idempotent provider-to-QwenPaw import transaction."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..agents.skill_system import SkillService
from ..agents.skill_system.store import (
    get_workspace_skill_manifest_path,
    get_workspace_skills_dir,
)
from ..app.driver_config_service import DriverConfigService
from ..drivers.adapters.mcp_legacy_config import legacy_mcp_client_to_driver
from ..drivers.contracts import iter_credential_refs
from ..config.utils import get_plugins_dir
from ..plugins.loader import resolved_plugin_manifest_path
from ..plugins.marketplace_registry import ExternalMarketplaceRegistry
from ..utils.io_utils import (
    get_path_lock,
    run_async_to_completion,
    run_sync_io,
    unlink_async,
    write_json_atomic_async,
)
from .adaptation_loop import run_adaptation_loop
from .compatibility import mcp_inline_secret_risks
from .codex_plugin_adapter import (
    ADAPTER as CODEX_PLUGIN_ADAPTER,
    stage_codex_content_plugin,
)
from .doctor import run_migration_doctor
from .models import (
    ImportReceipt,
    MigrationDoctorReport,
    ProviderInventory,
)
from .providers import create_migration_provider
from .providers.base import (
    ProgressReporter,
    report_progress as _report,
    report_result,
)
from .qoder_plugin_adapter import stage_qoder_skill_plugin
from .scheduled_tasks import build_imported_job, imported_job_source
from .transaction_journal import ImportTransactionJournal
from .import_conversations import ConversationState, import_conversations
from .import_support import (
    _RegistrySnapshot,
    _bounded_memory,
    _bounded_session,
    _memory_import_root,
    _mcp_client_data,
    _progress_milestone,
    _remove_session_state,
    _replace_memory_project,
    _restore_memory_project,
    _restore_registry_file,
    _skill_zip,
    _snapshot_registry_file,
)
from .import_planning import ImportPlanningMixin

logger = logging.getLogger(__name__)

_GENERATED_PLUGIN_ADAPTERS = {
    CODEX_PLUGIN_ADAPTER,
    "qoder_skill_only_v1",
}


def _copy_tree(source: Path, target: Path) -> None:
    """Copy one rollback tree, replacing a stale snapshot if present."""
    shutil.rmtree(target, ignore_errors=True)
    shutil.copytree(source, target)


def _restore_skill(
    workspace_dir: Path,
    name: str,
    backup: Path,
    manifest_backup: Path | None,
) -> None:
    skill_dir = get_workspace_skills_dir(workspace_dir) / name
    shutil.rmtree(skill_dir, ignore_errors=True)
    shutil.copytree(backup, skill_dir)
    manifest = get_workspace_skill_manifest_path(workspace_dir)
    if manifest_backup is None:
        manifest.unlink(missing_ok=True)
    else:
        shutil.copy2(manifest_backup, manifest)


def _plugin_id(source: Path) -> str:
    """Read the only field needed to snapshot a force-installed plugin."""
    payload = json.loads(
        resolved_plugin_manifest_path(source).read_text(encoding="utf-8"),
    )
    plugin_id = str(payload.get("id") or "")
    if not plugin_id:
        raise ValueError("plugin.json has no id")
    return plugin_id


class ImportRollbackError(RuntimeError):
    """An import failed and one or more rollback actions also failed."""

    def __init__(
        self,
        failures: list[str],
        *,
        cancelled: bool,
    ) -> None:
        self.failures = tuple(failures)
        self.cancelled = cancelled
        super().__init__(
            "迁移回滚未完成，请人工检查并清理以下内容：" + "；".join(failures),
        )


async def _asset_items(
    values: list[Any],
    progress: ProgressReporter | None,
    asset_type: str,
    imported: list[str],
    zones: dict[str, str] | None = None,
    zone_prefix: str = "",
    enabled: bool | None = True,
) -> AsyncIterator[tuple[int, Any]]:
    async def report(item: Any) -> None:
        zone = (zones or {}).get(f"{zone_prefix}:{item.source_id}", "")
        present = (
            item.source_id in imported or getattr(item, "name", "") in imported
        )
        state = "succeeded" if present else "failed"
        active = None if enabled is None else enabled and zone == "migrate"
        await report_result(
            progress,
            "asset",
            asset_type,
            state,
            "-" if active is None or not present else int(active),
            item.source_id,
        )

    for index, item in enumerate(values, start=1):
        if index > 1:
            await report(values[index - 2])
        yield index, item
    if values:
        await report(values[-1])


async def _commit_mutation(
    operation: Awaitable[Any],
    commit: Callable[[Any], None],
) -> Any:
    async def run() -> Any:
        result = await operation
        commit(result)
        return result

    return await run_async_to_completion(run())


class ProviderImportService(ImportPlanningMixin):
    """Trusted writer coordinating provider inventory and QwenPaw stores."""

    def __init__(self, workspace: Any) -> None:
        """Bind the importer to one already-started Agent workspace."""
        self._workspace = workspace

    def _create_provider(self, source: str, **kwargs: Any) -> Any:
        """Keep provider construction local for stable integrations/mocks."""
        return create_migration_provider(source, self._workspace, **kwargs)

    # pylint: disable-next=R0912,R0915,R0914,W0640
    async def _apply(
        self,
        inventory: ProviderInventory,
        *,
        started_at: datetime,
        plan_id: str = "",
        progress: ProgressReporter | None = None,
        retry_of_migration_id: str = "",
    ) -> ImportReceipt:
        """Apply one fully inventoried source as a rollback-capable batch."""
        migration_id = f"migration-{uuid4().hex}"
        replace_existing = bool(retry_of_migration_id)
        warnings = list(inventory.warnings)
        sessions = [_bounded_session(item) for item in inventory.sessions]
        _bounded_memory(inventory.memory_projects)
        existing_chats = await self._workspace.chat_manager.list_chats(
            archived=None,
        )
        existing_by_source = {
            (
                str((chat.meta.get("portability") or {}).get("source")),
                str((chat.meta.get("portability") or {}).get("source_id")),
            ): chat
            for chat in existing_chats
        }

        conversations = ConversationState()
        imported_sessions = conversations.imported
        skipped_sessions = conversations.skipped
        archived_internal_sessions = conversations.archived_internal
        imported_skills: list[str] = []
        skipped_skills: list[str] = []
        imported_mcp_servers: list[str] = []
        skipped_mcp_servers: list[str] = []
        imported_memory_projects: list[str] = []
        skipped_memory_projects: list[str] = []
        restored_marketplaces: list[str] = []
        skipped_marketplaces: list[str] = []
        prepared_plugins: list[str] = []
        installed_plugins: list[str] = []
        installed_plugin_paths: dict[str, Path] = {}
        skipped_plugins: list[str] = []
        imported_scheduled_tasks: list[str] = []
        skipped_scheduled_tasks: list[str] = []
        adaptation_status = "not_run"
        adaptation_manifest = ""
        adaptation_summary = ""
        adaptation_counts: dict[str, int] = {}
        adaptation_asset_zones: dict[str, str] = {}
        plugin_app = None
        created_chats = conversations.created_chats
        created_states = conversations.created_states
        patched_project_dirs = conversations.patched_project_dirs
        archived_chats = conversations.archived_chats
        created_mcp: list[tuple[str, str]] = []
        replaced_mcp: list[tuple[Any, list[Any], str]] = []
        replaced_skills: dict[str, tuple[Path, Path | None]] = {}
        replaced_plugins: dict[str, Path] = {}
        created_scheduled_tasks: list[str] = []
        replaced_scheduled_tasks: list[Any] = []
        memory_changes: list[tuple[Path, dict[Path, bytes] | None]] = []
        receipt_path: Path | None = None
        marketplace_registry_snapshot: _RegistrySnapshot | None = None
        marketplace_registry_touched = False
        adaptation_root = (
            Path(self._workspace.workspace_dir)
            / ".qwenpaw"
            / "imports"
            / migration_id
        )
        rollback_root = adaptation_root / ".rollback"
        skill_service = SkillService(self._workspace.workspace_dir)
        driver_config = DriverConfigService(self._workspace)
        transaction = (
            ImportTransactionJournal(
                Path(self._workspace.workspace_dir),
                plan_id,
            )
            if plan_id
            else None
        )

        async def watch(target: Path) -> None:
            if transaction is not None:
                await transaction.watch(target)

        async def restore_mcp(
            card: Any,
            credentials: list[Any],
            replacement_ref: str,
        ) -> None:
            await driver_config.card_store.delete(card.name)
            if replacement_ref:
                await driver_config.credential_store.delete(replacement_ref)
            for credential in credentials:
                await driver_config.credential_store.put(credential)
            await driver_config.save_card(card, reload_driver=card.enabled)

        async def restore_plugin(plugin_id: str, backup: Path) -> None:
            # pylint: disable-next=C0415
            from ..app.routers.plugins import (
                install_plugin_source,
                uninstall_plugin_source,
            )

            loader = plugin_app.state.plugin_loader
            if loader.get_loaded_plugin(plugin_id) is not None:
                await uninstall_plugin_source(
                    plugin_id,
                    app=plugin_app,
                    reload_agents=False,
                )
            else:
                await run_sync_io(
                    shutil.rmtree,
                    get_plugins_dir() / plugin_id,
                    True,
                )
            await install_plugin_source(
                str(backup),
                app=plugin_app,
                force=True,
                reload_agents=False,
            )

        try:
            if transaction is not None:
                await transaction.begin()
            targets: list[Path] = []
            workspace_root = Path(self._workspace.workspace_dir)
            if inventory.sessions:
                targets += [
                    workspace_root / "chats.json",
                    workspace_root / "sessions",
                ]
            if inventory.skills:
                targets += [
                    workspace_root / "skills",
                    workspace_root / "skill.json",
                ]
            if inventory.mcp_servers:
                targets += [
                    driver_config.cards_dir,
                    getattr(
                        driver_config.credential_store,
                        "_path",
                        workspace_root / "credentials.yaml",
                    ),
                ]
            if inventory.memory_projects:
                targets.append(
                    _memory_import_root(
                        self._workspace,
                        inventory.provider_id,
                    ),
                )
            if inventory.scheduled_tasks:
                targets += [
                    workspace_root / "jobs.json",
                    workspace_root / "jobs_history",
                ]
            for target in targets:
                await watch(target)

            await run_async_to_completion(
                import_conversations(
                    self._workspace,
                    inventory,
                    sessions,
                    existing_by_source,
                    warnings,
                    started_at,
                    progress,
                    conversations,
                ),
            )

            try:
                adaptation = await run_adaptation_loop(
                    self._workspace,
                    inventory,
                    migration_id,
                    progress,
                )
                adaptation_status = adaptation.status
                adaptation_counts = dict(adaptation.counts)
                adaptation_asset_zones = dict(adaptation.asset_zones)
                try:
                    adaptation_manifest = str(
                        adaptation.manifest_path.relative_to(
                            Path(self._workspace.workspace_dir),
                        ),
                    )
                except ValueError:
                    adaptation_manifest = str(adaptation.manifest_path)
                try:
                    adaptation_summary = str(
                        adaptation.summary_path.relative_to(
                            Path(self._workspace.workspace_dir),
                        ),
                    )
                except ValueError:
                    adaptation_summary = str(adaptation.summary_path)
                warnings.extend(adaptation.warnings)
            except Exception as exc:  # pylint: disable=broad-except
                logger.exception("Portability adaptation loop failed")
                adaptation_status = "failed_safe"
                warnings.append(
                    "工具和设置自动兼容 Loop 运行失败；迁移将继续，并保持"
                    "相关资产禁用："
                    f"{type(exc).__name__}: {exc}",
                )

            registry_path = getattr(
                self._workspace,
                "marketplace_registry_path",
                None,
            )
            marketplace_registry = ExternalMarketplaceRegistry(registry_path)
            if inventory.marketplaces:
                await watch(marketplace_registry.path)
                registry_file = marketplace_registry.path
                marketplace_registry_snapshot = _RegistrySnapshot(
                    path=registry_file,
                    content=await run_sync_io(
                        _snapshot_registry_file,
                        registry_file,
                    ),
                )
            marketplace_total = len(inventory.marketplaces)
            for marketplace_index, marketplace in enumerate(
                inventory.marketplaces,
                start=1,
            ):
                if _progress_milestone(marketplace_index, marketplace_total):
                    await _report(
                        progress,
                        "正在恢复插件 Marketplace 来源："
                        f"{marketplace_index}/{marketplace_total}",
                    )

                def record_marketplace(result: Any) -> None:
                    nonlocal marketplace_registry_touched
                    marketplace_registry_touched |= bool(result[0])

                (
                    changed,
                    credentials_removed,
                ) = await _commit_mutation(
                    marketplace_registry.register(
                        provider=inventory.provider_id,
                        source_id=marketplace.source_id,
                        name=marketplace.name,
                        source=marketplace.source,
                        source_type=marketplace.source_type,
                        ref_name=marketplace.ref_name,
                    ),
                    record_marketplace,
                )
                if credentials_removed:
                    warnings.append(
                        f"Marketplace {marketplace.name!r} contained URL "
                        "credentials/query parameters; they were removed and "
                        "must be configured again.",
                    )
                if not marketplace.source:
                    skipped_marketplaces.append(marketplace.name)
                    warnings.append(
                        f"Marketplace {marketplace.name!r} is built-in or its "
                        "independent source is unavailable. Its provenance "
                        "was recorded, but no source checkout was copied.",
                    )
                elif changed:
                    restored_marketplaces.append(marketplace.name)
                else:
                    skipped_marketplaces.append(marketplace.name)

            installable_plugins = [
                plugin
                for plugin in inventory.plugins
                if (
                    adaptation_asset_zones.get(f"plugins:{plugin.source_id}")
                    == "migrate"
                    or (
                        adaptation_asset_zones.get(
                            f"plugins:{plugin.source_id}",
                        )
                        == "repair"
                        and plugin.metadata.get("adapter")
                        in _GENERATED_PLUGIN_ADAPTERS
                    )
                )
            ]
            if installable_plugins:
                try:
                    # pylint: disable-next=C0415
                    from ..plugins.registry import (
                        PluginRegistry,
                    )

                    plugin_app = PluginRegistry().get_plugin_http_app()
                except Exception:  # pylint: disable=broad-except
                    logger.debug(
                        "Native plugin app is unavailable during import",
                        exc_info=True,
                    )
            plugin_total = len(inventory.plugins)
            async for plugin_index, plugin in _asset_items(
                inventory.plugins,
                progress,
                "plugin",
                installed_plugins,
                adaptation_asset_zones,
                "plugins",
            ):
                if _progress_milestone(plugin_index, plugin_total):
                    await _report(
                        progress,
                        "正在通过 QwenPaw 原生流程安装兼容插件："
                        f"{plugin_index}/{plugin_total}",
                    )
                if not plugin.install_source:
                    skipped_plugins.append(plugin.source_id)
                    warnings.append(
                        f"Plugin {plugin.source_id!r} has no independent "
                        "QwenPaw-compatible install source. Its installed "
                        f"{inventory.provider_name} cache was not copied; "
                        "portable Skills/MCP are handled separately.",
                    )
                    continue
                compatibility_zone = adaptation_asset_zones.get(
                    f"plugins:{plugin.source_id}",
                    "failed_safe",
                )
                if compatibility_zone not in {"migrate", "repair"}:
                    skipped_plugins.append(plugin.source_id)
                    warnings.append(
                        f"Plugin {plugin.source_id!r} was not installed: "
                        f"compatibility zone is {compatibility_zone!r}. "
                        "Its Marketplace provenance remains available for "
                        "manual review.",
                    )
                    continue
                if (
                    compatibility_zone == "repair"
                    and plugin.metadata.get("adapter")
                    not in _GENERATED_PLUGIN_ADAPTERS
                ):
                    prepared_plugins.append(plugin.source_id)
                    warnings.append(
                        f"Plugin {plugin.source_id!r} remains in the repair "
                        "zone. Its source was preserved but executable code "
                        "was not loaded.",
                    )
                    continue
                if plugin_app is None:
                    skipped_plugins.append(plugin.source_id)
                    warnings.append(
                        f"Plugin {plugin.source_id!r} is compatible, but the "
                        "QwenPaw native plugin loader is not ready. Retry "
                        "/import after startup completes.",
                    )
                    continue
                staged_plugin: Path | None = None
                plugin_backup: tuple[str, Path] | None = None
                try:
                    # pylint: disable-next=C0415
                    from ..app.routers.plugins import (
                        install_plugin_source,
                    )

                    install_source = plugin.install_source
                    if plugin.metadata.get("adapter") == "qoder_skill_only_v1":
                        staged_plugin = await run_sync_io(
                            stage_qoder_skill_plugin,
                            plugin,
                            enabled=compatibility_zone == "migrate",
                        )
                        install_source = str(staged_plugin)
                    elif (
                        plugin.metadata.get("adapter") == CODEX_PLUGIN_ADAPTER
                    ):
                        staged_plugin = await run_sync_io(
                            stage_codex_content_plugin,
                            plugin,
                            enabled=compatibility_zone == "migrate",
                        )
                        install_source = str(staged_plugin)
                    source_path = Path(install_source).resolve()
                    plugin_id = _plugin_id(source_path)
                    existing_path = get_plugins_dir() / plugin_id
                    await watch(existing_path)
                    if replace_existing and existing_path.is_dir():
                        backup = rollback_root / "plugins" / plugin_id
                        await run_sync_io(_copy_tree, existing_path, backup)
                        plugin_backup = (plugin_id, backup)
                        replaced_plugins.setdefault(*plugin_backup)
                    record = await _commit_mutation(
                        install_plugin_source(
                            install_source,
                            app=plugin_app,
                            force=replace_existing,
                            reload_agents=False,
                        ),
                        lambda value: installed_plugins.append(
                            value.manifest.id,
                        ),
                    )
                    source_path = getattr(record, "source_path", None)
                    if source_path is not None:
                        installed_plugin_paths[plugin.source_id] = Path(
                            source_path,
                        ).resolve()
                    if staged_plugin is not None:
                        warnings.append(
                            f"Adapted content plugin {plugin.source_id!r} "
                            "into a QwenPaw native wrapper; its Skills are "
                            + (
                                "enabled."
                                if compatibility_zone == "migrate"
                                else "installed disabled for further repair."
                            ),
                        )
                except Exception as exc:  # pylint: disable=broad-except
                    if plugin_backup is not None:
                        try:
                            await restore_plugin(*plugin_backup)
                            replaced_plugins.pop(plugin_backup[0], None)
                        except (
                            Exception
                        ) as restore_exc:  # pylint: disable=broad-except
                            raise RuntimeError(
                                "plugin replacement failed; "
                                "restoration failed",
                            ) from restore_exc
                    skipped_plugins.append(plugin.source_id)
                    warnings.append(
                        f"Plugin {plugin.source_id!r} failed native "
                        f"installation: {type(exc).__name__}: {exc}",
                    )
                finally:
                    if staged_plugin is not None:
                        await run_sync_io(
                            shutil.rmtree,
                            staged_plugin.parent,
                            True,
                        )
            if installed_plugins:
                warnings.append(
                    "兼容流程批准的插件已通过 QwenPaw 原生安装流程"
                    "写入；外部内容插件使用 QwenPaw 生成的原生包装器。"
                    "需要时请在迁移后重载智能体。",
                )

            memory_total = len(inventory.memory_projects)
            async for memory_index, project in _asset_items(
                inventory.memory_projects,
                progress,
                "memory",
                imported_memory_projects,
                enabled=None,
            ):
                if _progress_milestone(memory_index, memory_total):
                    await _report(
                        progress,
                        "正在按项目作用域迁移长期 Memory："
                        f"{memory_index}/{memory_total}",
                    )
                try:

                    def record_memory(result: Any) -> None:
                        target, previous, changed = result
                        if changed or replace_existing:
                            memory_changes.append((target, previous))
                            imported_memory_projects.append(project.source_id)
                        else:
                            skipped_memory_projects.append(project.source_id)

                    await _commit_mutation(
                        run_sync_io(
                            _replace_memory_project,
                            self._workspace,
                            inventory.provider_id,
                            project,
                        ),
                        record_memory,
                    )
                except Exception as exc:  # pylint: disable=broad-except
                    skipped_memory_projects.append(project.source_id)
                    warnings.append(
                        f"Memory project {project.project_key!r} was "
                        f"quarantined/skipped: {type(exc).__name__}: {exc}",
                    )

            skill_total = len(inventory.skills)
            async for skill_index, skill in _asset_items(
                inventory.skills,
                progress,
                "skill",
                imported_skills,
                adaptation_asset_zones,
                "skills",
            ):
                if _progress_milestone(skill_index, skill_total):
                    await _report(
                        progress,
                        f"正在安全检查并暂存 Skill：" f"{skill_index}/{skill_total}",
                    )
                compatibility_zone = adaptation_asset_zones.get(
                    f"skills:{skill.source_id}",
                    "failed_safe",
                )
                if compatibility_zone not in {"migrate", "repair"}:
                    skipped_skills.append(skill.name)
                    warnings.append(
                        f"Skill {skill.name!r} 未写入 QwenPaw：兼容状态为 "
                        f"{compatibility_zone!r}；源文件仍保留在兼容清单/"
                        "隔离暂存区，可修复后重试。",
                    )
                    continue
                skill_backup: tuple[Path, Path | None] | None = None
                try:
                    data = await run_sync_io(_skill_zip, skill)
                    result = await _commit_mutation(
                        run_sync_io(
                            skill_service.import_from_zip,
                            data,
                            compatibility_zone == "migrate",
                        ),
                        lambda value: imported_skills.extend(
                            str(name) for name in value.get("imported", [])
                        ),
                    )
                    names = [str(name) for name in result.get("imported", [])]
                    if (
                        not names
                        and result.get("conflicts")
                        and replace_existing
                    ):
                        skill_dir = (
                            get_workspace_skills_dir(
                                self._workspace.workspace_dir,
                            )
                            / skill.name
                        )
                        if not skill_dir.is_dir():
                            raise RuntimeError(
                                "conflicting Skill files are missing",
                            )
                        backup = rollback_root / "skills" / skill.name
                        await run_sync_io(_copy_tree, skill_dir, backup)
                        manifest = get_workspace_skill_manifest_path(
                            self._workspace.workspace_dir,
                        )
                        manifest_backup = (
                            rollback_root / "skill.json"
                            if manifest.is_file()
                            else None
                        )
                        if manifest_backup is not None:
                            await run_sync_io(
                                shutil.copy2,
                                manifest,
                                manifest_backup,
                            )
                        skill_backup = (backup, manifest_backup)
                        replaced_skills.setdefault(skill.name, skill_backup)
                        skill_service.disable_skill(skill.name)
                        await run_sync_io(
                            skill_service.delete_skill,
                            skill.name,
                        )
                        result = await _commit_mutation(
                            run_sync_io(
                                skill_service.import_from_zip,
                                data,
                                compatibility_zone == "migrate",
                            ),
                            lambda value: imported_skills.extend(
                                str(name) for name in value.get("imported", [])
                            ),
                        )
                        names = [
                            str(name) for name in result.get("imported", [])
                        ]
                        if not names:
                            await run_sync_io(
                                _restore_skill,
                                self._workspace.workspace_dir,
                                skill.name,
                                *skill_backup,
                            )
                            skill_backup = None
                            replaced_skills.pop(skill.name, None)
                    if not names:
                        skipped_skills.append(skill.name)
                        if result.get("conflicts"):
                            warnings.append(
                                f"Skill {skill.name!r} already exists; kept "
                                "the QwenPaw copy.",
                            )
                except Exception as exc:  # pylint: disable=broad-except
                    if skill_backup is not None:
                        try:
                            await run_sync_io(
                                _restore_skill,
                                self._workspace.workspace_dir,
                                skill.name,
                                *skill_backup,
                            )
                            replaced_skills.pop(skill.name, None)
                        except (
                            Exception
                        ) as restore_exc:  # pylint: disable=broad-except
                            raise RuntimeError(
                                "Skill replacement failed; restoration failed",
                            ) from restore_exc
                    skipped_skills.append(skill.name)
                    warnings.append(
                        f"Skill {skill.name!r} was quarantined/skipped: {exc}",
                    )

            existing_cards = {
                card.name: card for card in await driver_config.list_cards()
            }
            existing_driver_names = set(existing_cards)
            mcp_total = len(inventory.mcp_servers)
            async for mcp_index, server in _asset_items(
                inventory.mcp_servers,
                progress,
                "mcp",
                imported_mcp_servers,
                adaptation_asset_zones,
                "mcp",
            ):
                if _progress_milestone(mcp_index, mcp_total):
                    await _report(
                        progress,
                        f"正在转换并加密保存 MCP：{mcp_index}/{mcp_total}",
                    )
                compatibility_zone = adaptation_asset_zones.get(
                    f"mcp:{server.source_id}",
                    "failed_safe",
                )
                if compatibility_zone not in {"migrate", "repair"}:
                    skipped_mcp_servers.append(server.name)
                    warnings.append(
                        f"MCP {server.name!r} 未写入 DriverCard：兼容状态"
                        f"为 {compatibility_zone!r}；请根据兼容清单修复"
                        "后重试。",
                    )
                    continue
                if (
                    server.name in existing_driver_names
                    and not replace_existing
                ):
                    skipped_mcp_servers.append(server.name)
                    warnings.append(
                        f"MCP {server.name!r} conflicts with an existing "
                        "QwenPaw Driver; kept the QwenPaw copy.",
                    )
                    continue
                if server.transport not in {
                    "stdio",
                    "streamable_http",
                    "sse",
                }:
                    skipped_mcp_servers.append(server.name)
                    warnings.append(
                        f"MCP {server.name!r} uses unsupported transport "
                        f"{server.transport!r} and was skipped.",
                    )
                    continue
                inline_secret_risks = mcp_inline_secret_risks(
                    server.command,
                    server.args,
                    server.url,
                    server.env,
                    server.headers,
                    server.cwd,
                )
                if inline_secret_risks:
                    skipped_mcp_servers.append(server.name)
                    warnings.append(
                        f"MCP {server.name!r} 的命令参数或 URL 可能包含"
                        "无法安全绑定的明文凭据，已拒绝写入 DriverCard；"
                        "请改用环境变量/请求头凭据或在 QwenPaw 中重新配置。",
                    )
                    continue
                credential_ref = ""
                replacement: tuple[Any, list[Any]] | None = None
                card_written = False
                try:
                    if (
                        replace_existing
                        and (previous_card := existing_cards.get(server.name))
                        is not None
                    ):
                        previous_credentials = []
                        for reference in iter_credential_refs(
                            previous_card,
                        ).values():
                            credential = (
                                await driver_config.load_optional_credential(
                                    reference.ref,
                                )
                            )
                            if (
                                credential is not None
                                and not credential.ref.startswith("env:")
                            ):
                                previous_credentials.append(credential)
                        replacement = (previous_card, previous_credentials)
                    translated_server = server
                    relative_cwd = str(
                        server.metadata.get("source_plugin_relative_cwd")
                        or "",
                    )
                    if relative_cwd:
                        plugin_id = str(
                            server.metadata.get("source_plugin") or "",
                        )
                        plugin_root = installed_plugin_paths.get(plugin_id)
                        relative = Path(relative_cwd)
                        if (
                            plugin_root is None
                            or relative.is_absolute()
                            or ".." in relative.parts
                        ):
                            raise ValueError(
                                "plugin-owned MCP has no safe installed root",
                            )
                        cwd = (plugin_root / relative).resolve()
                        if not cwd.is_dir() or not cwd.is_relative_to(
                            plugin_root,
                        ):
                            raise ValueError(
                                "plugin-owned MCP directory is missing",
                            )
                        translated_server = server.model_copy(
                            update={"cwd": str(cwd)},
                        )
                    card, credential = legacy_mcp_client_to_driver(
                        server.name,
                        _mcp_client_data(translated_server),
                        force_encrypt_bindings=True,
                    )
                    card.enabled = False
                    card.config = {
                        **dict(card.config),
                        "migration_source": inventory.provider_id,
                        "migration_source_id": server.source_id,
                        "source_enabled": server.enabled,
                        "requires_review": True,
                        "auth_status": server.auth_status,
                        "source_runtime_bound": bool(
                            server.metadata.get("source_runtime_bound"),
                        ),
                    }

                    async def save_mcp() -> None:
                        nonlocal card_written, credential_ref
                        if credential is not None:
                            await driver_config.credential_store.put(
                                credential,
                            )
                            credential_ref = credential.ref
                        await driver_config.save_card(
                            card,
                            reload_driver=False,
                        )
                        card_written = True
                        if replacement is None:
                            created_mcp.append((card.name, credential_ref))
                        else:
                            replaced_mcp.append(
                                (*replacement, credential_ref),
                            )

                    await run_async_to_completion(save_mcp())
                    existing_driver_names.add(card.name)
                    existing_cards[card.name] = card
                    imported_mcp_servers.append(card.name)
                    if server.metadata.get("source_runtime_bound"):
                        warnings.append(
                            f"MCP {server.name!r} references Codex/ChatGPT "
                            "runtime files. Its disabled card was preserved "
                            "for review, but it may stop working if that "
                            "source plugin/runtime is removed.",
                        )
                    if server.auth_status not in {"", "unsupported"}:
                        warnings.append(
                            f"MCP {server.name!r} authentication state was "
                            "not copied; authorize it again before enabling.",
                        )
                except Exception as exc:  # pylint: disable=broad-except
                    try:
                        if replacement is not None:
                            await restore_mcp(
                                *replacement,
                                credential_ref,
                            )
                        else:
                            if card_written:
                                await driver_config.card_store.delete(
                                    server.name,
                                )
                            if credential_ref:
                                await driver_config.credential_store.delete(
                                    credential_ref,
                                )
                    except (
                        Exception
                    ) as restore_exc:  # pylint: disable=broad-except
                        raise RuntimeError(
                            "MCP replacement failed and could not be restored",
                        ) from restore_exc
                    skipped_mcp_servers.append(server.name)
                    warnings.append(
                        f"MCP {server.name!r} could not be translated and "
                        f"was skipped: {type(exc).__name__}: {exc}",
                    )

            cron_manager = getattr(self._workspace, "cron_manager", None)
            existing_task_jobs: dict[tuple[str, str], Any] = {}
            if cron_manager is not None:
                try:
                    existing_task_jobs = {
                        key: job
                        for job in await cron_manager.list_jobs()
                        if (key := imported_job_source(job)) is not None
                    }
                except Exception as exc:  # pylint: disable=broad-except
                    warnings.append(
                        "无法读取 QwenPaw 定时任务列表；本次定时任务迁移已"
                        f"安全跳过：{type(exc).__name__}: {exc}",
                    )
                    cron_manager = None
            task_total = len(inventory.scheduled_tasks)
            async for task_index, task in _asset_items(
                inventory.scheduled_tasks,
                progress,
                "cron",
                imported_scheduled_tasks,
                adaptation_asset_zones,
                "scheduled_tasks",
                False,
            ):
                if _progress_milestone(task_index, task_total):
                    await _report(
                        progress,
                        "正在转换定时任务模板（默认禁用）：" f"{task_index}/{task_total}",
                    )
                compatibility_zone = adaptation_asset_zones.get(
                    f"scheduled_tasks:{task.source_id}",
                    "failed_safe",
                )
                if compatibility_zone not in {"migrate", "repair"}:
                    skipped_scheduled_tasks.append(task.source_id)
                    warnings.append(
                        f"定时任务 {task.name!r} 未写入 Cron：兼容状态"
                        f"为 {compatibility_zone!r}；它只保留在兼容清单"
                        "中，不会被“立即运行”绕过。",
                    )
                    continue
                key = (inventory.provider_id, task.source_id)
                existing_task = existing_task_jobs.get(key)
                repairing_invalid_task = False
                if existing_task is not None and replace_existing:
                    repairing_invalid_task = True
                if existing_task is not None and not replace_existing:
                    validator = getattr(
                        cron_manager,
                        "validate_job_spec",
                        None,
                    )
                    if not callable(validator):
                        skipped_scheduled_tasks.append(task.source_id)
                        continue
                    try:
                        validator(existing_task)
                    except Exception as exc:  # pylint: disable=broad-except
                        portability = getattr(existing_task, "meta", {}).get(
                            "portability",
                            {},
                        )
                        review_checker = getattr(
                            cron_manager,
                            "requires_portability_review",
                            None,
                        )
                        still_awaiting_review = (
                            bool(review_checker(existing_task))
                            if callable(review_checker)
                            else bool(
                                isinstance(portability, dict)
                                and (
                                    portability.get("requires_review") is True
                                    or portability.get("safety")
                                    == "disabled_until_explicit_promotion"
                                ),
                            )
                        )
                        if not still_awaiting_review:
                            skipped_scheduled_tasks.append(task.source_id)
                            warnings.append(
                                f"已有定时任务 {task.name!r} 无法注册，但"
                                "它已经过人工处理，本次不会覆盖："
                                f"{type(exc).__name__}: {exc}",
                            )
                            continue
                        repairing_invalid_task = True
                        warnings.append(
                            f"检测到旧版留下的无效定时任务 "
                            f"{task.name!r}；将用已通过兼容检查的禁用"
                            "模板修复，不会执行任务。",
                        )
                    else:
                        review_checker = getattr(
                            cron_manager,
                            "requires_portability_review",
                            None,
                        )
                        normalizer = getattr(
                            cron_manager,
                            "canonicalize_imported_job_for_review",
                            None,
                        )
                        if (
                            callable(review_checker)
                            and review_checker(existing_task)
                            and callable(normalizer)
                        ):
                            normalized_existing = normalizer(existing_task)
                            if normalized_existing != existing_task:

                                async def normalize_task() -> None:
                                    await cron_manager.create_or_replace_job(
                                        normalized_existing,
                                    )
                                    replaced_scheduled_tasks.append(
                                        existing_task,
                                    )
                                    existing_task_jobs[
                                        key
                                    ] = normalized_existing

                                await run_async_to_completion(normalize_task())
                                warnings.append(
                                    f"定时任务 {task.name!r} 的旧版迁移审核"
                                    "门禁已补齐，任务保持禁用。",
                                )
                        skipped_scheduled_tasks.append(task.source_id)
                        continue
                if cron_manager is None:
                    skipped_scheduled_tasks.append(task.source_id)
                    warnings.append(
                        f"定时任务 {task.name!r} 未写入：目标智能体的 Cron "
                        "服务尚未初始化。聊天记录和其他资产不受影响。",
                    )
                    continue
                try:
                    target_session_id = ""
                    target_user_id = "cron"
                    source_kind = str(
                        task.metadata.get("source_kind") or "",
                    ).lower()
                    if source_kind == "heartbeat":
                        target_thread_id = str(
                            task.metadata.get("target_thread_id") or "",
                        )
                        target_chat = existing_by_source.get(
                            (inventory.provider_id, target_thread_id),
                        )
                        if target_chat is None:
                            raise ValueError(
                                "Codex heartbeat 的目标会话未迁移，不能安全" + "绑定到其他会话。",
                            )
                        target_session_id = target_chat.session_id
                        target_user_id = target_chat.user_id
                    job = build_imported_job(
                        inventory.provider_id,
                        task,
                        target_user_id=target_user_id,
                        target_session_id=target_session_id,
                        reviewed=compatibility_zone == "migrate",
                    )
                    if source_kind == "heartbeat":
                        job.runtime = job.runtime.model_copy(
                            update={"share_session": True},
                        )
                    if repairing_invalid_task:
                        existing_id = getattr(existing_task, "id", None)
                        if existing_id:
                            job = job.model_copy(update={"id": existing_id})
                    assert job.id is not None
                    job_id = job.id

                    async def save_task() -> None:
                        await cron_manager.create_or_replace_job(job)
                        if repairing_invalid_task:
                            replaced_scheduled_tasks.append(existing_task)
                        else:
                            created_scheduled_tasks.append(job_id)
                        imported_scheduled_tasks.append(task.source_id)
                        existing_task_jobs[key] = job

                    await run_async_to_completion(save_task())
                except Exception as exc:  # pylint: disable=broad-except
                    skipped_scheduled_tasks.append(task.source_id)
                    warnings.append(
                        f"定时任务 {task.name!r} 已保留在迁移清单中，但未"
                        f"启用或写入：{type(exc).__name__}: {exc}",
                    )

            completed_at = datetime.now(timezone.utc)
            await _report(progress, "正在生成迁移回执并完成一致性检查…")
            receipt = ImportReceipt(
                migration_id=migration_id,
                plan_id=plan_id,
                source=inventory.provider_id,
                source_locator=inventory.locator,
                agent_id=self._workspace.agent_id,
                started_at=started_at,
                completed_at=completed_at,
                imported_sessions=imported_sessions,
                skipped_sessions=skipped_sessions,
                ignored_source_sessions=list(inventory.ignored_session_ids),
                archived_internal_sessions=archived_internal_sessions,
                imported_skills=imported_skills,
                skipped_skills=skipped_skills,
                imported_mcp_servers=imported_mcp_servers,
                skipped_mcp_servers=skipped_mcp_servers,
                imported_memory_projects=imported_memory_projects,
                skipped_memory_projects=skipped_memory_projects,
                restored_marketplaces=restored_marketplaces,
                skipped_marketplaces=skipped_marketplaces,
                prepared_plugins=prepared_plugins,
                installed_plugins=installed_plugins,
                skipped_plugins=skipped_plugins,
                imported_scheduled_tasks=imported_scheduled_tasks,
                skipped_scheduled_tasks=skipped_scheduled_tasks,
                discovered_mcp_count=inventory.discovered_mcp_count,
                discovered_scheduled_task_count=(
                    inventory.discovered_scheduled_task_count
                ),
                adaptation_status=adaptation_status,
                adaptation_manifest=adaptation_manifest,
                adaptation_summary=adaptation_summary,
                adaptation_counts=adaptation_counts,
                retry_of_migration_id=retry_of_migration_id,
                warnings=warnings,
            )
            receipt_dir = Path(self._workspace.workspace_dir) / ".qwenpaw"
            receipt_dir = receipt_dir / "imports"
            receipt_dir.mkdir(parents=True, exist_ok=True)
            receipt_path = receipt_dir / f"{migration_id}.json"
            await write_json_atomic_async(
                receipt_path,
                receipt.model_dump(mode="json"),
                sort_keys=True,
                new_file_mode=0o600,
            )
            await _report(progress, "正在执行迁移后体检…")
            try:
                receipt.doctor_report = await run_migration_doctor(
                    self._workspace,
                    inventory,
                    receipt,
                )
            except Exception as exc:  # pylint: disable=broad-except
                logger.exception("Migration Doctor failed")
                warnings.append(
                    "迁移内容已写入，但自动体检运行失败：" f"{type(exc).__name__}: {exc}",
                )
                receipt.warnings = list(warnings)
                receipt.doctor_report = MigrationDoctorReport(
                    status="fail",
                    summary_zh="迁移已完成，但自动体检未能正常运行，请查看日志。",
                    checked_at=datetime.now(timezone.utc),
                    checks=[],
                )
            await write_json_atomic_async(
                receipt_path,
                receipt.model_dump(mode="json"),
                sort_keys=True,
                new_file_mode=0o600,
            )
            await _report(progress, "迁移事务已安全提交。")
            await run_sync_io(shutil.rmtree, rollback_root, True)
            return receipt
        except BaseException as original:
            rollback_failures: list[str] = []

            def record_rollback_failure(label: str, exc: Exception) -> None:
                logger.exception("Failed to roll back %s", label)
                rollback_failures.append(
                    f"{label}: {type(exc).__name__}: {exc}",
                )

            if receipt_path is not None:
                try:
                    await unlink_async(receipt_path, missing_ok=True)
                except Exception as exc:  # pylint: disable=broad-except
                    record_rollback_failure(f"receipt {receipt_path}", exc)
            if created_chats:
                try:
                    await self._workspace.chat_manager.delete_chats(
                        created_chats,
                    )
                except Exception as exc:  # pylint: disable=broad-except
                    record_rollback_failure(
                        "chats " + ",".join(created_chats),
                        exc,
                    )
            for session_id, user_id, channel in created_states:
                try:
                    await _remove_session_state(
                        self._workspace,
                        session_id=session_id,
                        user_id=user_id,
                        channel=channel,
                    )
                except Exception as exc:  # pylint: disable=broad-except
                    record_rollback_failure(
                        f"session state {session_id}",
                        exc,
                    )
            for chat_id, previous in reversed(patched_project_dirs):
                try:
                    await self._workspace.chat_manager.set_project_dir(
                        chat_id,
                        previous,
                    )
                except Exception as exc:  # pylint: disable=broad-except
                    record_rollback_failure(
                        f"project directory {chat_id}",
                        exc,
                    )
            for chat_id in reversed(archived_chats):
                try:
                    await self._workspace.chat_manager.unarchive_chat(chat_id)
                except Exception as exc:  # pylint: disable=broad-except
                    record_rollback_failure(f"archived chat {chat_id}", exc)
            for card_name, credential_ref in reversed(created_mcp):
                try:
                    await driver_config.card_store.delete(card_name)
                except Exception as exc:  # pylint: disable=broad-except
                    record_rollback_failure(
                        f"MCP card {card_name}",
                        exc,
                    )
                if credential_ref:
                    try:
                        await driver_config.credential_store.delete(
                            credential_ref,
                        )
                    except Exception as exc:  # pylint: disable=broad-except
                        record_rollback_failure(
                            f"MCP credential {credential_ref}",
                            exc,
                        )
            for card, credentials, replacement_ref in reversed(replaced_mcp):
                try:
                    await restore_mcp(card, credentials, replacement_ref)
                except Exception as exc:  # pylint: disable=broad-except
                    record_rollback_failure(f"replaced MCP {card.name}", exc)
            cron_manager = getattr(self._workspace, "cron_manager", None)
            if cron_manager is not None:
                for job_id in reversed(created_scheduled_tasks):
                    try:
                        await cron_manager.delete_job(job_id)
                    except Exception as exc:  # pylint: disable=broad-except
                        record_rollback_failure(
                            f"scheduled task {job_id}",
                            exc,
                        )
                for previous_job in reversed(replaced_scheduled_tasks):
                    try:
                        restore = getattr(
                            cron_manager,
                            "restore_imported_job_snapshot",
                            None,
                        )
                        if callable(restore):
                            await restore(previous_job)
                        else:
                            await cron_manager.create_or_replace_job(
                                previous_job,
                            )
                    except Exception as exc:  # pylint: disable=broad-except
                        record_rollback_failure(
                            "replaced scheduled task "
                            + getattr(previous_job, "id", ""),
                            exc,
                        )
            for skill_name, snapshot in reversed(
                tuple(replaced_skills.items()),
            ):
                try:
                    await run_sync_io(
                        _restore_skill,
                        self._workspace.workspace_dir,
                        skill_name,
                        *snapshot,
                    )
                except Exception as exc:  # pylint: disable=broad-except
                    record_rollback_failure(
                        f"replaced Skill {skill_name}",
                        exc,
                    )
            for skill_name in reversed(imported_skills):
                if skill_name in replaced_skills:
                    continue
                try:
                    skill_service.disable_skill(skill_name)
                except Exception as exc:  # pylint: disable=broad-except
                    record_rollback_failure(f"Skill disable {skill_name}", exc)
                try:
                    await run_sync_io(
                        skill_service.delete_skill,
                        skill_name,
                    )
                except Exception as exc:  # pylint: disable=broad-except
                    record_rollback_failure(f"Skill {skill_name}", exc)
            if plugin_app is not None:
                for plugin_id, backup in reversed(
                    tuple(replaced_plugins.items()),
                ):
                    try:
                        await restore_plugin(plugin_id, backup)
                    except Exception as exc:  # pylint: disable=broad-except
                        record_rollback_failure(f"plugin {plugin_id}", exc)
                for plugin_id in reversed(installed_plugins):
                    if plugin_id in replaced_plugins:
                        continue
                    try:
                        # pylint: disable-next=C0415
                        from ..app.routers.plugins import (
                            uninstall_plugin_source,
                        )

                        await uninstall_plugin_source(
                            plugin_id,
                            app=plugin_app,
                            reload_agents=False,
                        )
                    except Exception as exc:  # pylint: disable=broad-except
                        record_rollback_failure(f"plugin {plugin_id}", exc)
            for target, memory_previous in reversed(memory_changes):
                try:
                    await run_sync_io(
                        _restore_memory_project,
                        target,
                        memory_previous,
                    )
                except Exception as exc:  # pylint: disable=broad-except
                    record_rollback_failure(f"Memory {target}", exc)
            if (
                marketplace_registry_touched
                and marketplace_registry_snapshot is not None
            ):
                registry_file = marketplace_registry_snapshot.path
                previous_registry = marketplace_registry_snapshot.content
                try:
                    async with get_path_lock(registry_file):
                        await run_sync_io(
                            _restore_registry_file,
                            registry_file,
                            previous_registry,
                        )
                except Exception as exc:  # pylint: disable=broad-except
                    record_rollback_failure(
                        f"Marketplace registry {registry_file}",
                        exc,
                    )
            if adaptation_root.is_dir() and not adaptation_root.is_symlink():
                try:
                    await run_sync_io(shutil.rmtree, adaptation_root)
                except Exception as exc:  # pylint: disable=broad-except
                    record_rollback_failure(
                        f"compatibility staging {adaptation_root}",
                        exc,
                    )
            if not rollback_failures and transaction is not None:
                try:
                    await transaction.discard()
                except Exception as exc:  # pylint: disable=broad-except
                    record_rollback_failure("transaction journal", exc)
            if rollback_failures and isinstance(
                original,
                (Exception, asyncio.CancelledError),
            ):
                raise ImportRollbackError(
                    rollback_failures,
                    cancelled=isinstance(original, asyncio.CancelledError),
                ) from original
            raise


__all__ = ["ImportRollbackError", "ProviderImportService"]
