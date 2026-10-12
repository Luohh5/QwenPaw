# -*- coding: utf-8 -*-
# pylint: disable=protected-access,redefined-outer-name
# pylint: disable=unused-argument
"""Step-one contracts: light registration, configuration and probe imports."""

import asyncio
import json
from pathlib import Path
import socket
import subprocess
import sys
import threading

import pytest

from qwenpaw.app.channels.base import BaseChannel
from qwenpaw.app.channels.renderer import ChannelDisplayConfig
from qwenpaw.config.config import FeishuConfig
from qwenpaw.constant import DEFAULT_MEDIA_DIR
from qwenpaw.plugins.architecture import PluginManifest

PLUGIN_DIR = Path(__file__).resolve().parents[3] / "plugins/channel/feishu"


class RecordingApi:
    """Record the public registration call; do not emulate the lifecycle."""

    def __init__(self):
        self.channels = []

    def register_channel(
        self,
        channel_class,
        label="",
        description="",
        config_fields=None,
        icon="",
        doc_url="",
    ):
        self.channels.append((channel_class, config_fields))


def channel_class(load_plugin, namespace="plugin_feishu"):
    """Resolve the class through the actual plugin entry."""
    api = RecordingApi()
    load_plugin(namespace).plugin.register(api)
    assert len(api.channels) == 1
    return api.channels[0][0]


async def unused_process(_request):
    """A process handler that must not be invoked during construction."""
    raise AssertionError("Agent processing started during construction")
    yield  # pylint: disable=unreachable


def test_register_without_platform_sdk(load_plugin, monkeypatch):
    """Adding eager SDK imports, connections or background work breaks this."""

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Registration must not start external work")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(threading.Thread, "start", forbidden)
    monkeypatch.setattr(subprocess.Popen, "__init__", forbidden)
    monkeypatch.setattr(asyncio.BaseEventLoop, "create_task", forbidden)

    api = RecordingApi()
    load_plugin().plugin.register(api)
    assert len(api.channels) == 1
    cls, fields = api.channels[0]
    assert issubclass(cls, BaseChannel)
    assert cls.channel == "feishu"
    channel = cls.from_config(unused_process, FeishuConfig(enabled=True))
    assert channel.enabled is True
    assert "lark_oapi" not in sys.modules
    assert "plugin_feishu.platform" not in sys.modules
    fields = {field["name"]: field for field in fields}
    assert fields["app_secret"]["type"] == "password"
    assert fields["encrypt_key"]["type"] == "password"
    assert fields["verification_token"]["type"] == "password"
    assert fields["app_id"]["required"] is True
    assert fields["domain"]["default"] == "feishu"
    assert fields["streaming_enabled"]["default"] is False


def test_probe_namespace_uses_relative_imports(load_plugin):
    """Probe registration must bind the probe's class, never the live class."""
    live = channel_class(load_plugin)
    probe = channel_class(load_plugin, "plugin_feishu__probe")
    assert live is not probe
    assert live.__module__ == "plugin_feishu.channel"
    assert probe.__module__ == "plugin_feishu__probe.channel"


def test_from_config_preserves_settings(load_plugin, tmp_path):
    """Legacy settings and explicit host overrides reach the new instance."""
    config = FeishuConfig(
        enabled=True,
        app_id="cli_test",
        app_secret="test-secret",
        encrypt_key="test-encryption",
        verification_token="test-verification",
        domain="lark",
        bot_prefix="[bot]",
        dm_policy="allowlist",
        group_policy="allowlist",
        allow_from=["ou_allowed"],
        deny_message="denied",
        require_mention=True,
        access_control_dm=True,
        access_control_group=True,
        streaming_enabled=True,
        share_session_in_group=True,
        auto_collapse_thinking=True,
        show_tool_calls=False,
        show_thinking=False,
        tool_result_max_length=0,
    )
    original = config.model_dump()
    callback = object()
    channel = channel_class(load_plugin).from_config(
        unused_process,
        config,
        callback,
        None,
        False,
        tmp_path,
    )
    assert channel.config.model_dump() == original
    assert channel._process is unused_process
    assert channel._on_reply_sent is callback
    assert channel._no_text_debounce is False
    assert channel.dm_policy == channel.group_policy == "allowlist"
    assert channel.allow_from == {"ou_allowed"}
    assert channel.deny_message == "denied"
    assert channel.require_mention and channel.streaming_enabled
    assert channel.access_control_dm and channel.access_control_group
    assert channel._display_config.show_tool_calls is False
    assert channel._display_config.show_thinking is False
    assert channel._display_config.tool_result_max_length == 0
    assert channel._workspace_dir == tmp_path
    assert channel._media_dir == tmp_path / "media"
    assert not (tmp_path / "media").exists()
    assert config.model_dump() == original


def test_config_snapshot_and_display_override(load_plugin):
    """Instances do not share mutable config, and host rendering wins."""
    config = FeishuConfig(allow_from=["original"], no_text_debounce=False)
    display = ChannelDisplayConfig(show_tool_calls=False)
    cls = channel_class(load_plugin)
    first = cls.from_config(unused_process, config, display_config=display)
    second = cls.from_config(unused_process, config)
    config.allow_from.append("later")
    first.config.allow_from.append("first-only")
    assert second.config.allow_from == ["original"]
    assert second.allow_from == {"original"}
    assert first._display_config is display
    assert second._display_config.show_tool_calls is True
    # Match the old from_config: the explicit argument governs debounce.
    assert first._no_text_debounce is True
    assert first.enabled is False
    assert first.config.domain == "feishu"
    assert first.config.streaming_enabled is False
    assert first._media_dir == DEFAULT_MEDIA_DIR


@pytest.mark.parametrize("domain", ["feishu", "lark", "https://gateway.test"])
def test_explicit_media_path_and_domain(load_plugin, tmp_path, domain):
    """Explicit media paths take precedence over the workspace default."""
    channel = channel_class(load_plugin).from_config(
        unused_process,
        FeishuConfig(media_dir="~/feishu-media", domain=domain),
        workspace_dir=tmp_path,
    )
    assert channel._media_dir == Path("~/feishu-media").expanduser()
    assert channel.config.domain == domain


def test_manifest_is_valid_and_not_published():
    """The unfinished transport must not become a released channel."""
    data = json.loads((PLUGIN_DIR / "plugin.json").read_text())
    manifest = PluginManifest.model_validate(data)
    assert manifest.id == "feishu"
    assert manifest.plugin_type.value == "channel"
    assert (PLUGIN_DIR / manifest.entry.backend).is_file()
    assert data["publish"] is False


@pytest.mark.asyncio
async def test_unstarted_transport_does_not_report_success(load_plugin):
    """An accidental call to this preparatory package must fail explicitly."""
    channel = channel_class(load_plugin).from_config(
        unused_process,
        FeishuConfig(enabled=True),
    )
    with pytest.raises((RuntimeError, NotImplementedError)):
        await channel.send("ou_test", "hello")
    assert (await channel.health_check())["status"] == "unhealthy"
    await channel.stop()
    await channel.stop()
