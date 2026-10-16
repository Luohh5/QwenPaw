# -*- coding: utf-8 -*-
"""Retained OneBot responsibilities at the actual v2 plugin boundary.

Original assertions and contract changes: onebot-oracle-status.json.
"""

import pytest

from tests.onebot_isolated import run_onebot_suite


@pytest.mark.parametrize(
    "suite",
    [
        "onebot_lifecycle_closeout_suite.py",
        "onebot_watchdog_closeout_suite.py",
    ],
)
def test_onebot_v2_lifecycle(suite, tmp_path):
    run_onebot_suite(suite, tmp_path)
