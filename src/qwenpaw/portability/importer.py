# -*- coding: utf-8 -*-
"""Additive, idempotent provider-to-QwenPaw import transaction."""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from ..agents.skill_system import SkillService
from ..app.driver_config_service import DriverConfigService
from ..app.chats.models import ChatSpec
from ..app.chats.session import session_relative_paths
from ..drivers.adapters.mcp_legacy_config import legacy_mcp_client_to_driver
from ..harnesses.session import HarnessSessionBridge
from ..plugins.marketplace_registry import ExternalMarketplaceRegistry
from ..utils.io_utils import (
    get_path_lock,
    run_sync_io,
    unlink_async,
    write_json_atomic_async,
)
from .models import (
    ImportReceipt,
    ProviderInventory,
    SourceMemoryProject,
    SourceMCPServer,
    SourceSession,
    SourceSkill,
)
from .providers import create_migration_provider
from .providers.base import ProgressReporter

logger = logging.getLogger(__name__)

_MAX_SESSIONS = 500
_MAX_HISTORY_ITEMS = 20_000
_MAX_SESSION_TEXT_BYTES = 64 * 1024 * 1024
_MAX_SKILL_FILES = 5000
_MAX_SKILL_BYTES = 64 * 1024 * 1024
_MAX_MEMORY_FILES = 5000
_MAX_MEMORY_BYTES = 64 * 1024 * 1024


async def _report(
    progress: ProgressReporter | None,
    message: str,
) -> None:
    """Keep presentation failures from aborting the migration transaction."""
    if progress is None:
        return
    try:
        await progress(message)
    except Exception:  # pylint: disable=broad-except
        logger.debug("Migration progress reporter failed", exc_info=True)


def _progress_milestone(index: int, total: int) -> bool:
    """Report roughly five-percent increments without flooding the chat."""
    if total <= 20:
        return True
    step = max(1, total // 20)
    return index == 1 or index == total or index % step == 0


def _session_key(provider_id: str, source_id: str) -> str:
    digest = hashlib.sha256(source_id.encode("utf-8")).hexdigest()[:24]
    return f"import:{provider_id}:{digest}"


def _chat_id(provider_id: str, source_id: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"qwenpaw:{provider_id}:{source_id}"))


def _project_directory(
    session: SourceSession,
    warnings: list[str],
) -> str | None:
    """Return a safe existing source cwd for the QwenPaw session override."""
    raw = str(session.cwd or "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute() or not path.is_dir():
        warnings.append(
            f"Session {session.source_id} source project directory is no "
            f"longer available; retained it as provenance only: {raw}",
        )
        return None
    return str(path.resolve())


def _mcp_client_data(server: SourceMCPServer) -> Any:
    """Return the attribute shape consumed by the existing MCP translator."""
    return SimpleNamespace(
        name=server.name,
        description="Imported from external Agent; review before enabling.",
        enabled=False,
        transport=server.transport,
        command=server.command,
        args=list(server.args),
        env=dict(server.env),
        cwd=server.cwd,
        url=server.url,
        headers=dict(server.headers),
        oauth=None,
    )


def _bounded_session(session: SourceSession) -> SourceSession:
    if len(session.history) > _MAX_HISTORY_ITEMS:
        raise ValueError(
            f"Session {session.source_id} exceeds the history item limit.",
        )
    size = sum(
        len(item.model_dump_json().encode("utf-8", errors="replace"))
        for item in session.history
    )
    if size > _MAX_SESSION_TEXT_BYTES:
        raise ValueError(
            f"Session {session.source_id} exceeds the 64 MiB text limit.",
        )
    return session


def _bounded_memory(projects: list[SourceMemoryProject]) -> None:
    count = 0
    total = 0
    for project in projects:
        for item in project.files:
            source = item.source_path.expanduser()
            if source.is_symlink() or not source.is_file():
                raise ValueError(
                    f"Memory source is unavailable or symbolic: {source}",
                )
            count += 1
            total += source.stat().st_size
            if count > _MAX_MEMORY_FILES or total > _MAX_MEMORY_BYTES:
                raise ValueError(
                    "External memory exceeds the 5,000 file / 64 MiB "
                    "migration limit.",
                )


def _safe_memory_key(project: SourceMemoryProject) -> str:
    label = re.sub(r"[^A-Za-z0-9._-]+", "-", project.project_key).strip(
        ".-",
    )
    label = (label or "project")[:48]
    digest = hashlib.sha256(project.source_id.encode("utf-8")).hexdigest()[:10]
    return f"{label}-{digest}"


def _memory_import_root(workspace: Any, provider_id: str) -> Path:
    daily_dir = "memory"
    manager = getattr(workspace, "memory_manager", None)
    if manager is not None:
        try:
            config = manager.get_memory_config()
            configured = getattr(config, "daily_dir", "")
            if isinstance(configured, str) and configured.strip():
                daily_dir = configured.strip()
        except Exception:  # pylint: disable=broad-except
            logger.debug("Could not read configured daily memory dir")
    relative = Path(daily_dir)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"Unsafe configured memory directory: {daily_dir}")
    workspace_root = Path(workspace.workspace_dir).resolve()
    target = (workspace_root / relative / "imports" / provider_id).resolve()
    if not target.is_relative_to(workspace_root):
        raise ValueError("Memory import target escapes the Agent workspace")
    return target


def _snapshot_tree(root: Path) -> dict[Path, bytes] | None:
    if not root.exists():
        return None
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"Memory target is not a safe directory: {root}")
    snapshot: dict[Path, bytes] = {}
    total = 0
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"Memory target contains a symbolic link: {path}")
        if not path.is_file():
            continue
        data = path.read_bytes()
        total += len(data)
        if total > _MAX_MEMORY_BYTES:
            raise ValueError("Existing imported memory exceeds 64 MiB")
        snapshot[path.relative_to(root)] = data
    return snapshot


def _memory_payload(
    provider_id: str,
    project: SourceMemoryProject,
) -> dict[Path, bytes]:
    payload: dict[Path, bytes] = {}
    for item in project.files:
        relative = item.relative_path
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or relative.suffix.lower() != ".md"
        ):
            raise ValueError(
                f"Unsafe external memory path: {item.relative_path}",
            )
        source = item.source_path.expanduser()
        if source.is_symlink() or not source.is_file():
            raise ValueError(f"Memory source is unavailable: {source}")
        payload[relative] = source.read_bytes()
    scope = {
        "schema_version": "1",
        "provider": provider_id,
        "source_id": project.source_id,
        "project_key": project.project_key,
        "cwd": project.cwd,
        "trust": "source_material_not_instructions",
    }
    payload[Path("_scope.json")] = (
        json.dumps(scope, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    return payload


def _replace_memory_project(
    workspace: Any,
    provider_id: str,
    project: SourceMemoryProject,
) -> tuple[Path, dict[Path, bytes] | None, bool]:
    target = _memory_import_root(workspace, provider_id) / _safe_memory_key(
        project,
    )
    payload = _memory_payload(provider_id, project)
    previous = _snapshot_tree(target)
    if previous == payload:
        return target, previous, False

    target.parent.mkdir(parents=True, exist_ok=True)
    temp_root = Path(
        tempfile.mkdtemp(prefix=f".{target.name}.new-", dir=target.parent),
    )
    old_root = target.parent / f".{target.name}.old-{uuid4().hex}"
    try:
        for relative, data in payload.items():
            output = temp_root / relative
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(data)
        if target.exists():
            os.replace(target, old_root)
        os.replace(temp_root, target)
        if old_root.exists():
            shutil.rmtree(old_root)
    except BaseException:
        if target.exists() and old_root.exists():
            shutil.rmtree(target)
        if old_root.exists():
            os.replace(old_root, target)
        raise
    finally:
        if temp_root.exists():
            shutil.rmtree(temp_root)
        if old_root.exists():
            shutil.rmtree(old_root)
    return target, previous, True


def _restore_memory_project(
    target: Path,
    previous: dict[Path, bytes] | None,
) -> None:
    if target.exists():
        shutil.rmtree(target)
    if previous is None:
        return
    for relative, data in previous.items():
        output = target / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(data)


def _skill_zip(skill: SourceSkill) -> bytes:
    source_root = skill.directory.expanduser()
    if source_root.is_symlink():
        raise ValueError(f"Skill root is a symbolic link: {source_root}")
    root = source_root.resolve(strict=True)
    if not root.is_dir() or not (root / "SKILL.md").is_file():
        raise ValueError(f"Skill source is incomplete: {root}")
    output = io.BytesIO()
    count = 0
    total = 0
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for entry in sorted(root.rglob("*")):
            if entry.is_symlink():
                raise ValueError(f"Skill contains a symbolic link: {entry}")
            if not entry.is_file():
                continue
            resolved = entry.resolve(strict=True)
            if not resolved.is_relative_to(root):
                raise ValueError(f"Skill file escapes its root: {entry}")
            size = entry.stat().st_size
            count += 1
            total += size
            if count > _MAX_SKILL_FILES or total > _MAX_SKILL_BYTES:
                raise ValueError(
                    f"Skill {skill.name!r} exceeds migration limits.",
                )
            archive.write(entry, f"{root.name}/{entry.relative_to(root)}")
    return output.getvalue()


async def _remove_session_state(
    workspace: Any,
    *,
    session_id: str,
    user_id: str,
    channel: str,
) -> None:
    save_dir = Path(workspace.session.save_dir)
    for relative in session_relative_paths(session_id, user_id, channel):
        await unlink_async(save_dir / relative, missing_ok=True)


class ProviderImportService:  # pylint: disable=too-few-public-methods
    """Trusted writer coordinating provider inventory and QwenPaw stores."""

    def __init__(self, workspace: Any) -> None:
        """Bind the importer to one already-started Agent workspace."""
        self._workspace = workspace

    async def import_from(
        self,
        source: str,
        *,
        progress: ProgressReporter | None = None,
    ) -> ImportReceipt:
        """Inventory one provider and commit its portable state."""
        started_at = datetime.now(timezone.utc)
        lock_path = self._workspace.workspace_dir / ".qwenpaw" / "imports"
        await _report(progress, "正在等待迁移锁，避免重复导入…")
        async with get_path_lock(lock_path):
            await _report(progress, f"正在检测 {source} 并读取可迁移内容…")
            provider = create_migration_provider(source, self._workspace)
            inventory = await provider.inventory(
                limit=_MAX_SESSIONS,
                progress=progress,
            )
            if not inventory.detected:
                detail = "; ".join(inventory.warnings) or "source not detected"
                raise ValueError(
                    f"{inventory.provider_name} import source was not found: "
                    f"{detail}",
                )
            await _report(
                progress,
                "读取完成："
                f"{len(inventory.sessions)} 个会话、"
                f"{len(inventory.skills)} 个 Skill、"
                f"{len(inventory.mcp_servers)} 个 MCP、"
                f"{len(inventory.memory_projects)} 组 Memory、"
                f"{len(inventory.plugins)} 个插件；正在写入 QwenPaw…",
            )
            return await self._apply(
                inventory,
                started_at=started_at,
                progress=progress,
            )

    # pylint: disable-next=R0912,R0915,R0914
    async def _apply(
        self,
        inventory: ProviderInventory,
        *,
        started_at: datetime,
        progress: ProgressReporter | None = None,
    ) -> ImportReceipt:
        """Apply one fully inventoried source as a rollback-capable batch."""
        migration_id = f"migration-{uuid4().hex}"
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

        imported_sessions: list[str] = []
        skipped_sessions: list[str] = []
        imported_skills: list[str] = []
        skipped_skills: list[str] = []
        imported_mcp_servers: list[str] = []
        skipped_mcp_servers: list[str] = []
        imported_memory_projects: list[str] = []
        skipped_memory_projects: list[str] = []
        restored_marketplaces: list[str] = []
        skipped_marketplaces: list[str] = []
        installed_plugins: list[str] = []
        skipped_plugins: list[str] = []
        plugin_app = None
        created_chats: list[str] = []
        created_states: list[tuple[str, str, str]] = []
        patched_project_dirs: list[tuple[str, str | None]] = []
        created_mcp: list[tuple[str, str]] = []
        memory_changes: list[tuple[Path, dict[Path, bytes] | None]] = []
        skill_service = SkillService(self._workspace.workspace_dir)
        driver_config = DriverConfigService(self._workspace)

        try:
            registry_path = getattr(
                self._workspace,
                "marketplace_registry_path",
                None,
            )
            marketplace_registry = ExternalMarketplaceRegistry(registry_path)
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
                (
                    changed,
                    credentials_removed,
                ) = await marketplace_registry.register(
                    provider=inventory.provider_id,
                    source_id=marketplace.source_id,
                    name=marketplace.name,
                    source=marketplace.source,
                    source_type=marketplace.source_type,
                    ref_name=marketplace.ref_name,
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

            if inventory.plugins:
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
            for plugin_index, plugin in enumerate(
                inventory.plugins,
                start=1,
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
                if plugin_app is None:
                    skipped_plugins.append(plugin.source_id)
                    warnings.append(
                        f"Plugin {plugin.source_id!r} is compatible, but the "
                        "QwenPaw native plugin loader is not ready. Retry "
                        "/import after startup completes.",
                    )
                    continue
                try:
                    # pylint: disable-next=C0415
                    from ..app.routers.plugins import (
                        install_plugin_source,
                    )

                    record = await install_plugin_source(
                        plugin.install_source,
                        app=plugin_app,
                        force=False,
                        reload_agents=False,
                    )
                    installed_plugins.append(record.manifest.id)
                except Exception as exc:  # pylint: disable=broad-except
                    skipped_plugins.append(plugin.source_id)
                    warnings.append(
                        f"Plugin {plugin.source_id!r} failed native "
                        f"installation: {type(exc).__name__}: {exc}",
                    )
            if installed_plugins:
                warnings.append(
                    "Compatible plugins were installed and hot-loaded "
                    "without reloading Agents during the active /import. "
                    "Their tools remain disabled until reviewed; restart or "
                    "reload the Agent after this migration if needed.",
                )

            memory_total = len(inventory.memory_projects)
            for memory_index, project in enumerate(
                inventory.memory_projects,
                start=1,
            ):
                if _progress_milestone(memory_index, memory_total):
                    await _report(
                        progress,
                        "正在按项目作用域迁移长期 Memory："
                        f"{memory_index}/{memory_total}",
                    )
                try:
                    target, previous, changed = await run_sync_io(
                        _replace_memory_project,
                        self._workspace,
                        inventory.provider_id,
                        project,
                    )
                    if changed:
                        memory_changes.append((target, previous))
                        imported_memory_projects.append(project.source_id)
                    else:
                        skipped_memory_projects.append(project.source_id)
                except Exception as exc:  # pylint: disable=broad-except
                    skipped_memory_projects.append(project.source_id)
                    warnings.append(
                        f"Memory project {project.project_key!r} was "
                        f"quarantined/skipped: {type(exc).__name__}: {exc}",
                    )

            skill_total = len(inventory.skills)
            for skill_index, skill in enumerate(inventory.skills, start=1):
                if _progress_milestone(skill_index, skill_total):
                    await _report(
                        progress,
                        f"正在安全检查并暂存 Skill：" f"{skill_index}/{skill_total}",
                    )
                try:
                    data = await run_sync_io(_skill_zip, skill)
                    result = await run_sync_io(
                        skill_service.import_from_zip,
                        data,
                        False,
                    )
                    names = [str(name) for name in result.get("imported", [])]
                    if names:
                        imported_skills.extend(names)
                    else:
                        skipped_skills.append(skill.name)
                        if result.get("conflicts"):
                            warnings.append(
                                f"Skill {skill.name!r} already exists; kept "
                                "the QwenPaw copy.",
                            )
                except Exception as exc:  # pylint: disable=broad-except
                    skipped_skills.append(skill.name)
                    warnings.append(
                        f"Skill {skill.name!r} was quarantined/skipped: {exc}",
                    )

            existing_driver_names = {
                card.name for card in await driver_config.list_cards()
            }
            mcp_total = len(inventory.mcp_servers)
            for mcp_index, server in enumerate(
                inventory.mcp_servers,
                start=1,
            ):
                if _progress_milestone(mcp_index, mcp_total):
                    await _report(
                        progress,
                        f"正在转换并加密保存 MCP：{mcp_index}/{mcp_total}",
                    )
                if server.name in existing_driver_names:
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
                credential_ref = ""
                try:
                    card, credential = legacy_mcp_client_to_driver(
                        server.name,
                        _mcp_client_data(server),
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
                    if credential is not None:
                        await driver_config.credential_store.put(credential)
                        credential_ref = credential.ref
                    try:
                        await driver_config.save_card(
                            card,
                            reload_driver=False,
                        )
                    except BaseException:
                        if credential_ref:
                            await driver_config.credential_store.delete(
                                credential_ref,
                            )
                        raise
                    created_mcp.append((card.name, credential_ref))
                    existing_driver_names.add(card.name)
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
                    skipped_mcp_servers.append(server.name)
                    warnings.append(
                        f"MCP {server.name!r} could not be translated and "
                        f"was skipped: {type(exc).__name__}: {exc}",
                    )

            bridge = HarnessSessionBridge(self._workspace.session)
            session_total = len(sessions)
            for session_index, session in enumerate(sessions, start=1):
                if _progress_milestone(session_index, session_total):
                    await _report(
                        progress,
                        f"正在写入会话：{session_index}/{session_total}",
                    )
                source_key = (inventory.provider_id, session.source_id)
                existing_chat = existing_by_source.get(source_key)
                project_dir = _project_directory(session, warnings)
                if existing_chat is not None:
                    if project_dir:
                        current = str(
                            (
                                existing_chat.meta.get("runtime_context") or {}
                            ).get(
                                "project_dir",
                            )
                            or "",
                        )
                        if current != project_dir:
                            patched_project_dirs.append(
                                (existing_chat.id, current or None),
                            )
                            await self._workspace.chat_manager.set_project_dir(
                                existing_chat.id,
                                project_dir,
                            )
                    skipped_sessions.append(session.source_id)
                    continue
                if not session.history:
                    skipped_sessions.append(session.source_id)
                    warnings.append(
                        f"Session {session.source_id} contained no readable "
                        "conversation history.",
                    )
                    continue
                session_id = _session_key(
                    inventory.provider_id,
                    session.source_id,
                )
                user_id = session_id
                channel = "console"
                await bridge.hydrate(
                    session_id=session_id,
                    user_id=user_id,
                    channel=channel,
                    backend=inventory.provider_id,
                    history=session.history,
                )
                created_states.append((session_id, user_id, channel))
                portability_meta = {
                    "schema_version": "1",
                    "source": inventory.provider_id,
                    "source_id": session.source_id,
                    "source_locator": inventory.locator,
                    "source_cwd": session.cwd,
                    "imported_at": datetime.now(timezone.utc).isoformat(),
                    "import_mode": "historical_archive",
                    "read_only_enforced": False,
                    "continuation_fidelity": "not_guaranteed",
                    "historical_tools_are_data": True,
                    "fidelity": "normalized_lossy",
                }
                chat_meta: dict[str, Any] = {
                    "portability": portability_meta,
                }
                if project_dir:
                    chat_meta["runtime_context"] = {
                        "project_dir": project_dir,
                    }
                spec = ChatSpec(
                    id=_chat_id(inventory.provider_id, session.source_id),
                    name=session.title,
                    session_id=session_id,
                    user_id=user_id,
                    channel=channel,
                    created_at=session.created_at or started_at,
                    updated_at=session.updated_at
                    or session.created_at
                    or started_at,
                    meta=chat_meta,
                )
                await self._workspace.chat_manager.create_chat(spec)
                existing_by_source[source_key] = spec
                created_chats.append(spec.id)
                imported_sessions.append(session.source_id)

            completed_at = datetime.now(timezone.utc)
            await _report(progress, "正在生成迁移回执并完成一致性检查…")
            receipt = ImportReceipt(
                migration_id=migration_id,
                source=inventory.provider_id,
                source_locator=inventory.locator,
                agent_id=self._workspace.agent_id,
                started_at=started_at,
                completed_at=completed_at,
                imported_sessions=imported_sessions,
                skipped_sessions=skipped_sessions,
                imported_skills=imported_skills,
                skipped_skills=skipped_skills,
                imported_mcp_servers=imported_mcp_servers,
                skipped_mcp_servers=skipped_mcp_servers,
                imported_memory_projects=imported_memory_projects,
                skipped_memory_projects=skipped_memory_projects,
                restored_marketplaces=restored_marketplaces,
                skipped_marketplaces=skipped_marketplaces,
                installed_plugins=installed_plugins,
                skipped_plugins=skipped_plugins,
                discovered_mcp_count=inventory.discovered_mcp_count,
                warnings=warnings,
            )
            receipt_dir = (
                self._workspace.workspace_dir / ".qwenpaw" / "imports"
            )
            receipt_dir.mkdir(parents=True, exist_ok=True)
            await write_json_atomic_async(
                receipt_dir / f"{migration_id}.json",
                receipt.model_dump(mode="json"),
                sort_keys=True,
                new_file_mode=0o600,
            )
            await _report(progress, "迁移事务已安全提交。")
            return receipt
        except BaseException:
            if created_chats:
                await self._workspace.chat_manager.delete_chats(created_chats)
            for session_id, user_id, channel in created_states:
                await _remove_session_state(
                    self._workspace,
                    session_id=session_id,
                    user_id=user_id,
                    channel=channel,
                )
            for chat_id, previous in reversed(patched_project_dirs):
                await self._workspace.chat_manager.set_project_dir(
                    chat_id,
                    previous,
                )
            for card_name, credential_ref in reversed(created_mcp):
                try:
                    await driver_config.card_store.delete(card_name)
                    if credential_ref:
                        await driver_config.credential_store.delete(
                            credential_ref,
                        )
                except Exception:  # pylint: disable=broad-except
                    logger.exception(
                        "Failed to roll back imported MCP %s",
                        card_name,
                    )
            for skill_name in imported_skills:
                try:
                    await run_sync_io(
                        skill_service.delete_skill,
                        skill_name,
                    )
                except Exception:  # pylint: disable=broad-except
                    logger.exception(
                        "Failed to roll back imported Skill %s",
                        skill_name,
                    )
            if plugin_app is not None:
                for plugin_id in reversed(installed_plugins):
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
                    except Exception:  # pylint: disable=broad-except
                        logger.exception(
                            "Failed to roll back imported plugin %s",
                            plugin_id,
                        )
            for target, previous in reversed(memory_changes):
                try:
                    await run_sync_io(
                        _restore_memory_project,
                        target,
                        previous,
                    )
                except Exception:  # pylint: disable=broad-except
                    logger.exception(
                        "Failed to roll back imported Memory %s",
                        target,
                    )
            raise


__all__ = ["ProviderImportService"]
