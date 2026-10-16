"""
All 19 original inherited oracles on the actual unstarted XiaoYi host proxy.
"""
import json
from pathlib import Path  # noqa: E402
import sys

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from tests.contract.channels import ChannelContractTest  # noqa: E402
from qwenpaw.app.channels.renderer import ChannelDisplayConfig  # noqa: E402
from qwenpaw.plugins.architecture import PluginManifest  # noqa: E402
from qwenpaw.plugins.channel_adapter import describe_channel  # noqa: E402
from qwenpaw.runtime.runtime import Runtime  # noqa: E402
from qwenpaw.plugins.official_channels import bundled_channel_source  # noqa: E402
from qwenpaw.__version__ import __version__  # noqa: E402


class TestXiaoYiPluginContract(ChannelContractTest):
    @pytest.fixture(autouse=True)
    def _formal_workspace(self, tmp_path):
        self._workspace_dir = tmp_path / "workspace"
        self._workspace_dir.mkdir()
        with bundled_channel_source("xiaoyi") as source:
            self._formal_source = source
            yield

    def create_instance(self):
        source = self._formal_source
        manifest = PluginManifest.from_dict(
            json.loads((source / "plugin.json").read_text())
        )
        description = describe_channel(source, manifest)
        # Actual Runtime method is stored only; these inherited
        # interface checks
        # never run a process callback, install a plugin, or claim
        # Agent execution.
        return description.create_proxy(
            Runtime.run,
            {
                "enabled": False,
                "ak": "local-ak",
                "sk": "local-sk",
                "agent_id": "local-agent",
            },
            python_executable=sys.executable,
            support_root=ROOT / "src",
            instance_id="xiaoyi-inherited-contract",
            generation=1,
            qwenpaw_version=__version__,
            workspace_dir=self._workspace_dir,
            display_config=ChannelDisplayConfig(
                show_tool_calls=False, show_tool_results=False
            ),
        )
