# -*- coding: utf-8 -*-
"""Pure helpers shared by the provider import transaction."""

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
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from ..app.chats.session import session_relative_paths
from ..utils.io_utils import unlink_async
from .models import (
    SourceMemoryProject,
    SourceMCPServer,
    SourceSession,
    SourceSkill,
)
from .skill_transfer import read_bounded_skill_tree

logger = logging.getLogger(__name__)

_MAX_HISTORY_ITEMS = 20_000
_MAX_SESSION_TEXT_BYTES = 64 * 1024 * 1024
_MAX_MEMORY_FILES = 5000
_MAX_MEMORY_BYTES = 64 * 1024 * 1024
_MAX_MARKETPLACE_REGISTRY_BYTES = 16 * 1024 * 1024
_PLAN_ID_PATTERN = re.compile(r"^plan-[0-9a-f]{32}$")


@dataclass(frozen=True)
class _RegistrySnapshot:
    """Exact pre-migration Marketplace state used for rollback."""

    path: Path
    content: bytes | None


def _progress_milestone(index: int, total: int) -> bool:
    """Report roughly five-percent increments without flooding the chat."""
    if total <= 20:
        return True
    step = max(1, total // 20)
    return index == 1 or index == total or index % step == 0


def _snapshot_registry_file(path: Path) -> bytes | None:
    """Snapshot a small regular registry file for transaction rollback."""
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise ValueError("Marketplace registry is not a safe regular file")
    if path.stat().st_size > _MAX_MARKETPLACE_REGISTRY_BYTES:
        raise ValueError("Marketplace registry exceeds the 16 MiB limit")
    return path.read_bytes()


def _restore_registry_file(path: Path, content: bytes | None) -> None:
    """Atomically restore a prior registry snapshot, or remove a new file."""
    if content is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.rollback-",
        dir=path.parent,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
    finally:
        Path(temp_name).unlink(missing_ok=True)


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
    source = skill.directory.expanduser()
    root = source.resolve(strict=True)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for entry in read_bounded_skill_tree(source):
            if entry.is_dir:
                continue
            info = zipfile.ZipInfo(f"{root.name}/{entry.relative}")
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = entry.mode << 16
            archive.writestr(info, entry.data or b"")
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
