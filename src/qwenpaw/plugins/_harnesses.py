# -*- coding: utf-8 -*-
"""Harness declarations join the existing SDK ownership and teardown ledger."""

# Host adapters access private SDK ownership behind the public contract.
# pylint: disable=protected-access

from .errors import SDKAccessError, StaleContextError


def register(context, spec):
    context._guard()
    declaration = context._api.manifest.get("sdk") or {}
    if "harnesses" not in declaration.get("capabilities", []):
        raise SDKAccessError(
            "register_harness requires sdk.capabilities harnesses",
        )
    registry = context._api._registry.backend_registry
    owner = context.resources.owner
    handle = registry.register(owner, spec)

    def guard():
        context._guard()
        if not context._instance.activated:
            raise StaleContextError(
                f"Harness plugin {context.plugin_id!r} is not activated",
            )

    registry._bind_owner(owner, guard)
    context._instance.record_runtime(
        handle.description,
        handle.close,
        kind="harness",
    )
    return handle
