# -*- coding: utf-8 -*-
"""Editable channel metadata, independent of executable registrations."""

from __future__ import annotations

OFFICIAL_CHANNEL_OWNERS = {"feishu": "feishu", "dingtalk": "dingtalk"}
RESPONSE_FIELDS = frozenset(
    {
        "isBuiltin",
        "owner",
        "form_kind",
        "process_mode",
        "availability",
        "global_enabled",
        "runtime_state",
        "repair",
        "diagnostic",
    },
)


def config_fields(schema, names=None):
    """Adapt scalar schema fields to the Console's existing widgets."""
    properties = schema.get("properties", {})
    result = []
    for name in names if names is not None else properties:
        field = dict(properties[name])
        alternatives = field.get("anyOf", [])
        value = next(
            (item for item in alternatives if item.get("type") != "null"),
            field,
        )
        kind = value.get("type")
        if kind not in {
            "boolean",
            "integer",
            "number",
            "string",
            "object",
            "array",
        }:
            continue
        widget = {
            "boolean": "switch",
            "integer": "number",
            "number": "number",
            "object": "json",
            "array": "json",
        }.get(kind, "text")
        row = {"name": name, "label": field.get("title", name), "type": widget}
        if kind in {"object", "array"}:
            row["type"] = "json"
            row["json_type"] = kind
        if kind not in {"object", "array"} and "enum" in value:
            row.update(type="select", options=value["enum"])
        elif kind == "string" and any(
            part in name for part in ("secret", "token", "password")
        ):
            row["type"] = "password"
        if name in schema.get("required", ()):
            row["required"] = True
        if "default" in field:
            row["default"] = field["default"]
        result.append(row)
    return result


def clean_channel_config(value):
    """Response metadata never becomes credentials or plugin settings."""
    return {
        key: item for key, item in value.items() if key not in RESPONSE_FIELDS
    }


def get_configurable_channel_catalog(saved_keys=()):
    """Keep missing and disabled channel settings editable without imports."""
    from ...config.config import ChannelConfig
    from ...config.utils import get_plugins_dir, load_config
    from ...plugins.registry import PluginRegistry
    from ...plugins.settings import is_plugin_enabled

    registry = PluginRegistry()
    settings = load_config().plugins
    result = {}
    for key, field in ChannelConfig.model_fields.items():
        model = field.annotation
        official = key in OFFICIAL_CHANNEL_OWNERS
        result[key] = {
            "label": key,
            "description": "",
            "plugin_id": key if official else "",
            "owner": OFFICIAL_CHANNEL_OWNERS.get(key, "builtin"),
            "form_kind": key,
            "process_mode": "runner_process" if official else "in_process",
            "config_fields": config_fields(model.model_json_schema()),
            "defaults": model().model_dump(mode="json"),
        }
        if official:
            installed = (get_plugins_dir() / key / "plugin.json").is_file()
            enabled = is_plugin_enabled(settings.get(key))
            state = (
                "missing_code"
                if not installed
                else ("disabled" if not enabled else "validation_pending")
            )
            if installed and enabled and registry.channel_runtime is None:
                state = "missing_runtime"
            result[key].update(availability=state, global_enabled=enabled)
    for key, row in registry.get_channel_catalog().items():
        base = result.setdefault(
            key,
            {"owner": row.plugin_id, "form_kind": "schema"},
        )
        base.update(
            label=row.label,
            description=row.description,
            plugin_id=row.plugin_id,
            config_fields=row.config_fields,
            icon=row.icon,
            doc_url=row.doc_url,
        )
        if key in OFFICIAL_CHANNEL_OWNERS:
            base.update(global_enabled=row.global_enabled)
            base["availability"] = (
                "disabled"
                if not row.global_enabled
                else (
                    "missing_runtime"
                    if row.runtime_settings is None
                    else "validation_pending"
                )
            )
    for key in saved_keys:
        result.setdefault(
            key,
            {
                "label": key,
                "description": "",
                "plugin_id": key,
                "owner": key,
                "form_kind": "key_value",
                "config_fields": [],
            },
        )
    for key in OFFICIAL_CHANNEL_OWNERS:
        row = result[key]
        action = (
            "install"
            if row["availability"] == "missing_code"
            else ("enable" if row["availability"] == "disabled" else "repair")
        )
        row["repair"] = {
            "action": action,
            "plugin_id": key,
            "source": "bundled:" + key,
        }
    return result
