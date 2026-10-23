# -*- coding: utf-8 -*-
"""Fake Codex app-server and public SDK contexts for plugin tests."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from qwenpaw.plugins.harnesses import HarnessCallContext, HarnessContext
from qwenpaw.plugins.resources import ResourceOwner, ResourceScope
from qwenpaw.plugins.storage import PluginStorage


class FakeCodexClient:
    """Record vendor RPCs without starting a process or using an account."""

    installed = True

    def __init__(self):
        self.requests = []
        self.queue = None
        self.started = False
        self.stopped = False
        self.next_thread = 0
        self.binary_resolution = SimpleNamespace(
            path=Path("/fake/codex"),
            source="test",
        )

    def set_server_request_handler(self, handler):
        self.server_request_handler = handler

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True

    async def request(self, method, params):
        self.requests.append((method, params))
        if method == "thread/start":
            self.next_thread += 1
            return {"thread": {"id": f"new-thread-{self.next_thread}"}}
        if method == "thread/resume":
            return {"thread": {"id": params["threadId"]}}
        if method == "turn/start":
            thread_id = params["threadId"]
            await self.queue.put(
                {
                    "method": "item/agentMessage/delta",
                    "params": {
                        "threadId": thread_id,
                        "turnId": "turn-1",
                        "itemId": "message-1",
                        "delta": "done",
                    },
                },
            )
            await self.queue.put(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": thread_id,
                        "turn": {"id": "turn-1", "status": "completed"},
                    },
                },
            )
            return {"turn": {"id": "turn-1"}}
        if method == "thread/list":
            return {
                "data": [
                    {
                        "id": "native-thread",
                        "preview": "Old Codex task",
                        "cwd": "/external/project",
                    },
                ],
                "nextCursor": None,
            }
        if method == "thread/read":
            return {
                "thread": {
                    "turns": [
                        {
                            "items": [
                                {
                                    "id": "user-1",
                                    "type": "userMessage",
                                    "content": [
                                        {"type": "text", "text": "Old prompt"},
                                    ],
                                },
                                {
                                    "id": "answer-1",
                                    "type": "agentMessage",
                                    "text": "Old answer",
                                },
                            ],
                        },
                    ],
                },
            }
        return {}

    def subscribe(self):
        self.queue = asyncio.Queue()
        return self.queue

    def unsubscribe(self, queue):
        assert queue is self.queue
        self.queue = None


@pytest.fixture()
def codex_client():
    return FakeCodexClient()


@pytest.fixture(name="harness_context")
def make_harness_context(tmp_path):
    workspace = tmp_path / "workspace"
    state = workspace / "plugin-data" / "qwenpaw-harness-codex"
    state.mkdir(parents=True)
    owner = ResourceOwner("qwenpaw-harness-codex", 1, "workspace")
    return HarnessContext(
        plugin_id=owner.plugin_id,
        generation=owner.generation,
        workspace_id=owner.workspace_id,
        workspace_dir=workspace,
        state_dir=state,
        storage=PluginStorage(state, lambda: None),
        resources=ResourceScope(owner, lambda: None),
        logger=SimpleNamespace(),
    )


@pytest.fixture()
def call_context(harness_context):
    def create(user_id="alice", channel="console", session_id="chat-1"):
        return HarnessCallContext(
            request_id=f"{user_id}-{channel}-{session_id}",
            session_id=session_id,
            user_id=user_id,
            channel=channel,
            cwd=harness_context.workspace_dir,
            settings={},
            runtime_capabilities=None,
            resources=harness_context.resources.child(instance_id="call"),
        )

    return create
