"""All original nineteen contracts on the real bundled unstarted Proxy."""

import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from tests.contract.channels import ChannelContractTest  # noqa: E402
from qwenpaw.app.channels.renderer import ChannelDisplayConfig  # noqa: E402
from qwenpaw.plugins.architecture import PluginManifest  # noqa: E402
from qwenpaw.plugins.channel_adapter import describe_channel  # noqa: E402
from qwenpaw.plugins.official_channels import (  # noqa: E402
    bundled_channel_source,
)  # noqa: E402
from qwenpaw.runtime.runtime import Runtime  # noqa: E402
from qwenpaw.__version__ import __version__  # noqa: E402
from voice_closeout_support import voice_host_guard  # noqa: E402

__all__ = ["voice_host_guard"]


class TestVoicePluginContract(ChannelContractTest):
    @pytest.fixture(autouse=True)
    def _formal_workspace(self, tmp_path):
        self._workspace_dir = tmp_path / "workspace"
        self._workspace_dir.mkdir()
        with bundled_channel_source("voice") as source:
            self._formal_source = source
            yield

    def create_instance(self):
        source = self._formal_source
        manifest = PluginManifest.from_dict(
            json.loads((source / "plugin.json").read_text())
        )
        description = describe_channel(source, manifest)
        instance = description.create_proxy(
            Runtime.run,
            {
                "enabled": False,
                "twilio_account_sid": "AC-formal",
                "twilio_auth_token": "formal-local-voice-secret",
                "phone_number_sid": "PN-formal",
            },
            python_executable=sys.executable,
            support_root=ROOT / "src",
            instance_id="voice-inherited-contract",
            generation=1,
            qwenpaw_version=__version__,
            workspace_dir=self._workspace_dir,
            display_config=ChannelDisplayConfig(
                show_tool_calls=False,
                show_tool_results=False,
            ),
        )
        assert instance.channel_process.health_snapshot()["pid"] is None
        assert instance._launch_spec.plugin_id == "voice"
        return instance
