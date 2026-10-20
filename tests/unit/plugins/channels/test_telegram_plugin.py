# -*- coding: utf-8 -*-
"""Run Telegram contracts outside repository-wide platform mocks."""

import pytest

from tests.isolated_pytest import run_isolated_pytest


@pytest.mark.parametrize(
    "suite_name",
    [
        "telegram_contract_suite.py",
        "telegram_control_suite.py",
        "telegram_config_suite.py",
        "telegram_pure_suite.py",
        "telegram_business_suite.py",
        "telegram_route_suite.py",
        "telegram_media_suite.py",
        "telegram_cards_suite.py",
        "telegram_stream_suite.py",
        "telegram_reliability_suite.py",
        "telegram_polling_suite.py",
        "telegram_actual_agent_suite.py",
        "telegram_lifecycle_suite.py",
        "telegram_backoff_suite.py",
        "telegram_edge_suite.py",
        "telegram_fix1_suite.py",
        "telegram_legacy_oracle_suite.py",
    ],
)
def test_telegram_plugin(tmp_path, suite_name):
    run_isolated_pytest(
        suite_name,
        env={
            "QWENPAW_WORKING_DIR": str(tmp_path / "host"),
            "QWENPAW_SECRET_DIR": str(tmp_path / "secrets"),
            "QWENPAW_BACKUP_DIR": str(tmp_path / "backups"),
        },
        timeout=120,
    )
