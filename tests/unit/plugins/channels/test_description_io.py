# -*- coding: utf-8 -*-
"""Exercise real descriptor I/O and transaction cancellation in isolation."""

from tests.isolated_pytest import run_mapped_suites


def test_description_io(tmp_path):
    run_mapped_suites(
        ["description_io_suite.py", "bundled_description_io_suite.py"],
        tmp_path,
    )
