"""All original inherited interface assertions on actual official iMessage proxy."""
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT))
from tests.contract.channels import ChannelContractTest
from qwenpaw.app.channels.renderer import ChannelDisplayConfig
from qwenpaw.plugins.architecture import PluginManifest
from qwenpaw.plugins.channel_adapter import describe_channel
from qwenpaw.plugins.official_channels import bundled_channel_source
from qwenpaw.runtime.runtime import Runtime
from qwenpaw.__version__ import __version__


class TestIMessagePluginContract(ChannelContractTest):
    @pytest.fixture(autouse=True)
    def workspace(self, tmp_path):
        self._workspace = tmp_path/'workspace'
        with bundled_channel_source('imessage') as source:
            self._source = source
            yield

    def create_instance(self):
        description = describe_channel(self._source,PluginManifest.from_dict(json.loads((self._source/'plugin.json').read_bytes())))
        return description.create_proxy(Runtime.run,{'enabled':False,'db_path':str(self._workspace/'never-open.sqlite'),'poll_sec':5.0,'bot_prefix':'[Test]'},python_executable=sys.executable,support_root=ROOT/'src',instance_id='imessage-original-contract',generation=1,qwenpaw_version=__version__,workspace_dir=self._workspace,display_config=ChannelDisplayConfig(show_tool_calls=False,show_tool_results=False))
