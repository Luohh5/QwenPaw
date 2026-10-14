# -*- coding: utf-8 -*-
"""Run CH-0-008 tests outside legacy Channel imports."""

from pathlib import Path
import subprocess
import sys

SUITE = (
    Path(__file__).parents[2]
    / "fixtures"
    / "channel_isolation"
    / "onebot"
    / "ch_0_008_onebot_suite.py"
)


def test_ch_0_008_in_isolated_pytest_process() -> None:
    """Verify the production OneBot Driver in an isolated test process."""
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            """
import pathlib, sys
source = pathlib.Path(sys.argv[1]).resolve()
suite = pathlib.Path(sys.argv[2]).resolve()
sys.path.insert(0, str(source))
import qwenpaw
assert pathlib.Path(qwenpaw.__file__).resolve().is_relative_to(source)
import pytest
raise SystemExit(pytest.main([
    '--confcutdir', str(suite.parent), str(suite), '-q',
]))
""",
            str(SUITE.parents[4] / "src"),
            str(SUITE),
        ],
        capture_output=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout.decode(
        errors="replace",
    ) + result.stderr.decode(errors="replace")
