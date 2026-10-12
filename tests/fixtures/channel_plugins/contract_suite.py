# -*- coding: utf-8 -*-
"""Apply the existing host contract to the real plugin class without SDKs."""

import sys
from pathlib import Path

import pytest
from registration_suite import channel_class, unused_process

from qwenpaw.config.config import FeishuConfig

# Import the shared contract only; --confcutdir excludes root SDK mocks.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
# pylint: disable=wrong-import-position
from tests.contract.channels import ChannelContractTest  # noqa: E402


class TestFeishuPluginContract(ChannelContractTest):
    @pytest.fixture(autouse=True)
    def registered_channel(self, load_plugin):
        self.channel_cls = channel_class(load_plugin)

    def create_instance(self):
        return self.channel_cls.from_config(
            unused_process,
            FeishuConfig(enabled=True, app_id="cli_test", app_secret="secret"),
        )


class TestDingTalkPluginContract(ChannelContractTest):
    @pytest.fixture(autouse=True)
    def registered_channel(self, load_plugin):
        from dingtalk_registration_suite import channel_class as dingtalk_class

        self.channel_cls, _ = dingtalk_class(load_plugin)

    def create_instance(self):
        from qwenpaw.config.config import DingTalkConfig

        return self.channel_cls.from_config(
            unused_process,
            DingTalkConfig(
                enabled=True,
                client_id="client-test",
                client_secret="secret",
            ),
        )
