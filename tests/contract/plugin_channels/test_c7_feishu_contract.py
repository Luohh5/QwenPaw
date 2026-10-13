# -*- coding: utf-8 -*-
"""Official Feishu host contracts; platform effects are worker-owned."""

from tests.contract.channels import ChannelContractTest
from tests.fixtures.channel_plugins.c6_artifacts import proxy


class TestFeishuChannelContract(ChannelContractTest):
    """Apply the shared host contract without importing a platform SDK."""

    def create_instance(self):
        return proxy("feishu", {"enabled": True, "bot_prefix": "[Test]"})

    def test_channel_type_is_feishu(self, instance):
        assert instance.channel == "feishu"

    def test_construction_does_not_start_worker(self, instance):
        assert instance.channel_process.health_snapshot()["pid"] is None
