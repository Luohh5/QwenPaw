"""Retain all 19 inherited oracles on the real unstarted host proxy factory."""

import json
from pathlib import Path
import sys
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from tests.contract.channels import ChannelContractTest
from qwenpaw.app.channels.renderer import ChannelDisplayConfig
from qwenpaw.plugins.architecture import PluginManifest
from qwenpaw.plugins.channel_adapter import describe_channel


class TestYuanbaoPluginContract(ChannelContractTest):
    def create_instance(self):
        source = ROOT / "plugins/channel/yuanbao"
        manifest = PluginManifest.from_dict(json.loads((source / "plugin.json").read_text()))
        description = describe_channel(source, manifest)
        return description.create_proxy(
            AsyncMock(), {"enabled": False, "app_id": "test_app_id", "app_secret": "test_app_secret"},
            python_executable=sys.executable, support_root=ROOT / "src",
            instance_id="yuanbao-inherited-contract", generation=1,
            qwenpaw_version="2.2.2b4",
            display_config=ChannelDisplayConfig(show_tool_calls=False, show_tool_results=False),
        )
