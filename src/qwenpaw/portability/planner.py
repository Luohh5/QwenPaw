# -*- coding: utf-8 -*-
"""Read-only source fingerprinting and review-plan construction."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .models import (
    MigrationAssetPlan,
    MigrationPlan,
    ProviderInventory,
)
from .safe_files import (
    TreeBudget,
    TreeLimitError,
    TreeLimits,
    open_regular_file,
    regular_file_info,
    walk_tree,
)

_MAX_FINGERPRINT_ENTRIES = 6_000
_MAX_FINGERPRINT_FILES = 5_000
_MAX_FINGERPRINT_BYTES = 64 * 1024 * 1024
_HASH_CHUNK_BYTES = 1024 * 1024


def _fingerprint_error(path: Path, reason: str) -> ValueError:
    return ValueError(f"Unsafe portable fingerprint source {path}: {reason}")


def _absolute_path(path: Path) -> Path:
    expanded = path.expanduser()
    if expanded.is_absolute():
        return Path(os.path.abspath(expanded))
    return Path(os.path.abspath(Path.cwd() / expanded))


def _hash_record(hasher: Any, kind: str, value: str) -> None:
    encoded_kind = kind.encode("utf-8")
    encoded_value = value.encode("utf-8", errors="replace")
    hasher.update(len(encoded_kind).to_bytes(4, "big"))
    hasher.update(encoded_kind)
    hasher.update(len(encoded_value).to_bytes(8, "big"))
    hasher.update(encoded_value)


def _hash_regular_file(
    hasher: Any,
    path: Path,
    before: os.stat_result,
) -> None:
    try:
        with open_regular_file(path, expected=before) as (stream, opened):
            _hash_record(hasher, "file", str(path))
            hasher.update(opened.st_size.to_bytes(8, "big"))
            remaining = opened.st_size
            while remaining:
                chunk = stream.read(min(_HASH_CHUNK_BYTES, remaining))
                if not chunk:
                    raise _fingerprint_error(path, "file shrank while hashing")
                hasher.update(chunk)
                remaining -= len(chunk)
            if stream.read(1):
                raise _fingerprint_error(path, "file grew while hashing")
    except (OSError, ValueError) as exc:
        raise _fingerprint_error(
            path,
            f"cannot read ({type(exc).__name__})",
        ) from exc


def _hash_tree(
    hasher: Any,
    root: Path,
    budget: TreeBudget,
    limits: TreeLimits,
) -> None:
    try:
        for entry in walk_tree(
            root,
            limits=limits,
            budget=budget,
            include_root=True,
            unsafe="yield",
        ):
            if stat.S_ISLNK(entry.info.st_mode):
                kind = "rejected-symbolic-link"
            elif entry.is_file:
                _hash_regular_file(hasher, entry.path, entry.info)
                continue
            elif entry.is_dir:
                kind = "directory"
            else:
                kind = "rejected-non-regular-entry"
            _hash_record(hasher, kind, str(entry.path))
    except TreeLimitError:
        raise
    except (OSError, ValueError) as exc:
        raise _fingerprint_error(
            root,
            f"cannot walk source tree ({type(exc).__name__})",
        ) from exc


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _has_parent_in(path: Path, roots: set[Path]) -> bool:
    parent = path.parent
    while parent != parent.parent:
        if parent in roots:
            return True
        parent = parent.parent
    return parent in roots


@dataclass(frozen=True)
class _FingerprintSource:
    kind: str
    path: Path


def _fingerprint_sources(
    inventory: ProviderInventory,
) -> list[_FingerprintSource]:
    sources: list[_FingerprintSource] = []

    def add(kind: str, value: Path | str) -> None:
        if len(sources) >= _MAX_FINGERPRINT_ENTRIES:
            raise ValueError(
                "Portable inventory exceeds the fingerprint source limit "
                f"({_MAX_FINGERPRINT_ENTRIES}).",
            )
        sources.append(
            _FingerprintSource(kind=kind, path=_absolute_path(Path(value))),
        )

    for project in inventory.memory_projects:
        for item in project.files:
            relative = item.relative_path
            if relative.is_absolute() or ".." in relative.parts:
                raise _fingerprint_error(
                    item.source_path,
                    "memory relative path escapes its declared scope",
                )
            add("file", item.source_path)
    for skill in inventory.skills:
        add("tree", skill.directory)
    for plugin in inventory.plugins:
        if plugin.install_source:
            add("plugin", plugin.install_source)
    return sources


# pylint: disable-next=too-many-return-statements
def _canonical_source(
    source: _FingerprintSource,
) -> tuple[str, Path]:
    path = source.path
    try:
        info = path.lstat()
    except FileNotFoundError:
        if source.kind == "plugin":
            return "external", path
        return "rejected-missing", path
    except OSError as exc:
        raise _fingerprint_error(
            path,
            f"cannot inspect ({type(exc).__name__})",
        ) from exc

    if stat.S_ISLNK(info.st_mode):
        return "rejected-symbolic-link", path
    if source.kind == "tree" and not stat.S_ISDIR(info.st_mode):
        return "rejected-non-directory", path
    if source.kind == "file" and not stat.S_ISREG(info.st_mode):
        return "rejected-non-regular", path
    if source.kind == "plugin" and not (
        stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)
    ):
        return "rejected-non-regular", path
    try:
        canonical = path.resolve(strict=True)
    except OSError as exc:
        raise _fingerprint_error(path, "cannot resolve source") from exc
    return ("tree" if stat.S_ISDIR(info.st_mode) else "file"), canonical


def _hash_inventory_sources(
    hasher: Any,
    inventory: ProviderInventory,
) -> None:
    classified = {
        _canonical_source(source) for source in _fingerprint_sources(inventory)
    }
    roots = sorted(
        (path for kind, path in classified if kind == "tree"),
        key=lambda path: path.parts,
    )
    selected_roots: list[Path] = []
    for root in roots:
        if selected_roots and _is_within(root, selected_roots[-1]):
            continue
        selected_roots.append(root)

    root_set = set(selected_roots)
    files = {
        path
        for kind, path in classified
        if kind == "file" and not _has_parent_in(path, root_set)
    }
    markers = {
        (kind, path)
        for kind, path in classified
        if kind not in {"tree", "file"}
    }
    work = [("tree", path) for path in selected_roots]
    work.extend(("file", path) for path in files)
    work.extend(markers)
    work.sort(key=lambda item: (str(item[1]), item[0]))

    limits = TreeLimits(
        entries=_MAX_FINGERPRINT_ENTRIES,
        files=_MAX_FINGERPRINT_FILES,
        bytes=_MAX_FINGERPRINT_BYTES,
    )
    budget = TreeBudget()
    try:
        for kind, path in work:
            if kind == "tree":
                _hash_tree(hasher, path, budget, limits)
            elif kind == "file":
                info = regular_file_info(path)
                budget.add(path, info, limits)
                _hash_regular_file(hasher, path, info)
            else:
                budget.add_entry(path, limits)
                _hash_record(hasher, kind, str(path))
    except TreeLimitError as exc:
        raise ValueError(
            "Portable source tree exceeds the fingerprint "
            f"{exc.kind} limit ({exc.limit}): {exc.path}",
        ) from exc


def inventory_fingerprint(inventory: ProviderInventory) -> str:
    """Fingerprint normalized objects plus referenced portable file bytes."""
    hasher = hashlib.sha256()
    payload = inventory.model_dump(
        mode="json",
        exclude={
            "ignored_session_ids",
            "source_location",
            "warnings",
        },
    )
    hasher.update(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8"),
    )
    _hash_inventory_sources(hasher, inventory)
    return hasher.hexdigest()


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


def _asset(
    asset_type: str,
    source_id: str,
    name: str,
    action: str,
    fidelity: str,
    reason_zh: str,
) -> MigrationAssetPlan:
    return MigrationAssetPlan(
        asset_type=asset_type,
        source_id=source_id,
        name=name,
        action=action,
        fidelity=fidelity,
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
    existing_memory = _imported_memory_ids(
        workspace,
        inventory.provider_id,
    )
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
        actions.append(
            _asset(
                "skill",
                skill.source_id,
                skill.name,
                "agent_test_and_adapt",
                "agent_decision",
                "进入安全暂存区，由 Agent 语义判断，必要时再由 Mission 修复。",
            ),
        )

    for server in inventory.mcp_servers:
        actions.append(
            _asset(
                "mcp",
                server.source_id,
                server.name,
                "agent_test_and_adapt",
                "agent_decision",
                "进入安全暂存区，由 Agent 语义判断，必要时再由 Mission 修复。",
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
        actions.append(
            _asset(
                "plugin",
                plugin.source_id,
                plugin.name,
                "agent_test_and_adapt",
                "agent_decision",
                "进入安全暂存区，由 Agent 判断整体可用性，必要时再启动修复 Mission。",
            ),
        )

    for task in inventory.scheduled_tasks:
        actions.append(
            _asset(
                "scheduled_task",
                task.source_id,
                task.name,
                "agent_test_and_adapt",
                "agent_decision",
                "进入安全暂存区，由 Agent 语义判断，必要时再由 Mission 修复和验证。",
            ),
        )

    counts = {
        "sessions": len(inventory.sessions),
        "ignored_source_sessions": len(inventory.ignored_session_ids),
        "skills": len(inventory.skills),
        "mcp_servers": len(inventory.mcp_servers),
        "memory_scopes": len(inventory.memory_projects),
        "marketplaces": len(inventory.marketplaces),
        "plugins": len(inventory.plugins),
        "scheduled_tasks": len(inventory.scheduled_tasks),
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
