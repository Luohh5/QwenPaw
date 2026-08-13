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
    ProviderImportService,
    export_to_backup,
    export_trace,
    import_backup_path,
    provider_names,
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


class ImportCommandHandler(BaseControlCommandHandler):
    """Import a local backup archive or a supported Harness source."""

    command_name = "/import"
    description = "Import Codex/Qoder data or a QwenPaw backup ZIP"

    async def handle(self, context: ControlContext) -> str:
        _require_local_console(context)
        raw = str(context.args.get("_raw_args") or "").strip()
        if raw.lower() in {"", "help", "-h", "--help"}:
            return IMPORT_HELP
        tokens = _tokens(raw)
        if len(tokens) < 2 or tokens[0].lower() != "from":
            return f"Usage: `/import from <source>`\n\n{IMPORT_HELP}"
        unexpected = [item for item in tokens[2:] if not item.startswith("--")]
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
        source = tokens[1]
        path = Path(source).expanduser()
        looks_like_path = (
            path.suffix.lower() == ".zip"
            or path.is_absolute()
            or "/" in source
            or "\\" in source
        )
        if looks_like_path:
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
                    trust_hint = (
                        "\n\nIf you trust its origin, retry with "
                        "`--trust-foreign`."
                    )
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

        if flags:
            raise ValueError(
                "Backup trust/conflict options apply only to ZIP imports.",
            )
        receipt = await ProviderImportService(context.workspace).import_from(
            source,
            progress=context.report_progress,
        )
        warnings = ""
        if receipt.warnings:
            warnings = "\n\n**Notes**\n" + "\n".join(
                f"- {item}" for item in receipt.warnings
            )
        return (
            f"**{receipt.source.title()} import complete**\n\n"
            f"- Sessions imported: {len(receipt.imported_sessions)}\n"
            f"- Sessions already present/skipped: "
            f"{len(receipt.skipped_sessions)}\n"
            f"- Skills imported disabled: {len(receipt.imported_skills)}\n"
            f"- Skills kept/quarantined: {len(receipt.skipped_skills)}\n"
            f"- MCP imported disabled: "
            f"{len(receipt.imported_mcp_servers)}\n"
            f"- MCP kept/incompatible: "
            f"{len(receipt.skipped_mcp_servers)}\n"
            f"- Memory scopes imported: "
            f"{len(receipt.imported_memory_projects)}\n"
            f"- Memory scopes already present/skipped: "
            f"{len(receipt.skipped_memory_projects)}\n"
            f"- Marketplace sources restored: "
            f"{len(receipt.restored_marketplaces)}\n"
            f"- Marketplace sources unavailable/already present: "
            f"{len(receipt.skipped_marketplaces)}\n"
            f"- Plugins installed through native flow: "
            f"{len(receipt.installed_plugins)}\n"
            f"- Plugins kept/incompatible: "
            f"{len(receipt.skipped_plugins)}\n"
            "- Imported sessions: visible historical archives; continuation "
            "is not guaranteed\n"
            f"- Runtime backend changed: no\n"
            f"- Migration receipt: `{receipt.migration_id}`"
            f"{warnings}"
        )


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
