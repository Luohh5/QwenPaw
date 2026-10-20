# -*- coding: utf-8 -*-
# pylint: disable=protected-access
"""Private SDK-to-Driver bridge; registration never constructs a transport."""

import asyncio
import hashlib
import json
import re
from contextlib import asynccontextmanager, contextmanager
from dataclasses import asdict
from uuid import uuid4

from .drivers import (
    DriverCallResult,
    DriverCapabilitySpec,
    DriverContext,
    DriverCredential,
    DriverProtocolSpec,
    DriverRequestContext,
    DriverSource,
    _freeze,
    _thaw,
)
from .errors import SDKAccessError, SDKError, StaleContextError
from .resources import Registration
from ..drivers.handler import DriverHandler
from ..drivers.capabilities import (
    CapabilityExposure,
    DriverCapability,
    DriverInvocationResult,
    format_capability_id,
    parse_capability_id,
    mcp_tool_is_enabled,
    mcp_tool_whitelist,
)
from ..drivers.contracts import PolicyTarget, validate_card


def _source(card):
    return DriverSource(
        card.name,
        card.protocol,
        card.endpoint,
        card.config,
        card.enabled,
    )


def _revision(card):
    payload = json.dumps(asdict(card), sort_keys=True, default=str)
    return int.from_bytes(hashlib.sha256(payload.encode()).digest()[:8], "big")


def _workspace_guard(registry, workspace):
    manager = registry.get_workspace_manager()
    live = getattr(manager, "agents", getattr(manager, "workspaces", {}))
    if live.get(workspace.agent_id) is not workspace:
        raise StaleContextError("Driver workspace instance has been replaced")


def _instance_id(workspace):
    if not hasattr(workspace, "_sdk_driver_instance_id"):
        workspace._sdk_driver_instance_id = uuid4().hex
    return workspace._sdk_driver_instance_id


@contextmanager
def _cleanup_admission(plugin):
    """Admit only this owned cleanup while the lifecycle gate is closed."""
    activity = plugin._instance.activity
    task = asyncio.current_task()
    retained = task in activity.cleanup_tasks
    activity.cleanup_tasks.add(task)
    try:
        with plugin._admission(cleanup=True):
            yield
    finally:
        if not retained:
            activity.cleanup_tasks.discard(task)


async def _environment(plugin, check):
    from ..envs import load_envs
    from ..utils.io_utils import run_sync_io

    with plugin._admission():
        await check()
        environment = await run_sync_io(load_envs)
        await check()
        return _freeze(environment)


class CredentialAccess:
    def __init__(self, card, store, check, guard, plugin):
        self._card, self._store, self._check = card, store, check
        self._guard, self._plugin = guard, plugin

    @property
    def aliases(self):
        self._guard()
        return tuple(self._card.credentials)

    async def _ref(self, alias):
        await self._check()
        if type(alias) is not str or alias not in self._card.credentials:
            raise SDKAccessError(
                "Credential alias is not declared by this source",
            )
        return self._card.credentials[alias].ref

    async def resolve(self, alias):
        self._guard()
        with self._plugin._admission():
            return await self._resolve(alias)

    async def _resolve(self, alias):
        ref = await self._ref(alias)
        try:
            record, revision = await self._store.get_versioned(ref)
        except Exception as exc:
            raise SDKError(str(exc)) from exc
        await self._check()
        return DriverCredential(
            record.kind,
            revision,
            record.public,
            record.secrets,
        )

    async def update(self, alias, *, expected_revision, public, secrets):
        self._guard()
        with self._plugin._admission():
            await self._update(
                alias,
                expected_revision=expected_revision,
                public=public,
                secrets=secrets,
            )

    async def _update(self, alias, *, expected_revision, public, secrets):
        ref = await self._ref(alias)
        if type(expected_revision) is not int:
            raise SDKError("Credential revision must be an integer")
        try:
            await self._store.compare_and_put(
                ref,
                expected_revision=expected_revision,
                public=_thaw(_freeze(public)),
                secrets=_thaw(_freeze(secrets)),
            )
        except Exception as exc:
            raise SDKError(str(exc)) from exc
        await self._check()


class ProtocolRegistration:
    """One generation owns all pending and published source resource scopes."""

    def __init__(self, plugin, spec):
        self.plugin, self.spec = plugin, spec
        self.owner = plugin.resources.owner
        self.handlers = set()
        self.bindings = set()
        self.revoked = False

    def guard(self):
        self.plugin._guard()
        if self.revoked:
            raise StaleContextError("Driver protocol has been released")

    def validate(self, card):
        self.guard()
        with self.plugin._admission():
            self.spec.validate(_source(card))

    async def close(self):
        self.revoked = True
        errors = []
        for handler in tuple(self.handlers):
            try:
                await handler.manager._close_owned_handlers((handler,))
            except Exception as exc:
                errors.append(str(exc))
        if errors:
            raise SDKError("Driver cleanup failed: " + "; ".join(errors))
        for manager, factory in self.bindings:
            if manager._handler_types.get(self.spec.protocol) is factory:
                manager._handler_types.pop(self.spec.protocol, None)
                manager._endpoint_validators.pop(self.spec.protocol, None)
        self.plugin._api._registry.unregister_driver_protocol(
            self.spec.protocol,
            expected_owner=self.owner,
        )


def register(context, spec):
    context._guard()
    if not isinstance(spec, DriverProtocolSpec):
        raise TypeError("Expected a public DriverProtocolSpec")
    descriptor = ProtocolRegistration(context, spec)
    registry = context._api._registry
    registry.register_driver_protocol(descriptor, spec)
    handle = Registration(
        descriptor.close,
        owner=descriptor.owner,
        description=f"driver:{spec.protocol}",
        _bounded=False,
    )
    try:
        context._instance.record_runtime(
            handle.description,
            handle.close,
            kind="custody",
        )
    except BaseException:
        registry.unregister_driver_protocol(
            spec.protocol,
            expected_owner=descriptor.owner,
        )
        raise
    return handle


def attach_driver_protocols(manager, registry, workspace):
    """Attach current descriptors to a real manager for this workspace object.

    The lazy runtime calls this before building cards and after protocol
    registration changes. The registry's workspace manager must expose the
    live object in ``agents`` or ``workspaces``. This opens no connections.
    """
    for descriptor in registry._driver_protocols.values():
        if descriptor.revoked:
            continue
        if any(bound is manager for bound, _ in descriptor.bindings):
            continue

        def factory(
            card,
            provider,
            providers,
            *,
            approval_gate=None,
            descriptor=descriptor,
        ):
            return SDKDriverHandler(
                card,
                provider,
                providers,
                approval_gate=approval_gate,
                descriptor=descriptor,
                workspace=workspace,
                registry=registry,
            )

        manager.register_handler_type(
            descriptor.spec.protocol,
            factory,
            descriptor.validate,
        )
        descriptor.bindings.add((manager, factory))


class SDKDriverHandler(DriverHandler):
    def __init__(self, *args, descriptor, workspace, registry, **kwargs):
        super().__init__(*args, **kwargs)
        self.descriptor, self.workspace, self.registry = (
            descriptor,
            workspace,
            registry,
        )
        self.manager = None
        self.scope = None
        self.connection = None
        self.capabilities = None
        self.tasks = set()
        self.stopping = False
        self.closed = False
        self.metadata_revision = 0

    def bind_source(self, manager, revision, session_scope):
        self.manager, self.revision, self.session_scope = (
            manager,
            revision,
            session_scope,
        )
        self.descriptor.handlers.add(self)

    def guard(self):
        self.descriptor.guard()
        _workspace_guard(self.registry, self.workspace)
        if self.stopping or not self.manager.source_is_current(
            self.name,
            self.revision,
        ):
            raise StaleContextError(
                "Driver source revision is no longer active",
            )

    async def check(self):
        self.guard()
        if not self.session_scope:
            latest = await self.manager.card_store.load(
                self.name,
                protocol=self.card.protocol,
            )
            self.guard()
            if latest != self.card:
                raise StaleContextError("Driver source configuration changed")

    @contextmanager
    def admission(self):
        self.guard()
        task = asyncio.current_task()
        with self.descriptor.plugin._admission():
            nested = task in self.tasks
            self.tasks.add(task)
            try:
                yield
            finally:
                if not nested:
                    self.tasks.discard(task)

    def sync_runtime_metadata(self, card):
        self.metadata_revision += 1
        self.capabilities = None
        super().sync_runtime_metadata(card)

    def set_policy(self, policy):
        self.metadata_revision += 1
        super().set_policy(policy)

    def invalidate(self):
        self.guard()
        self.capabilities = None

    async def _setup(self):
        plugin = self.descriptor.plugin
        with self.admission():
            self.scope = plugin.resources.child(
                workspace_id=self.workspace.agent_id,
                instance_id=(
                    f"{_instance_id(self.workspace)}:{self.name}:"
                    f"{self.revision}"
                ),
            )
            self.scope._guard = self.guard
            context = DriverContext(
                plugin.plugin_id,
                plugin.generation,
                self.workspace.agent_id,
                _instance_id(self.workspace),
                self.name,
                self.revision,
                _source(self.card),
                self.scope,
                plugin.logger,
                CredentialAccess(
                    self.card,
                    self.manager.credential_store,
                    self.check,
                    self.guard,
                    plugin,
                ),
                self.session_scope,
                self.invalidate,
                _subprocess_environment=lambda: _environment(
                    plugin,
                    self.check,
                ),
            )
            self.connection = self.descriptor.spec.create(
                context,
                context.source,
            )
            # Install the close owner before open can suspend or fail.
            self.scope._defer(self._close_connection, "driver-connection")
            await self.connection.open()
            await self.check()

    async def _close_connection(self):
        with _cleanup_admission(self.descriptor.plugin):
            await self.connection.close()

    async def _teardown(self):
        if self.closed:
            return
        self.stopping = True
        pending = self.tasks - {asyncio.current_task()}
        for task in pending:
            task.cancel()
        # Init failure cleanup already runs from its owning task. The manager
        # waits for admitted callbacks before disposing their resources.
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if self.scope is not None:
            await self.scope.close()
        self.closed = True
        self.descriptor.handlers.discard(self)

    def request(self, values):
        values = values or {}
        task = asyncio.current_task()
        return DriverRequestContext(
            self.workspace.agent_id,
            _instance_id(self.workspace),
            str(values.get("session_id") or ""),
            str(values.get("user_id") or ""),
            str(values.get("channel") or ""),
            _is_cancelled=lambda: self.stopping or bool(task.cancelling()),
        )

    async def list_capabilities(self, request_context=None):
        with self.admission():
            await self.check()
            revision = self.metadata_revision
            if self.capabilities is None:
                specs = await self.connection.discover(
                    self.request(request_context),
                )
                await self.check()
                if revision != self.metadata_revision:
                    raise StaleContextError(
                        "Driver metadata changed during discovery",
                    )
                if any(
                    not isinstance(spec, DriverCapabilitySpec)
                    for spec in specs
                ):
                    raise SDKError(
                        "Driver discovery must return "
                        "DriverCapabilitySpec values",
                    )
                if len({spec.name for spec in specs}) != len(specs):
                    raise SDKError("Driver capability names must be unique")
                self.capabilities = tuple(specs)
            whitelist = mcp_tool_whitelist(self.card.config.get("tools"))
            display = str(self.card.config.get("display_name") or "")
            namespace = re.sub(r"[^A-Za-z0-9_-]+", "_", display.strip())
            if not namespace.strip("_"):
                namespace = (
                    re.sub(r"[^A-Za-z0-9_-]+", "_", self.name.strip())
                    or "tool"
                )
            if not namespace[0].isalpha():
                namespace = "tool_" + namespace
            result = []
            for spec in self.capabilities:
                name = spec.name
                tool = (
                    name
                    if re.fullmatch(r"[A-Za-z0-9_-]+", name)
                    else (
                        re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_")
                        or "tool"
                    )
                )
                result.append(
                    DriverCapability(
                        capability_id=format_capability_id(
                            self.card.protocol,
                            self.name,
                            "tool",
                            "invoke",
                            name,
                        ),
                        driver_name=self.name,
                        protocol=self.card.protocol,
                        kind="tool",
                        action="invoke",
                        name=name,
                        description=spec.description,
                        input_schema=_thaw(spec.input_schema),
                        output_schema=_thaw(spec.output_schema),
                        exposure=CapabilityExposure(
                            True,
                            spec.tool_name or f"{namespace}__{tool}",
                            namespace,
                        ),
                        metadata={
                            **_thaw(spec.metadata),
                            "driver_key": self.name,
                            "display_name": display or self.name,
                        },
                        enabled=spec.enabled
                        and mcp_tool_is_enabled(whitelist, name),
                    ),
                )
            return result

    async def invoke_capability(self, invocation):
        try:
            protocol, source, kind, action, name = parse_capability_id(
                invocation.capability_id,
            )
            if (protocol, source, kind, action) != (
                self.card.protocol,
                self.name,
                "tool",
                "invoke",
            ):
                raise SDKError("Unsupported Driver capability")
            with self.admission():
                await self.check()
                if not self.card.enabled or not mcp_tool_is_enabled(
                    mcp_tool_whitelist(self.card.config.get("tools")),
                    name,
                ):
                    raise SDKError("Driver tool is disabled")
                revision = self.metadata_revision
                values = invocation.request_context
                subjects = []
                if values.get("subject"):
                    subjects.append(values["subject"])
                for key, prefix in (
                    ("user_id", "user"),
                    ("session_id", "session"),
                    ("channel", "channel"),
                ):
                    value = values.get(key)
                    if value:
                        subjects.append(
                            (
                                value
                                if value.startswith(prefix + ":")
                                else f"{prefix}:{value}"
                            ),
                        )
                subjects = subjects or ["user:unknown"]
                await self._authorize_invocation(
                    subjects[0],
                    request_context=values,
                    subjects=subjects,
                    target=PolicyTarget(kind="tool", name=name),
                    extras={
                        "tool_name": name,
                        "arguments": invocation.payload,
                    },
                )
                await self.check()
                capabilities = await self.list_capabilities(values)
                capability = next(
                    (item for item in capabilities if item.name == name),
                    None,
                )
                if capability is None or not capability.enabled:
                    raise SDKError("Driver capability is missing or disabled")
                from jsonschema.validators import validator_for

                validator_for(capability.input_schema)(
                    capability.input_schema,
                ).validate(invocation.payload)
                await self.check()
                if revision != self.metadata_revision:
                    raise StaleContextError(
                        "Driver policy changed during authorization",
                    )
                result = await self.connection.invoke(
                    name,
                    _freeze(invocation.payload),
                    self.request(values),
                )
                await self.check()
                if revision != self.metadata_revision:
                    raise StaleContextError(
                        "Driver metadata changed during invocation",
                    )
                if not isinstance(result, DriverCallResult):
                    raise SDKError(
                        "Driver invoke must return DriverCallResult",
                    )
                return DriverInvocationResult(ok=True, value=result)
        except Exception as exc:
            return DriverInvocationResult(
                ok=False,
                error_type="driver_error",
                message=str(exc),
            )


@asynccontextmanager
async def management_source(services, protocol, source_id):
    """Authorize persistent management without creating runtime services."""
    from ..drivers.storage import AsyncDriverCardStore
    from ..drivers.credentials.store import AsyncCredentialStore

    services.guard()
    plugin = services.plugin
    registry = plugin._api._registry
    descriptor = registry._driver_protocols.get(protocol)
    if descriptor is None or descriptor.plugin is not plugin:
        raise SDKAccessError("Driver protocol belongs to another plugin")
    descriptor.guard()
    workspace = await services.workspace()
    services.guard()
    store = AsyncDriverCardStore(workspace.workspace_dir / "drivers")
    try:
        card = await store.load(source_id, protocol=protocol)
        validate_card(card)
    except Exception as exc:
        raise SDKAccessError(
            "Persistent Driver source is unavailable",
        ) from exc
    if card.protocol != protocol:
        raise SDKAccessError("Driver source belongs to another protocol")
    live = True

    def guard():
        services.guard()
        descriptor.guard()
        _workspace_guard(registry, workspace)
        if not live:
            raise StaleContextError("Driver management scope has ended")

    async def check():
        guard()
        descriptor.guard()
        _workspace_guard(registry, workspace)
        if not live:
            raise StaleContextError("Driver management scope has ended")
        current = await store.load(source_id, protocol=protocol)
        services.guard()
        if not live or current != card:
            raise StaleContextError("Driver management source changed")
        descriptor.guard()
        _workspace_guard(registry, workspace)

    await check()
    with plugin._admission():
        scope = plugin.resources.child(
            workspace_id=workspace.agent_id,
            instance_id=uuid4().hex,
        )
        scope._guard = guard
        context = DriverContext(
            plugin.plugin_id,
            plugin.generation,
            workspace.agent_id,
            _instance_id(workspace),
            source_id,
            _revision(card),
            _source(card),
            scope,
            plugin.logger,
            CredentialAccess(
                card,
                AsyncCredentialStore(
                    workspace.workspace_dir / "credentials.yaml",
                ),
                check,
                guard,
                plugin,
            ),
            _subprocess_environment=lambda: _environment(plugin, check),
        )
        try:
            yield context
        finally:
            live = False
            await scope.close()
