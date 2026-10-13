# -*- coding: utf-8 -*-
# pylint: disable=wrong-import-position,wrong-import-order
"""Shared BaseChannel contracts apply to official host proxies without SDKs."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from tests.contract.channels import ChannelContractTest  # noqa: E402
from c6_artifacts import proxy  # noqa: E402


class TestFeishuPluginContract(ChannelContractTest):
    def create_instance(self):
        return proxy("feishu", {"enabled": True})


class TestDingTalkPluginContract(ChannelContractTest):
    def create_instance(self):
        return proxy("dingtalk", {"enabled": True})
