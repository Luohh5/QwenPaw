# -*- coding: utf-8 -*-
"""Portable third-party harness acceptance using only the public SDK."""

import asyncio
import json

import pytest

SOURCE = """
import asyncio
from qwenpaw.plugins.harnesses import (
    HarnessApprovalSummary,
    HarnessCapabilities,
    HarnessEvent,
    HarnessEventKind,
    HarnessLogin,
    HarnessSpec,
    HarnessStatus,
)


class Backend:
    def __init__(self, context):
        self.context = context
        self.lock = asyncio.Lock()

    async def status(self, context):
        assert context.runtime_capabilities is None
        return HarnessStatus(installed=True, authenticated=True)

    async def start_login(self, context, *, device_code=False):
        return HarnessLogin({"device_code": device_code})

    async def logout(self, context):
        pass

    async def models(self, context):
        return ()

    async def discover_skills(self, context):
        return ()

    async def discover_mcp(self, context):
        return ()

    async def run_turn(self, context, request):
        assert context.runtime_capabilities is not None
        if request.prompt == "approval":
            decision = await context.request_approval(
                HarnessApprovalSummary(
                    "public-tool", "Allow test", "Test", {}, "medium"
                ),
                timeout=5,
            )
            assert decision == "approved"
        key = f"{context.session_id}-{context.user_id}-{context.channel}.json"
        async with self.lock:
            count = await self.context.storage.aread_json(key, 0) + 1
            await self.context.storage.awrite_json(key, count)
        yield HarnessEvent(
            HarnessEventKind.TEXT_DELTA, text=f"{context.user_id}:{count}"
        )
        yield HarnessEvent(HarnessEventKind.COMPLETED)

    async def run_command(self, context, command, arguments):
        return ()

    async def history(self, context):
        return ()

    async def reset_session(self, context):
        key = f"{context.session_id}-{context.user_id}-{context.channel}.json"
        await self.context.storage.awrite_json(key, 0)

    async def close(self):
        await self.context.storage.awrite_json("closed.json", True)


class Plugin:
    def register(self, plugin_context):
        async def create(context, settings):
            closed = await context.storage.aread_json("closed.json", False)
            await plugin_context.storage.awrite_json(
                "previous-close.json", closed
            )
            count = await plugin_context.storage.aread_json(
                "factories.json", 0
            )
            await plugin_context.storage.awrite_json(
                "factories.json", count + 1
            )
            return Backend(context)

        plugin_context.register_harness(
            HarnessSpec(
                "public-harness",
                "Public harness",
                create,
                HarnessCapabilities(),
            )
        )


plugin = Plugin()
"""


def package(tmp_path):
    root = tmp_path / "public-plugin"
    root.mkdir()
    (root / "plugin.json").write_text(
        json.dumps(
            {
                "id": "public-harness-owner",
                "version": "1.0.0",
                "entry": {"backend": "main.py"},
                "sdk": {
                    "version": "1.1.0",
                    "capabilities": ["harnesses", "storage", "resources"],
                },
                "meta": {
                    "harnesses": [
                        {"id": "public-harness", "label": "Public harness"},
                    ],
                },
            },
        ),
        encoding="utf-8",
    )
    (root / "main.py").write_text(SOURCE, encoding="utf-8")
    return root


async def run(host, prompt):
    from qwenpaw.plugins.events import ChatEvent
    from qwenpaw.plugins.harnesses import HarnessRunRequest

    events = [
        event
        async for event in host.run_harness(
            "public-harness",
            HarnessRunRequest(prompt),
            workspace_id="one",
            session_id="resume-session",
            user_id="alice",
            channel="console",
        )
    ]
    assert events and all(isinstance(event, ChatEvent) for event in events)
    final = events[-1]
    assert final.object == "response" and final.status == "completed"
    return final.output[-1].content[0].text


@pytest.mark.asyncio
async def test_author_harness_approvals_resume_after_enable(tmp_path):
    from qwenpaw.plugins import SDKError
    from qwenpaw.plugins.compliance import check_plugin
    from qwenpaw.plugins.harnesses import HarnessStatus
    from qwenpaw.plugins.testing import PluginTestHost

    source = package(tmp_path)
    assert check_plugin(source).ok
    async with PluginTestHost(tmp_path / "host") as host:
        installed = await host.install(source)
        assert installed.ok
        context = host.context("public-harness-owner")
        rows = host.list_harnesses()
        assert isinstance(rows, tuple)
        assert [
            (row["id"], row["plugin_id"], row["status"]) for row in rows
        ] == [("public-harness", "public-harness-owner", "available")]
        assert context.storage.read_json("factories.json", 0) == 0
        status = await host.harness_status(
            "public-harness",
            workspace_id="one",
        )
        assert isinstance(status, HarnessStatus) and status.installed
        assert await run(host, "hello") == "alice:1"
        approving = asyncio.create_task(run(host, "approval"))
        try:
            async with asyncio.timeout(5):
                while not (
                    pending := await host.approval_requests("resume-session")
                ):
                    await asyncio.sleep(0.005)
            assert len(pending) == 1 and pending[0].workspace_id == "one"
            assert await host.resolve_approval(
                pending[0].request_id,
                approved=True,
            )
            assert await approving == "alice:2"
        finally:
            if not approving.done():
                approving.cancel()
                await asyncio.gather(approving, return_exceptions=True)
        assert (await host.set_enabled("public-harness-owner", False)).ok
        assert host.list_harnesses()[0]["status"] == "disabled"
        with pytest.raises(SDKError, match="unavailable|active|disabled"):
            await run(host, "must refuse")
        assert (await host.set_enabled("public-harness-owner", True)).ok
        assert await run(host, "resumed") == "alice:3"
        resumed_context = host.context("public-harness-owner")
        assert resumed_context.storage.read_json("factories.json") == 2
        assert resumed_context.storage.read_json("previous-close.json") is True
