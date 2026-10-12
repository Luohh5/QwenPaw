# -*- coding: utf-8 -*-
"""Register DingTalk without importing its SDK or opening connections."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from qwenpaw.plugins.api import PluginApi


class DingTalkChannelPlugin:
    def register(self, api: PluginApi) -> None:
        from qwenpaw.config.config import DingTalkConfig
        from .channel import DingTalkChannel

        api.register_channel(
            channel_class=DingTalkChannel,
            label="DingTalk",
            description="DingTalk messaging channel",
            config_fields=[
                {
                    "name": name,
                    "label": name.replace("_", " ").title(),
                    "type": kind,
                    "default": DingTalkConfig.model_fields[name].default,
                    "required": name in ("client_id", "client_secret"),
                }
                for name, kind in (
                    ("client_id", "text"),
                    ("client_secret", "password"),
                    ("message_type", "text"),
                    ("cron_message_type", "text"),
                    ("card_template_id", "text"),
                    ("card_template_key", "text"),
                    ("robot_code", "text"),
                    ("card_auto_layout", "switch"),
                    ("at_sender_on_reply", "switch"),
                    ("media_dir", "text"),
                    ("endpoint", "text"),
                    ("streaming_enabled", "switch"),
                    ("share_session_in_group", "switch"),
                )
            ],
        )


plugin = DingTalkChannelPlugin()
