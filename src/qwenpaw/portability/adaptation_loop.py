# -*- coding: utf-8 -*-
"""Parallel Agent triage followed by Mission-mode testing and repair."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import stat
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ..agents.tools.agent_management import MAX_SPAWN_BATCH_CONCURRENCY
from ..modes.mission import MissionMode
from ..utils.io_utils import run_sync_io
from .adaptation_mission import prepare_mission, sync_mission
from .adaptation_phase import (
    REPAIR_PHASE,
    TRIAGE_PHASE,
    AccessBinding,
    AdaptationAccessGuard,
    PhaseOutcome,
    PhaseRunner,
)
from .adaptation_staging import component_map, stage_local_assets
from .compatibility import (
    AssetType,
    AssetZone,
    CompatibilityAsset,
    CompatibilityStore,
    counts,
    load_manifest,
    mcp_inline_secret_risks,
    redact_sensitive_text,
    write_summary,
)
from .compatibility_testing import (
    ADAPTATION_TEXT_SUFFIXES,
    CompatibilityTester,
    find_source,
)
from .models import ProviderInventory
from .providers.base import ProgressReporter, report_progress as _report

_MAX_FILE_BYTES = 256 * 1024
_EXCERPT_BYTES = 16 * 1024
_MAX_REPLACEMENT_BYTES = 32 * 1024


@dataclass(frozen=True)
class AdaptationResult:
    manifest_path: Path
    summary_path: Path
    status: str
    counts: dict[str, int]
    asset_zones: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


_ACCESS_GUARD = AdaptationAccessGuard()


def get_active_adaptation_context() -> "ActiveAdaptationContext":
    return _ACCESS_GUARD.current().context


class ActiveAdaptationContext:
    """In-memory capability shared by isolated migration workers."""

    def __init__(
        self,
        *,
        inventory: ProviderInventory,
        store: CompatibilityStore,
        tester: CompatibilityTester,
        staging_root: Path,
        audit_path: Path,
        progress: ProgressReporter | None = None,
    ) -> None:
        self.inventory = inventory
        self.store = store
        self.tester = tester
        self.staging_root = staging_root.resolve()
        self.audit_path = audit_path
        self.progress = progress
        self._activities: dict[str, str] = {}
        self.tool_calls = 0
        manifest = load_manifest(store.path)
        self.total_tool_budget = sum(
            item.tool_budget for item in manifest.assets
        )
        self._lock = asyncio.Lock()

    def _binding(self) -> AccessBinding:
        return _ACCESS_GUARD.current(expected_context=self)

    @property
    def phase(self) -> str:
        return self._binding().phase.name

    @property
    def active_asset_key(self) -> str:
        return self._binding().asset_key

    def activity(self, session_id: str) -> str:
        return self._activities.get(session_id, "等待 Agent 开始处理。")

    def clear_activity(self, session_id: str) -> None:
        self._activities.pop(session_id, None)

    async def _publish(self, message: str) -> None:
        from ..app.agent_context import get_current_session_id

        session_id = get_current_session_id() or ""
        self._activities[session_id] = message
        await _report(self.progress, message)

    @staticmethod
    def _label(asset: CompatibilityAsset) -> str:
        kind = {
            AssetType.SKILL: "Skill",
            AssetType.MCP: "MCP",
            AssetType.PLUGIN: "插件",
            AssetType.SCHEDULED_TASK: "定时任务",
        }[asset.asset_type]
        name = redact_sensitive_text(asset.name, limit=120).replace("\n", " ")
        return f"{kind}「{name}」"

    def _consume(
        self,
        key: str = "",
        *,
        final: bool = False,
    ) -> None:
        if key and key != self.active_asset_key:
            raise PermissionError("worker may access only its assigned asset")
        self.tool_calls += 1
        if self.tool_calls > self.total_tool_budget:
            raise RuntimeError("migration tool-call budget exhausted")
        if key:
            self.store.consume(key, reserve=0 if final else 1)

    def _asset(self, key: str) -> CompatibilityAsset:
        return load_manifest(self.store.path).get_asset(key)

    def _require_repair_phase(self) -> None:
        if not self._binding().phase.mutable:
            raise PermissionError("compatibility changes require repair phase")

    def _audit(self, action: str, key: str, detail: str = "") -> None:
        self.audit_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        record = {
            "at": datetime.now().astimezone().isoformat(),
            "action": action,
            "asset_key": key,
            "detail": detail[:200],
        }
        descriptor = os.open(
            self.audit_path,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o600,
        )
        try:
            os.write(
                descriptor,
                (json.dumps(record, ensure_ascii=False) + "\n").encode(),
            )
        finally:
            os.close(descriptor)

    def _asset_root(self, asset: CompatibilityAsset) -> Path:
        source = find_source(self.inventory, asset)
        if asset.asset_type is AssetType.SKILL:
            root = Path(source.directory).resolve(strict=True)
            staging = self.staging_root / "skills"
        elif asset.asset_type is AssetType.PLUGIN:
            root = Path(source.install_source).resolve(strict=True)
            staging = self.staging_root / "plugins"
        else:
            raise ValueError("asset has no readable file tree")
        if not root.is_relative_to(staging.resolve()):
            raise PermissionError("asset is outside the staging area")
        return root

    @staticmethod
    def _asset_file(root: Path, relative_path: str) -> Path:
        relative = Path(relative_path)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or relative.suffix.lower() not in ADAPTATION_TEXT_SUFFIXES
        ):
            raise PermissionError("asset path is not editable")
        path = root / relative
        parent = path.parent.resolve(strict=False)
        if not parent.is_relative_to(root):
            raise PermissionError("asset path escapes staging")
        if path.exists() and (
            path.is_symlink()
            or not path.is_file()
            or not path.resolve(strict=True).is_relative_to(root)
        ):
            raise PermissionError("asset path is not a regular staged file")
        return path

    @staticmethod
    def _excerpt(path: Path, size: int) -> str:
        with path.open("rb") as stream:
            head = stream.read(_EXCERPT_BYTES)
            if size <= _EXCERPT_BYTES:
                tail = b""
            else:
                stream.seek(max(_EXCERPT_BYTES, size - _EXCERPT_BYTES))
                tail = stream.read(_EXCERPT_BYTES)
        excerpt = head.decode("utf-8", errors="replace")
        if tail:
            excerpt += "\n\n[... middle omitted ...]\n\n"
            excerpt += tail.decode("utf-8", errors="replace")
        return redact_sensitive_text(excerpt)

    async def inspect_asset(self, key: str) -> dict[str, Any]:
        async with self._lock:
            self._consume(key)
            asset = self._asset(key)
            await self._publish(
                f"正在分析 {self._label(asset)} 的功能和来源生态依赖…",
            )
            result = {"ok": True, **self.tester.inspect(asset)}
            self.store.record_inspection(key)
            return result

    async def read_file(
        self,
        key: str,
        relative_path: str,
        *,
        start_line: int = 1,
        end_line: int = 240,
    ) -> dict[str, Any]:
        async with self._lock:
            asset = self._asset(key)
            self._consume(key)
            await self._publish(
                f"正在阅读 {self._label(asset)}：{relative_path}",
            )
            path = self._asset_file(self._asset_root(asset), relative_path)
            if not path.is_file():
                raise FileNotFoundError(relative_path)
            size = path.stat().st_size
            if size > _MAX_FILE_BYTES:
                self.store.record_read(
                    key,
                    relative_path,
                    started=True,
                    finished=True,
                )
                return {
                    "ok": True,
                    "path": relative_path,
                    "read_mode": "bounded_excerpt",
                    "size_bytes": size,
                    "text": self._excerpt(path, size),
                    "has_more": False,
                    "next_line": None,
                }
            lines = redact_sensitive_text(
                path.read_text(encoding="utf-8"),
            ).splitlines()
            start = max(1, start_line)
            end = min(len(lines), max(start, end_line), start + 399)
            finished = end >= len(lines)
            self.store.record_read(
                key,
                relative_path,
                started=start == 1,
                finished=finished,
            )
            return {
                "ok": True,
                "path": relative_path,
                "text": "\n".join(
                    f"{number}: {lines[number - 1]}"
                    for number in range(start, end + 1)
                ),
                "has_more": not finished,
                "next_line": end + 1 if not finished else None,
            }

    async def write_file(
        self,
        key: str,
        relative_path: str,
        content: str,
    ) -> dict[str, Any]:
        async with self._lock:
            self._require_repair_phase()
            asset = self._asset(key)
            self._consume(key)
            if asset.zone is not AssetZone.REPAIR:
                raise RuntimeError("files can only be changed in repair")
            if len(content.encode()) > _MAX_FILE_BYTES:
                raise ValueError("updated text file is too large")
            if redact_sensitive_text(content) != content:
                raise ValueError("updated text contains a possible secret")
            await self._publish(
                f"正在兼容性优化 {self._label(asset)}：{relative_path}",
            )
            path = self._asset_file(self._asset_root(asset), relative_path)
            path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(dir=path.parent)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.chmod(
                    temporary,
                    (
                        stat.S_IMODE(path.stat().st_mode)
                        if path.exists()
                        else 0o600
                    ),
                )
                os.replace(temporary, path)
            finally:
                Path(temporary).unlink(missing_ok=True)
            self.store.mark_changed(key, f"写入 {relative_path}")
            self._audit("write_file", key, relative_path)
            await self._publish(
                f"已修改 {self._label(asset)}，等待重新兼容性测试。",
            )
            return {"ok": True, "path": relative_path, "zone": "repair"}

    async def update_asset(  # pylint: disable=too-many-branches
        self,
        key: str,
        field_name: str,
        value_json: str,
    ) -> dict[str, Any]:
        async with self._lock:
            self._require_repair_phase()
            self._consume(key)
            if len(value_json.encode()) > _MAX_REPLACEMENT_BYTES:
                raise ValueError("updated value is too large")
            asset = self._asset(key)
            if asset.zone is not AssetZone.REPAIR:
                raise RuntimeError("assets can only be changed in repair")
            await self._publish(
                f"正在兼容性优化 {self._label(asset)}：{field_name}",
            )
            source = find_source(self.inventory, asset)
            value = json.loads(value_json)
            if asset.asset_type is AssetType.MCP:
                allowed = {"command", "args", "cwd", "url", "transport"}
            elif asset.asset_type is AssetType.SCHEDULED_TASK:
                allowed = {
                    "prompt",
                    "cron",
                    "timezone",
                    "cwd",
                    "schedule_type",
                    "run_at",
                }
            else:
                raise PermissionError("asset has no structured repair surface")
            if field_name not in allowed:
                raise PermissionError("field is not editable")
            if field_name == "args":
                if not isinstance(value, list) or any(
                    not isinstance(item, str) for item in value
                ):
                    raise ValueError("args must be a string list")
            elif field_name == "run_at":
                value = datetime.fromisoformat(value.replace("Z", "+00:00"))
            elif not isinstance(value, str):
                raise ValueError("field must be a string")
            if (
                field_name == "prompt"
                and redact_sensitive_text(value) != value
            ):
                raise ValueError("updated prompt contains a secret")
            candidate = source.model_copy(update={field_name: value})
            if asset.asset_type is AssetType.MCP and mcp_inline_secret_risks(
                candidate.command,
                candidate.args,
                candidate.url,
                candidate.env,
                candidate.headers,
                candidate.cwd,
            ):
                raise ValueError("updated MCP binding contains a secret")
            setattr(source, field_name, value)
            if (
                asset.asset_type is AssetType.SCHEDULED_TASK
                and field_name == "cwd"
                and value
                and Path(value).expanduser().is_dir()
            ):
                source.metadata.update(
                    {
                        "remote_unverified": False,
                        "workspace_status": "local",
                        "execution_environment": "local",
                        "source_target_remote_authority": "",
                        "target_remote_authority": "",
                    },
                )
            self.store.mark_changed(key, f"更新字段 {field_name}")
            self._audit("update_asset", key, field_name)
            await self._publish(
                f"已更新 {self._label(asset)}，等待重新兼容性测试。",
            )
            return {"ok": True, "zone": "repair"}

    async def test_asset(self, key: str) -> dict[str, Any]:
        async with self._lock:
            self._require_repair_phase()
            self._consume(key)
            asset = self._asset(key)
            await self._publish(
                f"正在测试 {self._label(asset)} 的 QwenPaw 兼容性…",
            )
            result = self.tester.test(asset)
            self._audit("test", key, "passed" if result.passed else "failed")
            outcome = "测试通过，可以迁移。" if result.passed else "测试未通过，需要继续兼容性修复。"
            await self._publish(f"{self._label(asset)}{outcome}")
            return {
                "ok": True,
                "passed": result.passed,
                "summary": result.summary,
                "evidence": result.evidence,
            }

    async def classify_asset(
        self,
        key: str,
        zone: str,
        reason: str,
        plugin_disposition: str = "",
        component_assessments_json: str = "{}",
    ) -> dict[str, Any]:
        async with self._lock:
            asset = self._asset(key)
            expected = self._binding().phase.source_zone
            if asset.zone is not expected:
                raise PermissionError(
                    f"{self.phase} phase cannot classify {asset.zone.value}",
                )
            self._consume(
                key,
                final=True,
            )
            selected = AssetZone(zone)
            assessments = json.loads(component_assessments_json)
            if not isinstance(assessments, dict):
                raise ValueError("component assessments must be an object")
            manifest = self.store.classify(
                key,
                selected,
                reason,
                plugin_disposition=plugin_disposition,
                component_assessments=assessments,
            )
            self._audit("classify", key, selected.value)
            status = {
                AssetZone.REPAIR: "语义评估完成：可以进行兼容性修复。",
                AssetZone.DISCARD: "语义评估完成：无需迁移。",
                AssetZone.MIGRATE: "兼容性优化完成，已进入待迁移区。",
            }[selected]
            await self._publish(f"{self._label(self._asset(key))}{status}")
            return {
                "ok": True,
                "zone": selected.value,
                "counts": counts(manifest),
            }


def _mission_mode(workspace: Any) -> MissionMode:
    matches = [
        mode
        for mode in getattr(workspace.plugins, "modes", ())
        if isinstance(mode, MissionMode)
    ]
    if len(matches) != 1:
        raise RuntimeError("workspace does not have exactly one MissionMode")
    return matches[0]


async def _triage_asset(
    runner: PhaseRunner,
    context: ActiveAdaptationContext,
    key: str,
    warnings: list[str],
) -> None:
    session_id = f"migration-triage:{secrets.token_urlsafe(24)}"
    while True:
        asset = context._asset(key)  # pylint: disable=protected-access
        if asset.zone is not AssetZone.STAGING or asset.budget_exhausted:
            return
        before = asset.tool_calls
        label = context._label(asset)  # pylint: disable=protected-access
        try:
            await runner.run_asset(
                asset,
                TRIAGE_PHASE,
                session_id=session_id,
                label=f"正在检查 {label}",
            )
        except Exception as exc:  # pylint: disable=broad-except
            warnings.append(
                f"{label}语义判断失败：{type(exc).__name__}: {exc}",
            )
            current = context._asset(key)  # pylint: disable=protected-access
            if (
                current.zone is AssetZone.STAGING
                and current.tool_calls > before
                and not current.budget_exhausted
            ):
                await _report(
                    context.progress,
                    f"{label}保留已读进度，继续完成语义判断。",
                )
                continue
            return
        current = context._asset(key)  # pylint: disable=protected-access
        if current.zone is not AssetZone.STAGING:
            return
        if current.tool_calls == before:
            warnings.append(f"{label}语义判断未产生进展")
            return


async def _triage_assets(
    runner: PhaseRunner,
    context: ActiveAdaptationContext,
    warnings: list[str],
) -> PhaseOutcome:
    keys = [
        item.asset_key
        for item in load_manifest(context.store.path).by_zone(
            AssetZone.STAGING,
        )
    ]
    await runner.run_batch(
        keys,
        lambda key: _triage_asset(runner, context, key, warnings),
    )
    remaining = len(
        load_manifest(context.store.path).by_zone(AssetZone.STAGING),
    )
    if not remaining:
        return PhaseOutcome(completed=True)
    if context.tool_calls >= context.total_tool_budget:
        reason = f"第一阶段达到总工具调用上限（{context.total_tool_budget} 次）。"
    else:
        reason = f"第一阶段仍有 {remaining} 项未完成语义判断。"
    return PhaseOutcome(False, remaining, reason)


async def _repair_asset(
    runner: PhaseRunner,
    context: ActiveAdaptationContext,
    key: str,
    warnings: list[str],
) -> None:
    asset = context._asset(key)  # pylint: disable=protected-access
    label = context._label(asset)  # pylint: disable=protected-access
    try:
        await runner.run_asset(
            asset,
            REPAIR_PHASE,
            session_id=f"migration-worker:{secrets.token_urlsafe(24)}",
            label=f"Mission 正在修复 {label}",
        )
    except Exception as exc:  # pylint: disable=broad-except
        warnings.append(f"{label}修复失败：{type(exc).__name__}: {exc}")


async def _repair_with_mission(
    workspace: Any,
    runner: PhaseRunner,
    context: ActiveAdaptationContext,
    root: Path,
    warnings: list[str],
) -> PhaseOutcome:
    mode = _mission_mode(workspace)
    max_attempts = mode.max_retries_per_story + 1
    session_id = f"migration-mission:{secrets.token_urlsafe(24)}"
    manifest = load_manifest(context.store.path)
    loop_dir = await run_sync_io(
        prepare_mission,
        root,
        manifest,
        session_id,
        max_attempts,
    )
    attempts: dict[str, int] = {}
    with mode.internal_mission(session_id, loop_dir) as mission:
        for round_number in range(1, max_attempts + 1):
            manifest = load_manifest(context.store.path)
            pending = [
                item.asset_key
                for item in manifest.by_zone(AssetZone.REPAIR)
                if not item.budget_exhausted
                and attempts.get(item.asset_key, 0) < max_attempts
            ]
            if not pending:
                break
            await _report(
                context.progress,
                f"Mission 第 {round_number}/{max_attempts} 轮："
                f"以 {MAX_SPAWN_BATCH_CONCURRENCY} 个并行 Agent "
                f"处理 {len(pending)} 项资产。",
            )
            for key in pending:
                attempts[key] = attempts.get(key, 0) + 1
            await runner.run_batch(
                pending,
                lambda key: _repair_asset(runner, context, key, warnings),
            )
            manifest = load_manifest(context.store.path)
            await run_sync_io(sync_mission, loop_dir, manifest)
            if await mission.check():
                return PhaseOutcome(completed=True)
        manifest = load_manifest(context.store.path)
        await run_sync_io(sync_mission, loop_dir, manifest, stopped=True)
        await mission.check()
        remaining = len(manifest.by_zone(AssetZone.REPAIR))
        return PhaseOutcome(
            completed=False,
            remaining=remaining,
            reason=(
                f"兼容性修复 Mission 已达到每项最多 {max_attempts} 次尝试，"
                f"仍有 {remaining} 项未通过原生检查。"
            ),
        )


async def run_adaptation_loop(
    workspace: Any,
    inventory: ProviderInventory,
    migration_id: str,
    progress: ProgressReporter | None = None,
) -> AdaptationResult:
    root = (
        Path(workspace.workspace_dir)
        / ".qwenpaw"
        / "imports"
        / migration_id
        / "adaptation"
    )
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    manifest_path = root / "manifest.json"
    summary_path = root / "summary.md"
    staging_root = root / "staging"
    warnings = await run_sync_io(stage_local_assets, inventory, staging_root)
    store = CompatibilityStore(manifest_path)
    components = await run_sync_io(component_map, inventory)
    manifest = store.prepare(
        migration_id=migration_id,
        source=inventory.provider_id,
        skills=inventory.skills,
        mcp_servers=inventory.mcp_servers,
        plugins=inventory.plugins,
        scheduled_tasks=inventory.scheduled_tasks,
        components=components,
    )
    await _report(
        progress,
        f"工具和设置已进入安全暂存区，共 {len(manifest.assets)} 项；"
        f"正在由 {MAX_SPAWN_BATCH_CONCURRENCY} 个隔离 Agent "
        "并行进行语义判断…",
    )
    if not manifest.assets:
        manifest = store.finish()
        write_summary(summary_path, manifest)
        return AdaptationResult(
            manifest_path,
            summary_path,
            manifest.state.value,
            counts(manifest),
        )

    tester = CompatibilityTester(workspace, inventory, store)
    context = ActiveAdaptationContext(
        inventory=inventory,
        store=store,
        tester=tester,
        staging_root=staging_root,
        audit_path=root / "progress.jsonl",
        progress=progress,
    )
    runner = PhaseRunner(workspace, context, _ACCESS_GUARD)
    await _report(
        progress,
        f"两阶段共享 {context.total_tool_budget} 次工具调用；" + "不设总时长限制，推理上限按剩余预算动态分配。",
    )
    outcome = await _triage_assets(runner, context, warnings)
    stopped_reason = outcome.reason

    if outcome.completed:
        manifest = load_manifest(store.path)
        if manifest.by_zone(AssetZone.REPAIR):
            await _report(
                progress,
                "第一阶段完成，安全暂存区已清空；正在启动 QwenPaw " + "Mission 并行进行兼容性测试与修复…",
            )
            try:
                outcome = await _repair_with_mission(
                    workspace,
                    runner,
                    context,
                    root,
                    warnings,
                )
                stopped_reason = outcome.reason
                if not outcome.completed:
                    warnings.append(stopped_reason)
            except Exception as exc:  # pylint: disable=broad-except
                stopped_reason = (
                    f"无法完成 QwenPaw Mission：{type(exc).__name__}: {exc}"
                )
                warnings.append(stopped_reason)
        else:
            await _report(
                progress,
                "第一阶段完成；没有需要兼容性修复的资产，无需启动 Mission。",
            )

    complete, reason = store.complete()
    if not complete and not stopped_reason:
        if context.tool_calls >= context.total_tool_budget:
            stopped_reason = (
                "兼容性迁移达到总工具调用上限（" + f"{context.total_tool_budget} 次）。"
            )
        else:
            stopped_reason = reason
    manifest = store.finish(
        stopped=not complete,
        reason="" if complete else stopped_reason,
    )
    write_summary(summary_path, manifest)
    await _report(
        progress,
        "兼容性迁移已结束："
        f"待迁移 {len(manifest.by_zone(AssetZone.MIGRATE))}，"
        f"待修复 {len(manifest.by_zone(AssetZone.REPAIR))}，"
        f"丢弃 {len(manifest.by_zone(AssetZone.DISCARD))}。",
    )
    return AdaptationResult(
        manifest_path=manifest_path,
        summary_path=summary_path,
        status=manifest.state.value,
        counts=counts(manifest),
        asset_zones={
            item.asset_key: item.zone.value for item in manifest.assets
        },
        warnings=warnings,
    )
