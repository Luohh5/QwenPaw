"""Compatibility execution entry for mapped v2 OneBot lifecycle oracles.

Original v1 assertions remain in the protected onebot-legacy-snapshot.
No v1 controller/lease or retired Core imports are executed here.
"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from tests.onebot_isolated import run_onebot_suite  # noqa: E402


def test_current_onebot_v2_boundary(tmp_path):
    run_onebot_suite(
        [
            "onebot_lifecycle_closeout_suite.py",
            "onebot_watchdog_closeout_suite.py",
        ],
        tmp_path,
    )
