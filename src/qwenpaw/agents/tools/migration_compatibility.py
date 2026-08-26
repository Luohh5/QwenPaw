# -*- coding: utf-8 -*-
"""Private tools used by the two-stage migration compatibility workflow."""

from __future__ import annotations

import inspect
import json
from typing import Any

from ...runtime.tool_registry import tool_descriptor


def _context() -> Any:
    from ...portability.adaptation_loop import get_active_adaptation_context

    return get_active_adaptation_context()


async def _invoke(method: str, *args: Any, **kwargs: Any) -> str:
    try:
        value = getattr(_context(), method)(*args, **kwargs)
        if inspect.isawaitable(value):
            value = await value
    except Exception as exc:  # pylint: disable=broad-except
        value = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return json.dumps(value, ensure_ascii=False, default=str)


_COMMON = {
    "enabled_by_default": False,
    "tool_type": "internal",
    "default_policy": "allow",
    "display_to_user": False,
    "self_authorizing_request_opt_in": True,
}


@tool_descriptor(
    name="migration_compat_inspect",
    description="Inspect one staged asset and current QwenPaw capabilities.",
    **_COMMON,
)
async def migration_compat_inspect(asset_key: str) -> str:
    return await _invoke("inspect_asset", asset_key)


@tool_descriptor(
    name="migration_compat_read_file",
    description="Read one staged asset file; paginate until has_more=false.",
    **_COMMON,
)
async def migration_compat_read_file(
    asset_key: str,
    relative_path: str,
    start_line: int = 1,
    end_line: int = 240,
) -> str:
    return await _invoke(
        "read_file",
        asset_key,
        relative_path,
        start_line=start_line,
        end_line=end_line,
    )


@tool_descriptor(
    name="migration_compat_write_file",
    description="Create or overwrite one text file inside a repair asset.",
    **_COMMON,
)
async def migration_compat_write_file(
    asset_key: str,
    relative_path: str,
    content: str,
) -> str:
    return await _invoke("write_file", asset_key, relative_path, content)


@tool_descriptor(
    name="migration_compat_update",
    description="Update one allowlisted MCP or scheduled-task field.",
    **_COMMON,
)
async def migration_compat_update(
    asset_key: str,
    field: str,
    value_json: str,
) -> str:
    return await _invoke("update_asset", asset_key, field, value_json)


@tool_descriptor(
    name="migration_compat_test",
    description="Run QwenPaw's native compatibility test for one asset.",
    **_COMMON,
)
async def migration_compat_test(asset_key: str) -> str:
    return await _invoke("test_asset", asset_key)


@tool_descriptor(
    name="migration_compat_classify",
    description=(
        "Triage staging to repair/discard, or promote tested repair "
        "to migrate."
    ),
    **_COMMON,
)
async def migration_compat_classify(
    asset_key: str,
    zone: str,
    reason: str,
    plugin_disposition: str = "",
    component_assessments_json: str = "{}",
) -> str:
    return await _invoke(
        "classify_asset",
        asset_key,
        zone,
        reason,
        plugin_disposition,
        component_assessments_json,
    )


MIGRATION_COMPAT_TOOL_NAMES = (
    "migration_compat_inspect",
    "migration_compat_read_file",
    "migration_compat_write_file",
    "migration_compat_update",
    "migration_compat_test",
    "migration_compat_classify",
)

__all__ = ["MIGRATION_COMPAT_TOOL_NAMES", *MIGRATION_COMPAT_TOOL_NAMES]
