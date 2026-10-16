# -*- coding: utf-8 -*-
"""Original 19 inherited contracts against the official Matrix proxy."""
from tests.matrix_isolated import run_matrix_suite


def test_matrix_official_inherited_contracts(tmp_path):
    run_matrix_suite("matrix_formal_contract_suite.py", tmp_path)
