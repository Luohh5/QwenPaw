# -*- coding: utf-8 -*-
# pylint: disable=undefined-all-variable
"""QwenPaw plugin system with import-safe execution primitives."""

from importlib import import_module

_EXPORTS = {
    "PluginLoader": "loader",
    "PluginRegistry": "registry",
    "PluginApi": "api",
    "get_tool_config": "api",
    "PluginManifest": "architecture",
    "PluginRecord": "architecture",
    "PluginLifecycle": "lifecycle",
    "ConfigUpdateReport": "lifecycle",
    "ReloadReport": "lifecycle",
    "UnloadMode": "lifecycle",
}
__all__ = list(_EXPORTS)


def __getattr__(name):
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f"{__name__}.{module}"), name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(__all__))
