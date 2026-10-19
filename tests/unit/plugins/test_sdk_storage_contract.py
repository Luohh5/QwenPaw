# -*- coding: utf-8 -*-
"""Public persistent storage and configuration boundaries."""

import json
from pathlib import Path
from urllib.parse import unquote, urlparse

import pytest


def make_plugin(tmp_path):
    root = tmp_path / "storage-plugin"
    root.mkdir()
    (root / "plugin.json").write_text(
        json.dumps(
            {
                "id": "storage-contract",
                "version": "1.0.0",
                "entry": {"backend": "main.py"},
                "sdk": {
                    "version": "1.0",
                    "capabilities": ["storage", "config"],
                    "secrets": ["token"],
                },
            }
        ),
        encoding="utf-8",
    )
    (root / "main.py").write_text(
        "class Plugin:\n    def register(self, ctx):\n        pass\nplugin = Plugin()\n",
        encoding="utf-8",
    )
    return root


async def test_reload_preserves_data_and_rejects_old_generation_writes(tmp_path):
    from qwenpaw.plugins import StaleContextError
    from qwenpaw.plugins.testing import PluginTestHost

    async with PluginTestHost(tmp_path / "host") as host:
        old = await host.load(make_plugin(tmp_path))
        old.storage.write_json("nested/state.json", {"value": "retained"})
        assert (await host.reload("storage-contract")).ok
        current = host.context("storage-contract")
        assert current.storage.read_json("nested/state.json") == {"value": "retained"}
        with pytest.raises(StaleContextError):
            old.storage.write_json("nested/state.json", {"value": "stale"})
        with pytest.raises(StaleContextError):
            old.storage.delete("nested/state.json")
        assert current.storage.read_json("nested/state.json") == {"value": "retained"}


async def test_storage_rejects_path_escape_without_touching_other_files(tmp_path):
    from qwenpaw.plugins import SDKError
    from qwenpaw.plugins.testing import PluginTestHost

    outside = tmp_path / "outside.json"
    outside.write_text("untouched", encoding="utf-8")
    async with PluginTestHost(tmp_path / "host") as host:
        ctx = await host.load(make_plugin(tmp_path))
        for name in (str(outside), "../outside.json", "nested/../../outside.json"):
            with pytest.raises(SDKError):
                ctx.storage.write_json(name, {"overwrite": True})
        assert outside.read_text() == "untouched"


async def test_config_snapshot_and_explicit_secret_access(tmp_path):
    from qwenpaw.plugins import SDKError
    from qwenpaw.plugins.testing import PluginTestHost

    initial = {"token": "test-only-value", "options": {"quality": "high"}}
    async with PluginTestHost(tmp_path / "host") as host:
        ctx = await host.load(make_plugin(tmp_path), config=initial)
        initial["options"]["quality"] = "changed"
        returned = ctx.config.get("options")
        assert returned == {"quality": "high"}
        returned["quality"] = "changed-again"
        assert ctx.config.get("options") == {"quality": "high"}
        with pytest.raises(SDKError):
            ctx.config.get("token")
        assert ctx.config.secret("token") == "test-only-value"
        with pytest.raises(SDKError):
            ctx.config.secret("undeclared")


async def test_media_reference_remains_readable_after_reload(tmp_path):
    from qwenpaw.plugins.messages import Attachment
    from qwenpaw.plugins.testing import PluginTestHost

    async with PluginTestHost(tmp_path / "host") as host:
        ctx = await host.load(make_plugin(tmp_path))
        attachment = ctx.media.write(
            "results/sample.txt", b"persistent media", mime_type="text/plain"
        )
        assert isinstance(attachment, Attachment)
        assert attachment.mime_type == "text/plain" and attachment.name == "sample.txt"
        parsed = urlparse(attachment.url)
        assert parsed.scheme == "file"
        path = Path(unquote(parsed.path))
        assert path.read_bytes() == b"persistent media"
        assert attachment.preview_url.startswith("/api/files/preview/")
        assert (await host.reload("storage-contract")).ok
        assert path.read_bytes() == b"persistent media"


async def test_tool_workspace_data_and_media_are_independent(tmp_path):
    from qwenpaw.plugins.testing import PluginTestHost
    from .sdk_author_fixture import make_plugin as extension_plugin

    source = """
from qwenpaw.plugins.tools import ToolSpec, ToolResult
from qwenpaw.plugins.messages import ContentBlock
class Plugin:
    def register(self, ctx):
        async def count(call, args):
            value = call.storage.read_json("count.json", 0) + 1
            call.storage.write_json("count.json", value)
            attachment = call.media.write("same.txt", call.workspace_id.encode(), mime_type="text/plain")
            return ToolResult(content=(ContentBlock(text=str(value)),), metadata={"url": attachment.url})
        ctx.register_tool(ToolSpec("workspace_count", "count", {"type":"object"}, count, enabled=True))
plugin = Plugin()
"""
    async with PluginTestHost(tmp_path / "host") as host:
        ctx = await host.load(extension_plugin(tmp_path, source, ["tools", "storage"]))
        ctx.storage.write_json("count.json", 99)
        one = await host.call_tool("workspace_count", {}, workspace_id="one")
        two = await host.call_tool("workspace_count", {}, workspace_id="two")
        again = await host.call_tool("workspace_count", {}, workspace_id="one")
        assert [r.content[0].text for r in (one, two, again)] == ["1", "1", "2"]
        assert one.metadata["url"] != two.metadata["url"]
        for result, expected in ((one, b"one"), (two, b"two")):
            assert (
                Path(unquote(urlparse(result.metadata["url"]).path)).read_bytes()
                == expected
            )
        assert ctx.storage.read_json("count.json") == 99
        assert (await host.reload("extension-contract")).ok
        assert (
            await host.call_tool("workspace_count", {}, workspace_id="two")
        ).content[0].text == "2"


async def test_tool_settings_are_declared_workspace_scoped_and_persistent(tmp_path):
    from qwenpaw.plugins import SDKError
    from qwenpaw.plugins.testing import PluginTestHost
    from .sdk_author_fixture import make_plugin as extension_plugin

    source = """
from qwenpaw.plugins.tools import ToolSpec, ToolResult
class Plugin:
    def register(self, ctx):
        async def settings(call, args):
            return ToolResult.text(call.config.get("quality") + ":" + call.config.secret("token"))
        ctx.register_tool(ToolSpec("settings_echo", "settings", {"type":"object"}, settings, enabled=True,
            config_fields=({"name":"quality", "label":"Quality", "type":"text", "default":"normal"},
                           {"name":"token", "label":"Token", "type":"password", "default":"test-default"})))
plugin = Plugin()
"""
    async with PluginTestHost(tmp_path / "host") as host:
        await host.load(extension_plugin(tmp_path, source, ["tools", "config"]))
        await host.configure_tool(
            "settings_echo",
            {"quality": "high", "token": "test-one"},
            workspace_id="one",
        )
        assert (await host.call_tool("settings_echo", {}, workspace_id="one")).content[
            0
        ].text == "high:test-one"
        assert (await host.call_tool("settings_echo", {}, workspace_id="two")).content[
            0
        ].text == "normal:test-default"
        with pytest.raises(SDKError):
            await host.configure_tool("settings_echo", {"undeclared": "value"})
        assert (await host.reload("extension-contract")).ok
        assert (await host.call_tool("settings_echo", {}, workspace_id="one")).content[
            0
        ].text == "high:test-one"


async def test_tool_media_preserves_public_preview_reference(tmp_path):
    from qwenpaw.plugins.testing import PluginTestHost
    from .sdk_author_fixture import make_plugin as extension_plugin

    source = """
from qwenpaw.plugins.tools import ToolSpec, ToolResult
from qwenpaw.plugins.messages import ContentBlock
class Plugin:
    def register(self, ctx):
        async def image(call, args):
            attachment = call.media.write("image.png", b"image-data", mime_type="image/png")
            return ToolResult((ContentBlock("image", attachment=attachment),), metadata={"preview":attachment.preview_url})
        ctx.register_tool(ToolSpec("preview_image", "preview", {"type":"object"}, image, enabled=True))
plugin = Plugin()
"""
    async with PluginTestHost(tmp_path / "host") as host:
        await host.load(extension_plugin(tmp_path, source, ["tools", "storage"]))
        result = await host.call_tool("preview_image", {})
        attachment = result.content[0].attachment
        assert attachment.preview_url == result.metadata["preview"]
        assert attachment.preview_url.startswith("/api/files/preview/")
        assert (
            Path(unquote(urlparse(attachment.url).path)).read_bytes() == b"image-data"
        )
