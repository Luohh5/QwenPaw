# -*- coding: utf-8 -*-
# pylint: disable=protected-access
"""DingTalk entry must remain usable without platform dependencies."""

import asyncio
import socket
import subprocess
import sys
import threading

import pytest
from registration_suite import RecordingApi, unused_process
from qwenpaw.app.channels.base import BaseChannel
from qwenpaw.app.channels.renderer import ChannelDisplayConfig
from qwenpaw.config.config import DingTalkConfig


def channel_class(load_plugin, namespace="plugin_dingtalk"):
    api = RecordingApi()
    load_plugin(namespace, channel="dingtalk").plugin.register(api)
    assert len(api.channels) == 1
    return api.channels[0]


def test_registration_and_construction_without_sdk_or_work(
    load_plugin,
    monkeypatch,
    tmp_path,
):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Registration must not start external work")

    for target, attr in [
        (socket.socket, "connect"),
        (threading.Thread, "start"),
        (subprocess.Popen, "__init__"),
        (asyncio.BaseEventLoop, "create_task"),
    ]:
        monkeypatch.setattr(target, attr, forbidden)
    cls, fields = channel_class(load_plugin)
    assert issubclass(cls, BaseChannel)
    assert cls.channel == "dingtalk"
    instance = cls.from_config(
        unused_process,
        DingTalkConfig(enabled=True),
        workspace_dir=tmp_path,
    )
    assert instance.enabled is True
    assert not any(
        name.startswith(("dingtalk_stream", "alibabacloud_", "Tea."))
        for name in sys.modules
    )
    fields = {field["name"]: field for field in fields}
    assert fields["client_secret"]["type"] == "password"
    assert fields["client_id"]["required"] is True
    assert not (tmp_path / "media").exists()


def test_probe_uses_its_own_relative_channel(load_plugin):
    live, _ = channel_class(load_plugin)
    probe, _ = channel_class(load_plugin, "plugin_dingtalk__probe")
    assert live is not probe
    assert live.__module__ == "plugin_dingtalk.channel"
    assert probe.__module__ == "plugin_dingtalk__probe.channel"


@pytest.mark.parametrize(
    "message_type,streaming",
    [("markdown", False), ("card", True)],
)
def test_config_preserves_workspace_routing_and_host_arguments(
    load_plugin,
    tmp_path,
    message_type,
    streaming,
):
    config = DingTalkConfig(
        enabled=True,
        client_id="robot-one",
        client_secret="secret-one",
        bot_prefix="[bot]",
        message_type=message_type,
        cron_message_type="text",
        card_template_id="template-one",
        card_template_key="answer",
        robot_code="robot-two",
        endpoint="https://gateway.test/",
        streaming_enabled=True,
        share_session_in_group=True,
        card_auto_layout=True,
        at_sender_on_reply=True,
        dm_policy="allowlist",
        group_policy="allowlist",
        allow_from=["staff-one"],
        require_mention=True,
        access_control_dm=True,
        access_control_group=True,
        deny_message="denied",
        show_tool_calls=False,
    )
    original = config.model_dump()
    callback = object()
    display = ChannelDisplayConfig(show_thinking=False)
    cls, _ = channel_class(load_plugin)
    channel = cls.from_config(
        unused_process,
        config,
        callback,
        display,
        False,
        tmp_path,
    )
    assert channel.client_id == "robot-one"
    assert channel.client_secret == "secret-one"
    assert channel.robot_code == "robot-two"
    assert channel.endpoint == "https://gateway.test"
    assert channel.message_type == message_type
    assert channel.cron_message_type == "text"
    assert channel.streaming_enabled is streaming
    assert channel.card_template_id == "template-one"
    assert channel.card_template_key == "answer"
    assert (
        channel.card_auto_layout
        and channel.at_sender_on_reply
        and channel.share_session_in_group
    )
    assert channel.dm_policy == channel.group_policy == "allowlist"
    assert channel.allow_from == {"staff-one"}
    assert (
        channel.require_mention
        and channel.access_control_dm
        and channel.access_control_group
    )
    assert channel._process is unused_process
    assert channel._on_reply_sent is callback
    assert channel._display_config is display
    assert channel._no_text_debounce is False
    assert channel._workspace_dir == tmp_path
    assert channel._media_dir == tmp_path / "media"
    assert config.model_dump() == original


@pytest.mark.asyncio
async def test_missing_sdk_is_named_without_installing_or_leaking_resources(
    load_plugin,
    tmp_path,
    monkeypatch,
):
    import builtins

    cls, _ = channel_class(load_plugin)
    channel = cls.from_config(
        unused_process,
        DingTalkConfig(enabled=True, client_id="id", client_secret="secret"),
        workspace_dir=tmp_path,
    )
    original = builtins.__import__

    def missing(name, *args, **kwargs):
        if name == "dingtalk_stream" or name.startswith("dingtalk_stream."):
            raise ModuleNotFoundError(
                "No module named 'dingtalk_stream'",
                name="dingtalk_stream",
            )
        return original(name, *args, **kwargs)

    def no_install(*_args, **_kwargs):
        raise AssertionError("Plugin must never install dependencies")

    monkeypatch.setattr(builtins, "__import__", missing)
    monkeypatch.setattr(subprocess, "Popen", no_install)
    with pytest.raises(
        (ImportError, RuntimeError),
        match="dingtalk[-_]stream",
    ):
        await channel.start()
    assert (await channel.health_check())["status"] == "unhealthy"
    assert channel._client is None and channel._http is None
    await channel.stop()
