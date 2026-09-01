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
from .importer import ImportRollbackError, ProviderImportService
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
_READY_TO_IMPORT = "兼容性优化完成，已进入待迁移区。"
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
    mode: str = "import"
    retry_of_job_id: str = ""
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
    retry_selections: dict[str, ImportSelection] = dataclass_field(
        default_factory=dict,
    )
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
        self._closing = False

    async def create(
        self,
        workspace: Any,
        sources: list[str],
    ) -> ImportJobSnapshot:
        """Create a job and start source discovery in the background."""
        self._ensure_open()
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
        self._spawn(live, self._scan(live))
        return snapshot.model_copy(deep=True)

    async def start(
        self,
        workspace: Any,
        job_id: str,
        selections: dict[str, ImportSelection],
    ) -> ImportJobSnapshot:
        """Apply the user selection for every successfully scanned source."""
        self._ensure_open()
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
        if not any(
            selection.sessions
            or any(
                getattr(selection, field) for field in _TYPE_FIELDS.values()
            )
            for selection in selections.values()
        ):
            raise ValueError("select at least one conversation or tool")
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
        self._spawn(live, self._apply(live))
        return live.snapshot.model_copy(deep=True)

    async def retry(
        self,
        workspace: Any,
        job_id: str,
        selections: dict[str, ImportSelection],
    ) -> ImportJobSnapshot:
        """Retry explicitly selected failed tools in a fresh import job."""
        self._ensure_open()
        previous = await self._live(workspace, job_id)
        if previous.snapshot.state not in _TERMINAL:
            raise RuntimeError("import job has not finished")
        if any(
            item.snapshot.agent_id == workspace.agent_id
            and item.snapshot.state == "running"
            for item in self._jobs.values()
        ):
            raise RuntimeError("an import is already running for this agent")
        selected_sources = {
            source
            for source, selection in selections.items()
            if self._tool_ids(selection)
        }
        if not selected_sources or selected_sources != set(selections):
            raise ValueError("retry requires at least one selected tool")
        providers = {item.source: item for item in previous.snapshot.providers}
        if not selected_sources <= set(providers):
            raise ValueError("retry source is not part of the original import")
        for source, selection in selections.items():
            self._validate_retry_selection(providers[source], selection)
        retry_id = f"import-{uuid4().hex}"
        snapshot = previous.snapshot.model_copy(deep=True)
        snapshot = snapshot.model_copy(
            update={
                "job_id": retry_id,
                "state": "running",
                "phase": "retry",
                "mode": "retry",
                "retry_of_job_id": job_id,
                "seq": 0,
            },
        )
        for provider in snapshot.providers:
            selection = selections.get(provider.source)
            if selection is None:
                continue
            pending = {
                (item.asset_type, item.source_id): item
                for item in self._selected_assets(
                    previous.plans[provider.source],
                    selection,
                    force_retry=True,
                )
            }
            provider.assets = [
                pending.get((item.asset_type, item.source_id), item)
                for item in provider.assets
            ]
            provider.state = "pending"
            provider.error = ""
        live = _LiveJob(
            workspace=workspace,
            snapshot=snapshot,
            plans=dict(previous.plans),
            retry_selections=selections,
        )
        self._jobs[(workspace.agent_id, retry_id)] = live
        await self._emit(live, persist=True)
        self._spawn(live, self._apply(live, retry_from=previous))
        return snapshot.model_copy(deep=True)

    async def shutdown(self) -> None:
        """Stop active jobs before their workspace services close."""
        self._closing = True
        await asyncio.gather(
            *(
                self.cancel(live.workspace, live.snapshot.job_id)
                for live in tuple(self._jobs.values())
                if live.snapshot.state not in _TERMINAL
            ),
            return_exceptions=True,
        )

    async def cancel(
        self,
        workspace: Any,
        job_id: str,
    ) -> ImportJobSnapshot:
        """Stop one active import and persist its terminal state."""
        live = await self._live(workspace, job_id)
        if live.snapshot.state in _TERMINAL:
            return live.snapshot.model_copy(deep=True)
        cancel_error = ""
        if live.task is not None and not live.task.done():
            live.task.cancel()
            try:
                await live.task
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # pylint: disable=broad-except
                cancel_error = redact_sensitive_text(exc, limit=500)
        if live.snapshot.state not in _TERMINAL:
            live.snapshot.state = "failed" if cancel_error else "interrupted"
            live.snapshot.phase = "done"
            if cancel_error:
                self._log(live, cancel_error)
                for provider in live.snapshot.providers:
                    if provider.state == "running":
                        provider.state = "failed"
                        provider.error = cancel_error
            await self._emit(live, persist=True)
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

    def _ensure_open(self) -> None:
        if self._closing:
            raise RuntimeError("import service is shutting down")

    def _spawn(self, live: _LiveJob, operation: Any) -> None:
        async def run() -> None:
            try:
                await operation
            except Exception as exc:  # pylint: disable=broad-except
                if live.snapshot.state not in _TERMINAL:
                    live.snapshot.state = "failed"
                    live.snapshot.phase = "done"
                    self._log(live, redact_sensitive_text(exc, limit=500))
                    for provider in live.snapshot.providers:
                        if provider.state == "running":
                            provider.state = "failed"
                            provider.error = redact_sensitive_text(
                                exc,
                                limit=500,
                            )
                    await self._emit(live, persist=True)

        live.task = asyncio.create_task(run())

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

    async def _apply(  # pylint: disable=too-many-branches
        self,
        live: _LiveJob,
        *,
        retry_from: _LiveJob | None = None,
    ) -> None:
        for provider in live.snapshot.providers:
            if retry_from is None and provider.source not in live.plans:
                continue
            retry_selection = live.retry_selections.get(provider.source)
            if retry_from is not None and retry_selection is None:
                continue
            provider.state = "running"
            await self._emit(live, persist=True)

            try:
                service = self._service_factory(live.workspace)
                if retry_from is None:
                    plan = live.plans[provider.source]
                    receipt = await service.apply_selection(
                        provider.plan_id,
                        provider.selection,
                        progress=partial(
                            self._apply_progress,
                            live,
                            provider,
                        ),
                    )
                else:
                    plan, receipt = await service.retry_selection(
                        retry_from.plans[provider.source].plan_id,
                        retry_selection,
                        progress=partial(
                            self._apply_progress,
                            live,
                            provider,
                        ),
                    )
                    live.plans[provider.source] = plan
                    provider.plan_id = plan.plan_id
                manifest = self._manifest(live.workspace, receipt)
                keys = (
                    self._tool_ids(retry_selection)
                    if retry_selection is not None
                    else {
                        f"{item.asset_type}:{item.source_id}"
                        for item in provider.assets
                    }
                )
                results = project_asset_results(
                    plan,
                    keys,
                    manifest=manifest,
                    receipt=receipt,
                    force_retry=retry_from is not None,
                )
                if retry_from is None:
                    provider.assets = results
                else:
                    updates = {
                        (item.asset_type, item.source_id): item
                        for item in results
                    }
                    provider.assets = [
                        updates.get((item.asset_type, item.source_id), item)
                        for item in provider.assets
                    ]
                if retry_from is None:
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
                retry_keys = (
                    self._tool_ids(retry_selection)
                    if retry_selection is not None
                    else None
                )
                for asset in provider.assets:
                    if (
                        retry_keys is None
                        and asset.state is not ImportAssetState.NOT_NEEDED
                    ) or (
                        retry_keys is not None
                        and f"{asset.asset_type}:{asset.source_id}"
                        in retry_keys
                    ):
                        asset.state = ImportAssetState.FAILED
                        asset.message = "请手动修改相关配置后重试。"
                if isinstance(exc, ImportRollbackError):
                    if exc.cancelled:
                        for asset in provider.assets:
                            if asset.state is ImportAssetState.FAILED:
                                asset.message = "回滚未完成，请根据错误信息人工检查。"
                        await self._emit(live, persist=True)
                        raise
            await self._emit(live, persist=True)
        live.snapshot.state = (
            "completed_with_issues"
            if any(
                item.state == "failed"
                or any(
                    asset.state is ImportAssetState.FAILED
                    for asset in item.assets
                )
                for item in live.snapshot.providers
            )
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
        if not message.startswith("\x1e"):
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
        *,
        force_retry: bool = False,
    ) -> list[ImportAssetResult]:
        keys = {
            f"{'cron' if action_type == 'scheduled_task' else action_type}:"
            f"{source_id}"
            for action_type, selection_field in _TYPE_FIELDS.items()
            for source_id in getattr(selection, selection_field)
        }
        return project_asset_results(plan, keys, force_retry=force_retry)

    @staticmethod
    def _tool_ids(selection: ImportSelection) -> set[str]:
        if selection.sessions:
            raise ValueError("retry does not support sessions")
        return {
            f"{'cron' if asset_type == 'scheduled_task' else asset_type}:"
            f"{source_id}"
            for asset_type, field in _TYPE_FIELDS.items()
            for source_id in getattr(selection, field)
        }

    @classmethod
    def _validate_retry_selection(
        cls,
        provider: ImportProviderSnapshot,
        selection: ImportSelection,
    ) -> None:
        failed = {
            f"{item.asset_type}:{item.source_id}"
            for item in provider.assets
            if item.state is ImportAssetState.FAILED
        }
        requested = cls._tool_ids(selection)
        if not requested <= failed:
            raise ValueError("retry only accepts tools that previously failed")

    @staticmethod
    def _project_progress(
        provider: ImportProviderSnapshot,
        message: str,
    ) -> None:
        result = message.split("\t")
        if len(result) == 5 and result[0] == "\x1esessions":
            (
                provider.sessions_processed,
                provider.sessions_total,
                provider.sessions_imported,
                provider.sessions_skipped,
            ) = map(int, result[1:])
            return
        if len(result) == 5 and result[0] == "\x1easset":
            asset_type, state, enabled, source_id = result[1:]
            for asset in provider.assets:
                if (
                    asset.asset_type == asset_type
                    and asset.source_id == source_id
                ):
                    if asset.state is not ImportAssetState.NOT_NEEDED:
                        asset.state = ImportAssetState(state)
                        asset.enabled = (
                            None if enabled == "-" else enabled == "1"
                        )
                    return
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
                if asset.name == name.group(1) and asset.state in {
                    ImportAssetState.PENDING,
                    ImportAssetState.REPAIRING,
                }:
                    asset.state = ImportAssetState.REPAIRING
                    asset.reason_code = (
                        "ready_to_import"
                        if message.endswith(_READY_TO_IMPORT)
                        else ""
                    )

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
