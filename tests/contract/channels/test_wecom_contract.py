# -*- coding: utf-8 -*-
"""Retained WeCom duties on the isolated official plugin boundaries.

Original sources/assertions and intentional contract changes are retained in
wecom-old-oracle-map.json. This entry never imports a retired Core channel.
"""

import pytest

from tests.wecom_isolated import run_wecom_suite


@pytest.mark.parametrize(
    "suite",
    ["wecom_contract_suite.py"],
)
def test_wecom_official_inherited_contracts(suite, tmp_path):
    run_wecom_suite(suite, tmp_path)
