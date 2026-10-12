# -*- coding: utf-8 -*-
"""Lightweight Feishu registration using the existing channel interface."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from qwenpaw.plugins.api import PluginApi


class FeishuChannelPlugin:
    """Declare the channel without loading its platform SDK."""

    def register(self, api: PluginApi) -> None:
        from qwenpaw.config.config import FeishuConfig

        from .channel import FeishuChannel

        api.register_channel(
            channel_class=FeishuChannel,
            label="Feishu / Lark",
            description="Feishu / Lark messaging channel",
            config_fields=[
                {
                    "name": name,
                    "label": label,
                    "type": kind,
                    "default": FeishuConfig.model_fields[name].default,
                    "required": name in ("app_id", "app_secret"),
                }
                for name, label, kind in (
                    ("app_id", "App ID", "text"),
                    ("app_secret", "App Secret", "password"),
                    ("encrypt_key", "Encrypt Key", "password"),
                    ("verification_token", "Verification Token", "password"),
                    ("domain", "Domain", "text"),
                    ("media_dir", "Media directory", "text"),
                    ("streaming_enabled", "Streaming", "switch"),
                    (
                        "share_session_in_group",
                        "Shared group session",
                        "switch",
                    ),
                    ("auto_collapse_thinking", "Collapse thinking", "switch"),
                )
            ],
        )


plugin = FeishuChannelPlugin()
