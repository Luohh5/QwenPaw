# -*- coding: utf-8 -*-
# pylint: disable=protected-access
"""Owned protocol registration; source connections are admitted separately."""

from .drivers import DriverProtocolSpec
from .resources import Registration


def register(context, spec):
    context._guard()
    if not isinstance(spec, DriverProtocolSpec):
        raise TypeError("Expected a public DriverProtocolSpec")
    registry = context._api._registry
    owner = context.resources.owner
    registry.register_driver_protocol(owner, spec)

    def release():
        registry.unregister_driver_protocol(
            spec.protocol,
            expected_owner=owner,
        )

    handle = Registration(
        release,
        owner=owner,
        description=f"driver:{spec.protocol}",
    )
    try:
        context._instance.record_runtime(handle.description, handle.close)
    except BaseException:
        release()
        raise
    return handle
