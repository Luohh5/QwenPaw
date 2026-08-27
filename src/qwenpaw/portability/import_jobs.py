# -*- coding: utf-8 -*-
"""Persisted scan/apply jobs for the Console import workflow."""

from __future__ import annotations

import asyncio
import re
from collections import deque
from dataclasses import dataclass, field as dataclass_field
from functools import partial
from pathlib import Path
from typing import Any, AsyncIterator, Callable
from uuid import uuid4

from pydantic import BaseModel, Field

from ..utils.io_utils import read_json_async, write_json_atomic_async
from .compatibility import load_manifest
from .compatibility_safety import redact_sensitive_text
from .import_status import project_asset_results
from .importer import ProviderImportService
from .models import (
    ImportAssetResult,
    ImportAssetState,
    ImportReceipt,
    ImportSelection,
    MigrationPlan,
)

_SUPPORTED_SOURCES = {"codex", "qoder"}
_TERMINAL = {"completed", "completed_with_issues", "failed", "interrupted"}
_SESSIONS = re.compile(
    r"^正在写入会话[：:]\s*(\d+)\s*/\s*(\d+)[（(]聊天记录阶段[）)]$",
)
_ASSET_NAME = re.compile(r"[「『](.+?)[」』]")
_TYPE_FIELDS = {
    "memory": "memory",
    "scheduled_task": "cron",
    "skill": "skills",
    "mcp": "mcp",
    "plugin": "plugins",
}


class ImportProviderSnapshot(BaseModel):
    """One source in a UI import job."""

    source: str
    state: str = "scanning"
    plan_id: str = ""
    sessions_total: int = 0
    sessions_processed: int = 0
    sessions_imported: int = 0
    sessions_skipped: int = 0
    selection: ImportSelection = Field(default_factory=ImportSelection)
    assets: list[ImportAssetResult] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    error: str = ""


class ImportJobSnapshot(BaseModel):
    """UI-safe durable state; source content and secrets are excluded."""

    job_id: str
    agent_id: str
    state: str = "scanning"
    phase: str = "scan"
    seq: int = 0
    providers: list[ImportProviderSnapshot] = Field(default_factory=list)
    logs: list[str] = Field(default_factory=list)


@dataclass
class _LiveJob:
    workspace: Any
    snapshot: ImportJobSnapshot
    plans: dict[str, MigrationPlan] = dataclass_field(default_factory=dict)
    events: deque[dict[str, Any]] = dataclass_field(
        default_factory=lambda: deque(maxlen=64),
    )
    subscribers: set[asyncio.Queue] = dataclass_field(default_factory=set)
    task: asyncio.Task | None = None


class PortabilityImportJobManager:
    """Bridge the existing migration service to resumable UI jobs."""

    def __init__(
        self,
        *,
        service_factory: Callable[[Any], Any] = ProviderImportService,
    ) -> None:
        self._service_factory = service_factory
        self._jobs: dict[tuple[str, str], _LiveJob] = {}

    async def create(
        self,
        workspace: Any,
        sources: list[str],
    ) -> ImportJobSnapshot:
        """Create a job and start source discovery in the background."""
        normalized = list(dict.fromkeys(sources))
        if not normalized or any(
            item not in _SUPPORTED_SOURCES for item in normalized
        ):
            raise ValueError("sources must contain codex or qoder")
        job_id = f"import-{uuid4().hex}"
        snapshot = ImportJobSnapshot(
            job_id=job_id,
            agent_id=workspace.agent_id,
            providers=[
                ImportProviderSnapshot(source=item) for item in normalized
            ],
        )
        live = _LiveJob(workspace=workspace, snapshot=snapshot)
        self._jobs[(workspace.agent_id, job_id)] = live
        await self._emit(live, persist=True)
        live.task = asyncio.create_task(self._scan(live))
        return snapshot.model_copy(deep=True)

    async def start(
        self,
        workspace: Any,
        job_id: str,
        selections: dict[str, ImportSelection],
    ) -> ImportJobSnapshot:
        """Apply the user selection for every successfully scanned source."""
        live = await self._live(workspace, job_id)
        if live.snapshot.state != "awaiting_selection":
            raise RuntimeError("import job is not awaiting selection")
        if any(
            item.snapshot.agent_id == workspace.agent_id
            and item.snapshot.state == "running"
            for item in self._jobs.values()
        ):
            raise RuntimeError("an import is already running for this agent")
        ready = {
            item.source
            for item in live.snapshot.providers
            if item.state == "ready"
        }
        if set(selections) != ready:
            raise ValueError("selection must cover every detected source")
        for provider in live.snapshot.providers:
            if provider.source not in selections:
                continue
            provider.selection = selections[provider.source]
            provider.state = "pending"
            provider.assets = self._selected_assets(
                live.plans[provider.source],
                provider.selection,
            )
        live.snapshot.state = "running"
        live.snapshot.phase = "import"
        await self._emit(live, persist=True)
        live.task = asyncio.create_task(self._apply(live))
        return live.snapshot.model_copy(deep=True)

    async def wait(self, job_id: str) -> None:
        """Wait for the current scan or import task to finish."""
        matches = [
            item
            for (_, current), item in self._jobs.items()
            if current == job_id
        ]
        if not matches:
            raise ValueError("import job not found")
        if matches[0].task:
            await matches[0].task

    async def snapshot(self, workspace: Any, job_id: str) -> ImportJobSnapshot:
        """Return current state, restoring a persisted job when needed."""
        key = (workspace.agent_id, job_id)
        live = self._jobs.get(key)
        if live:
            return live.snapshot.model_copy(deep=True)
        try:
            value = await read_json_async(self._path(workspace, job_id))
            snapshot = ImportJobSnapshot.model_validate(value)
        except (FileNotFoundError, OSError, ValueError, TypeError) as exc:
            raise ValueError("import job not found or invalid") from exc
        if snapshot.agent_id != workspace.agent_id:
            raise ValueError("import job belongs to another agent")
        if snapshot.state in {"scanning", "running"}:
            snapshot.state = "interrupted"
            snapshot.phase = "done"
            await self._persist(workspace, snapshot)
        self._jobs[key] = _LiveJob(workspace=workspace, snapshot=snapshot)
        return snapshot.model_copy(deep=True)

    async def subscribe(
        self,
        workspace: Any,
        job_id: str,
        *,
        after: int = 0,
    ) -> AsyncIterator[dict[str, Any]]:
        """Replay buffered updates, then stream updates through completion."""
        live = await self._live(workspace, job_id)
        for event in live.events:
            if event["seq"] > after:
                yield event
        if live.snapshot.state in _TERMINAL:
            return
        queue: asyncio.Queue = asyncio.Queue(maxsize=16)
        live.subscribers.add(queue)
        try:
            while True:
                event = await queue.get()
                yield event
                if event["snapshot"]["state"] in _TERMINAL:
                    return
        finally:
            live.subscribers.discard(queue)

    async def _scan(self, live: _LiveJob) -> None:
        async def scan(provider: ImportProviderSnapshot) -> None:
            service = self._service_factory(live.workspace)

            async def progress(message: str) -> None:
                self._log(live, message)
                await self._emit(live)

            try:
                plan = await service.plan_from(
                    provider.source,
                    progress=progress,
                )
                live.plans[provider.source] = plan
                provider.plan_id = plan.plan_id
                provider.sessions_total = plan.inventory_counts.get(
                    "sessions",
                    0,
                )
                provider.selection = self._default_selection(plan)
                provider.assets = self._selected_assets(
                    plan,
                    provider.selection,
                )
                provider.warnings = [
                    redact_sensitive_text(item, limit=500)
                    for item in plan.warnings[:20]
                ]
                provider.state = "ready"
            except Exception as exc:  # pylint: disable=broad-except
                provider.state = "failed"
                provider.error = redact_sensitive_text(exc, limit=500)
            await self._emit(live, persist=True)

        await asyncio.gather(*(scan(item) for item in live.snapshot.providers))
        live.snapshot.state = (
            "awaiting_selection"
            if any(item.state == "ready" for item in live.snapshot.providers)
            else "failed"
        )
        live.snapshot.phase = (
            "select" if live.snapshot.state == "awaiting_selection" else "done"
        )
        await self._emit(live, persist=True)

    async def _apply(self, live: _LiveJob) -> None:
        for provider in live.snapshot.providers:
            if provider.source not in live.plans:
                continue
            provider.state = "running"
            await self._emit(live, persist=True)

            try:
                service = self._service_factory(live.workspace)
                receipt = await service.apply_selection(
                    provider.plan_id,
                    provider.selection,
                    progress=partial(self._apply_progress, live, provider),
                )
                manifest = self._manifest(live.workspace, receipt)
                keys = {
                    f"{item.asset_type}:{item.source_id}"
                    for item in provider.assets
                }
                provider.assets = project_asset_results(
                    live.plans[provider.source],
                    keys,
                    manifest=manifest,
                    receipt=receipt,
                )
                provider.sessions_processed = provider.sessions_total
                provider.sessions_imported = len(receipt.imported_sessions)
                provider.sessions_skipped = len(receipt.skipped_sessions)
                provider.warnings = (
                    provider.warnings
                    + [
                        redact_sensitive_text(item, limit=500)
                        for item in receipt.warnings
                    ]
                )[:20]
                provider.state = "completed"
            except Exception as exc:  # pylint: disable=broad-except
                provider.error = redact_sensitive_text(exc, limit=500)
                provider.state = "failed"
                for asset in provider.assets:
                    if asset.state is not ImportAssetState.NOT_NEEDED:
                        asset.state = ImportAssetState.FAILED
                        asset.message = "请手动修改相关配置后重试。"
            await self._emit(live, persist=True)
        live.snapshot.state = (
            "completed_with_issues"
            if any(item.state == "failed" for item in live.snapshot.providers)
            else "completed"
        )
        live.snapshot.phase = "done"
        await self._emit(live, persist=True)

    async def _live(self, workspace: Any, job_id: str) -> _LiveJob:
        key = (workspace.agent_id, job_id)
        if key not in self._jobs:
            await self.snapshot(workspace, job_id)
        live = self._jobs[key]
        if not live.plans:
            service = self._service_factory(workspace)
            reader = getattr(service, "_read_plan", None)
            if reader is not None:
                for provider in live.snapshot.providers:
                    if provider.plan_id:
                        live.plans[provider.source] = await reader(
                            provider.plan_id,
                        )
        return live

    async def _emit(self, live: _LiveJob, *, persist: bool = False) -> None:
        live.snapshot.seq += 1
        event = {
            "seq": live.snapshot.seq,
            "snapshot": live.snapshot.model_dump(mode="json"),
        }
        live.events.append(event)
        for queue in live.subscribers:
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(event)
        if persist:
            await self._persist(live.workspace, live.snapshot)

    async def _apply_progress(
        self,
        live: _LiveJob,
        provider: ImportProviderSnapshot,
        message: str,
    ) -> None:
        self._project_progress(provider, message)
        self._log(live, message)
        await self._emit(live)

    async def _persist(
        self,
        workspace: Any,
        snapshot: ImportJobSnapshot,
    ) -> None:
        path = self._path(workspace, snapshot.job_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        await write_json_atomic_async(path, snapshot.model_dump(mode="json"))

    @staticmethod
    def _path(workspace: Any, job_id: str) -> Path:
        if not re.fullmatch(r"import-[0-9a-f]{32}", job_id):
            raise ValueError("invalid import job id")
        return (
            Path(workspace.workspace_dir)
            / ".qwenpaw/imports/jobs"
            / f"{job_id}.json"
        )

    @staticmethod
    def _default_selection(plan: MigrationPlan) -> ImportSelection:
        values: dict[str, Any] = {
            "sessions": any(
                item.asset_type == "session" for item in plan.actions
            ),
        }
        for action in plan.actions:
            selection_field = _TYPE_FIELDS.get(action.asset_type)
            if selection_field:
                values.setdefault(selection_field, []).append(action.source_id)
        return ImportSelection(**values)

    @staticmethod
    def _selected_assets(
        plan: MigrationPlan,
        selection: ImportSelection,
    ) -> list[ImportAssetResult]:
        keys = {
            f"{'cron' if action_type == 'scheduled_task' else action_type}:"
            f"{source_id}"
            for action_type, selection_field in _TYPE_FIELDS.items()
            for source_id in getattr(selection, selection_field)
        }
        return project_asset_results(plan, keys)

    @staticmethod
    def _project_progress(
        provider: ImportProviderSnapshot,
        message: str,
    ) -> None:
        match = _SESSIONS.search(message)
        if match:
            provider.sessions_processed = int(match.group(1))
            provider.sessions_total = max(
                provider.sessions_total,
                int(match.group(2)),
            )
        name = _ASSET_NAME.search(message)
        if name:
            for asset in provider.assets:
                if asset.name == name.group(1):
                    asset.state = ImportAssetState.REPAIRING

    @staticmethod
    def _log(live: _LiveJob, message: str) -> None:
        safe = redact_sensitive_text(message, limit=500)
        live.snapshot.logs = (live.snapshot.logs + [safe])[-50:]

    @staticmethod
    def _manifest(workspace: Any, receipt: ImportReceipt):
        if not receipt.adaptation_manifest:
            return None
        path = Path(receipt.adaptation_manifest)
        if not path.is_absolute():
            path = Path(workspace.workspace_dir) / path
        try:
            return load_manifest(path)
        except (OSError, ValueError):
            return None


__all__ = [
    "ImportJobSnapshot",
    "ImportProviderSnapshot",
    "PortabilityImportJobManager",
]
