# -*- coding: utf-8 -*-
"""Contract tests for the Console channel."""

from __future__ import annotations

import json

import pytest

from qwenpaw.app.channels.renderer import ChannelDisplayConfig
from qwenpaw.schemas import Event, Message, RunStatus, TextContent

from tests.contract.channels import ChannelContractTest


async def reply(_request):
    yield Event(
        object="message",
        status=RunStatus.Completed,
        type="message.completed",
        id="console-reply",
        created_at=1,
        message=Message(role="assistant", content=[TextContent(text="hello")]),
    )


class TestConsoleChannelContract(ChannelContractTest):
    """Ensure ConsoleChannel satisfies the common channel contract."""

    @pytest.fixture(autouse=True)
    def isolated_media(self, tmp_path):
        self.media = tmp_path / "media"

    def create_instance(self):
        """Provide a ConsoleChannel instance for contract testing."""
        from qwenpaw.app.channels.console.channel import ConsoleChannel

        return ConsoleChannel(
            process=reply,
            enabled=True,
            bot_prefix="[TEST] ",
            media_dir=str(self.media),
            display_config=ChannelDisplayConfig(
                show_tool_calls=False,
                show_tool_results=False,
            ),
        )

    async def test_start_send_and_stop(self, instance, capsys):
        await instance.start()
        assert (await instance.health_check())["status"] == "healthy"
        await instance.send("alice", "hello")
        assert "[TEST] hello" in capsys.readouterr().out
        await instance.stop()
        await instance.stop()

    async def test_disabled_channel_does_not_send(self, instance, capsys):
        instance.enabled = False
        await instance.start()
        await instance.send("alice", "hidden")
        await instance.stop()
        assert capsys.readouterr().out == ""
        assert (await instance.health_check())["status"] == "disabled"

    async def test_content_parts_are_output(self, instance, capsys):
        await instance.send_content_parts(
            "alice",
            [TextContent(text="first"), TextContent(text="second")],
        )
        output = capsys.readouterr().out
        assert "first" in output and "second" in output
        assert output.index("first") < output.index("second")

    async def test_inbound_message_streams_final_reply(self, instance):
        events = [
            json.loads(frame.removeprefix("data: "))
            async for frame in instance.stream_one(
                {
                    "sender_id": "alice",
                    "content_parts": [TextContent(text="question")],
                    "meta": {},
                },
            )
        ]
        completed = next(e for e in events if e.get("id") == "console-reply")
        assert completed["status"] == "completed"
        assert completed["message"]["content"][0]["text"] == "hello"
