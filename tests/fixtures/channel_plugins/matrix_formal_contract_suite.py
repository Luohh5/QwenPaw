"""Original 19 inherited assertions on the unchanged bundled Matrix factory."""
import importlib
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
ChannelContractTest = importlib.import_module(
    "tests.contract.channels"
).ChannelContractTest


class TestMatrixBotProxyContract(ChannelContractTest):
    @pytest.fixture(autouse=True)
    def official_factory(self, tmp_path):
        from qwenpaw.plugins.official_channels import bundled_channel_source

        self.workspace = tmp_path / "workspace"
        with bundled_channel_source("matrix") as source:
            self.source = source
            yield

    def create_instance(self):
        from qwenpaw.__version__ import __version__
        from qwenpaw.plugins.architecture import PluginManifest
        from qwenpaw.plugins.channel_adapter import describe_channel
        from qwenpaw.runtime.runtime import Runtime

        manifest = PluginManifest.from_dict(
            json.loads((self.source / "plugin.json").read_bytes())
        )
        assert manifest.id == "matrix"
        return describe_channel(self.source, manifest).create_proxy(
            Runtime.run,
            dict(
                enabled=False,
                homeserver="http://127.0.0.1:1",
                user_id="@bot:example.com",
                access_token="local-token",
            ),
            python_executable=sys.executable,
            support_root=ROOT / "src",
            instance_id="matrix-original-contract",
            generation=1,
            qwenpaw_version=__version__,
            workspace_dir=self.workspace,
        )
