# -*- coding: utf-8 -*-
"""Stable models shared by QwenPaw import flows."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ..harnesses.events import HarnessHistoryItem


class SourceLocation(BaseModel):
    """Resolved provider data roots and the evidence used to choose them."""

    provider_id: str
    data_home: str
    data_home_source: str = "default"
    user_data_home: str = ""
    user_data_home_source: str = ""
    runtime_path: str = ""
    data_home_exists: bool = False
    user_data_home_exists: bool = False
    evidence: list[str] = Field(default_factory=list)


class SourceSkill(BaseModel):
    """One provider-owned Skill that can be staged into QwenPaw."""

    source_id: str
    name: str
    directory: Path
    description: str = ""
    scope: str = "provider"


class SourceMCPServer(BaseModel):
    """One external MCP launch configuration ready for safe translation."""

    source_id: str
    name: str
    transport: str = "stdio"
    enabled: bool = False
    command: str = ""
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str = ""
    url: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    auth_status: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class SourceMemoryFile(BaseModel):
    """One immutable Markdown resource owned by an external memory store."""

    source_path: Path
    relative_path: Path


class SourceMemoryProject(BaseModel):
    """Project-scoped external memory resources ready for safe staging."""

    source_id: str
    project_key: str
    cwd: str = ""
    files: list[SourceMemoryFile] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SourceMarketplace(BaseModel):
    """A third-party plugin Marketplace source, not an installed cache."""

    source_id: str
    name: str
    source: str = ""
    source_type: str = "unknown"
    ref_name: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class SourcePlugin(BaseModel):
    """An enabled external plugin that should use native installation."""

    source_id: str
    name: str
    marketplace: str
    version: str = ""
    enabled: bool = True
    install_source: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class SourceScheduledTask(BaseModel):
    """One external scheduled task normalized for safe QwenPaw staging.

    ``enabled`` records the state at the source only.  The compatibility
    workflow decides whether the imported QwenPaw job is enabled or held for
    repair.
    ``unsupported`` is a first-class schedule type so that a task definition
    can be audited without guessing at lossy timing semantics.
    """

    source_id: str
    name: str
    schedule_type: str = "unsupported"
    cron: str = ""
    run_at: datetime | None = None
    timezone: str = "UTC"
    prompt: str = ""
    cwd: str = ""
    enabled: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class SourceSession(BaseModel):
    """Provider-neutral external conversation ready for materialization."""

    source_id: str
    title: str = "Imported conversation"
    cwd: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None
    history: list[HarnessHistoryItem] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProviderInventory(BaseModel):
    """Bounded, read-only inventory returned by a Migration Provider."""

    provider_id: str
    provider_name: str
    detected: bool
    locator: str = ""
    source_location: SourceLocation | None = None
    sessions: list[SourceSession] = Field(default_factory=list)
    ignored_session_ids: list[str] = Field(default_factory=list)
    skills: list[SourceSkill] = Field(default_factory=list)
    mcp_servers: list[SourceMCPServer] = Field(default_factory=list)
    memory_projects: list[SourceMemoryProject] = Field(default_factory=list)
    marketplaces: list[SourceMarketplace] = Field(default_factory=list)
    plugins: list[SourcePlugin] = Field(default_factory=list)
    scheduled_tasks: list[SourceScheduledTask] = Field(default_factory=list)
    discovered_mcp_count: int = 0
    discovered_scheduled_task_count: int = 0
    warnings: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class MigrationAssetPlan(BaseModel):
    """One reviewable action in a provider migration plan."""

    asset_type: str
    source_id: str
    name: str
    action: str
    fidelity: str
    reason_zh: str = ""


class MigrationPlan(BaseModel):
    """Persisted dry-run plan that can be verified again before apply."""

    plan_id: str
    schema_version: str = "1"
    source: str
    source_home: str = ""
    source_location: SourceLocation | None = None
    agent_id: str
    created_at: datetime
    inventory_fingerprint: str
    inventory_counts: dict[str, int] = Field(default_factory=dict)
    actions: list[MigrationAssetPlan] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    state: str = "ready"
    migration_id: str = ""


class MigrationDoctorCheck(BaseModel):
    """One Chinese post-import verification result."""

    category: str
    status: str
    title_zh: str
    detail_zh: str


class MigrationDoctorReport(BaseModel):
    """Post-import health report attached to an import receipt."""

    status: str
    summary_zh: str
    checked_at: datetime
    checks: list[MigrationDoctorCheck] = Field(default_factory=list)


class ImportReceipt(BaseModel):
    """Durable receipt for one additive provider migration."""

    migration_id: str
    plan_id: str = ""
    schema_version: str = "1"
    source: str
    source_locator: str = ""
    agent_id: str
    started_at: datetime
    completed_at: datetime
    imported_sessions: list[str] = Field(default_factory=list)
    skipped_sessions: list[str] = Field(default_factory=list)
    ignored_source_sessions: list[str] = Field(default_factory=list)
    archived_internal_sessions: list[str] = Field(default_factory=list)
    imported_skills: list[str] = Field(default_factory=list)
    skipped_skills: list[str] = Field(default_factory=list)
    imported_mcp_servers: list[str] = Field(default_factory=list)
    skipped_mcp_servers: list[str] = Field(default_factory=list)
    imported_memory_projects: list[str] = Field(default_factory=list)
    skipped_memory_projects: list[str] = Field(default_factory=list)
    restored_marketplaces: list[str] = Field(default_factory=list)
    skipped_marketplaces: list[str] = Field(default_factory=list)
    prepared_plugins: list[str] = Field(default_factory=list)
    installed_plugins: list[str] = Field(default_factory=list)
    skipped_plugins: list[str] = Field(default_factory=list)
    imported_scheduled_tasks: list[str] = Field(default_factory=list)
    skipped_scheduled_tasks: list[str] = Field(default_factory=list)
    discovered_mcp_count: int = 0
    discovered_scheduled_task_count: int = 0
    adaptation_status: str = "not_run"
    adaptation_manifest: str = ""
    adaptation_summary: str = ""
    adaptation_counts: dict[str, int] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    doctor_report: MigrationDoctorReport | None = None


__all__ = [
    "ImportReceipt",
    "MigrationAssetPlan",
    "MigrationDoctorCheck",
    "MigrationDoctorReport",
    "MigrationPlan",
    "ProviderInventory",
    "SourceMarketplace",
    "SourceMemoryFile",
    "SourceMemoryProject",
    "SourcePlugin",
    "SourceScheduledTask",
    "SourceSession",
    "SourceSkill",
    "SourceMCPServer",
    "SourceLocation",
]
