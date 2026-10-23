# -*- coding: utf-8 -*-
"""Synthetic public harness exercised by the real loader and backend bridge."""

import asyncio
from types import SimpleNamespace

import pytest

from .test_sdk_entry_contract import write_plugin

SOURCE = """
import asyncio
from qwenpaw.plugins.harnesses import (
    HarnessApprovalSummary,
    HarnessCapabilities,
    HarnessCommand,
    HarnessEvent,
    HarnessEventKind as Kind,
    HarnessHistoryItem,
    HarnessHistoryKind,
    HarnessLogin,
    HarnessModel,
    HarnessSpec,
    HarnessStatus,
)


class Backend:
    def __init__(self, context, settings, plugin):
        asyncio.get_running_loop()  # Constructors belong to the event loop.
        self.context, self.settings, self.plugin = (
            context,
            settings,
            plugin,
        )
        self.calls = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.stream_closed = asyncio.Event()
        self.close_entered = asyncio.Event()
        self.close_release = asyncio.Event()
        self.close_release.set()
        self.closed = False
        self.resets = 0
        self.history_calls = 0

    async def status(self, context):
        self.calls.append(context)
        return HarnessStatus(available=True, installed=True)

    async def start_login(self, context, *, device_code=False):
        self.calls.append(context)
        return HarnessLogin({"device_code": device_code})

    async def logout(self, context):
        self.calls.append(context)

    async def discover_skills(self, context):
        self.calls.append(context)
        return ()

    async def discover_mcp(self, context):
        self.calls.append(context)
        return ()

    async def models(self, context):
        self.calls.append(context)
        return (HarnessModel("fake-model", "Fake model"),)

    async def run_turn(self, context, request):
        self.calls.append(context)
        self.last_request = request
        self.entered.set()
        try:
            if request.prompt == "approval":
                self.approval_decision = await context.request_approval(
                    HarnessApprovalSummary(
                        "shell",
                        "Confirm",
                        "Test",
                        {"user_id": "forged"},
                        "medium",
                    ),
                    timeout=5,
                )
            yield HarnessEvent(Kind.TEXT_DELTA, text="first")
            if request.prompt == "hold":
                await self.release.wait()
            if request.prompt == "error":
                yield HarnessEvent(Kind.ERROR, text="backend failed")
                return
            yield HarnessEvent(Kind.REASONING_DELTA, text="reason")
            yield HarnessEvent(
                Kind.TOOL_STARTED,
                item_id="tool1",
                tool_name="shell",
                data={"arguments": {"command": "echo test"}},
            )
            yield HarnessEvent(
                Kind.TOOL_PROGRESS, item_id="tool1", text="progress"
            )
            yield HarnessEvent(
                Kind.TOOL_COMPLETED,
                item_id="tool1",
                tool_name="shell",
                text="done",
            )
            yield HarnessEvent(Kind.COMPLETED)
        finally:
            self.stream_closed.set()

    async def run_command(self, context, command, arguments):
        self.calls.append(context)
        return (
            HarnessEvent(Kind.TEXT_DELTA, text=command + ":" + arguments),
            HarnessEvent(Kind.COMPLETED),
        )

    async def reset_session(self, context):
        self.calls.append(context)
        self.resets += 1

    async def history(self, context):
        self.calls.append(context)
        self.history_calls += 1
        return (
            HarnessHistoryItem(
                HarnessHistoryKind.USER, text="old question"
            ),
            HarnessHistoryItem(
                HarnessHistoryKind.MESSAGE, text="old answer"
            ),
        )

    async def list_external_threads(self, context, *, limit, archived):
        self.calls.append(context)
        return (
            {
                "id": "native-thread",
                "archived": archived,
                "vendor_metadata": {"kept": True},
            },
        )

    async def read_external_thread(self, context, thread_id):
        return await self.history(context)

    async def external_skill_records(self, context):
        return ()

    async def external_mcp_records(self, context):
        return ()

    async def close(self):
        self.close_entered.set()
        await self.close_release.wait()
        if self.plugin.close_fail:
            raise RuntimeError("intentional backend close failure")
        self.closed = True


class Plugin:
    def register(self, context):
        self.created = []
        self.close_fail = False

        def create(factory_context, settings):
            backend = Backend(factory_context, settings, self)
            self.created.append(backend)
            return backend

        self.handle = context.register_harness(
            HarnessSpec(
                "fake-backend",
                "Fake backend",
                create,
                HarnessCapabilities(
                    external_history=True,
                    commands=(HarnessCommand("compact"),),
                ),
            )
        )


plugin = Plugin()
"""


async def load_backend(
    tmp_path,
    loader,
    *,
    owner="bridge-owner",
    backend="fake-backend",
    source=SOURCE,
):
    root = tmp_path / "plugins" / owner
    manifest = write_plugin(
        root,
        source.replace("fake-backend", backend),
        sdk={
            "version": "1.1.0",
            "capabilities": ["harnesses", "storage", "resources"],
        },
        meta={"harnesses": [{"id": backend, "label": backend}]},
    )
    record = await loader.load_plugin(manifest, root, {})
    assert record.status == "active", record.diagnostics
    return record.instance


def request(
    prompt="hello",
    *,
    user="user",
    channel="console",
    session="same-session",
):
    from qwenpaw.schemas import AgentRequest

    return AgentRequest(
        session_id=session,
        user_id=user,
        channel=channel,
        request_id="trusted-request",
        input=[
            {"role": "user", "content": [{"type": "text", "text": prompt}]},
        ],
    )


async def collect(runtime, prompt="hello", *, settings=None, **identity):
    return [
        item
        async for item in runtime.stream(
            "fake-backend",
            request(prompt, **identity),
            settings,
        )
    ]


async def until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.005)


@pytest.fixture(name="harness")
async def harness_fixture(tmp_path, loader):
    from qwenpaw.runtime.backends.execution import BackendExecution

    plugin = await load_backend(tmp_path, loader)
    runtimes = []

    def runtime(workspace="one", session=None):
        path = tmp_path / workspace
        path.mkdir(exist_ok=True)
        instance = BackendExecution(
            path,
            session=session,
            agent_id=workspace,
            registry=loader.registry.backend_registry,
        )
        runtimes.append(instance)
        return instance

    yield SimpleNamespace(plugin=plugin, runtime=runtime, loader=loader)
    plugin.close_fail = False
    for backend in plugin.created:
        backend.release.set()
        backend.close_release.set()
    for instance in runtimes:
        await asyncio.wait_for(instance.close(), 5)
    if loader.get_loaded_plugin("bridge-owner") is not None:
        await loader.unload_plugin("bridge-owner")
