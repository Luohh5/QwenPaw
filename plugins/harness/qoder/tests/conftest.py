# -*- coding: utf-8 -*-
"""Public SDK contexts and fake Qoder client, without vendor processes."""

# Pytest fixtures are injected by their declared names.
# pylint: disable=redefined-outer-name

from pathlib import Path
from types import SimpleNamespace

import pytest

from qoder_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    TextBlock,
)
from qwenpaw.plugins.harnesses import HarnessCallContext, HarnessContext
from qwenpaw.plugins.resources import ResourceOwner, ResourceScope
from qwenpaw.plugins.storage import PluginStorage


class FakeQoderClient:
    """A bounded protocol fake recording one SDK session."""

    def __init__(self, options):
        self.options = options
        self.connected = self.disconnected = self.interrupted = False
        self.prompts = []
        self.messages = [
            StreamEvent(
                uuid="stream-1",
                session_id="native-qoder-session",
                event={
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "Done"},
                },
            ),
            AssistantMessage(
                content=[TextBlock(text="Done")],
                model="qoder-test",
            ),
            ResultMessage(
                subtype="success",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="native-qoder-session",
            ),
        ]
        self.server_info = {"skills": []}

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.disconnected = True

    async def query(self, prompt, session_id="default"):
        if not isinstance(prompt, str):
            prompt = [message async for message in prompt]
        self.prompts.append((prompt, session_id))

    async def receive_response(self):
        for message in self.messages:
            yield message

    async def interrupt(self):
        self.interrupted = True

    async def get_available_models(self):
        return [
            {
                "value": "model-without-default",
                "displayName": "Qoder Test",
                "isEnabled": True,
                "thinking_config": {
                    "enabled": {
                        "efforts": {
                            "low": {"is_default": False},
                            "high": {"is_default": True},
                        },
                    },
                },
            },
        ]

    async def get_server_info(self):
        return self.server_info


@pytest.fixture()
def client_factory():
    created = []

    def make(options):
        client = FakeQoderClient(options)
        if hasattr(make, "server_info"):
            client.server_info = make.server_info
        created.append(client)
        return client

    make.created = created
    return make


@pytest.fixture()
def harness_context(tmp_path):
    workspace = tmp_path / "workspace"
    state = workspace / "plugin-data" / "qwenpaw-harness-qoder"
    state.mkdir(parents=True)
    owner = ResourceOwner("qwenpaw-harness-qoder", 1, "workspace")
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
    def make(user_id="alice", channel="console", session_id="chat-1"):
        return HarnessCallContext(
            request_id=f"{user_id}-{channel}-{session_id}",
            session_id=session_id,
            user_id=user_id,
            channel=channel,
            cwd=Path(harness_context.workspace_dir),
            settings={},
            runtime_capabilities=None,
            resources=harness_context.resources.child(instance_id="call"),
        )

    return make
