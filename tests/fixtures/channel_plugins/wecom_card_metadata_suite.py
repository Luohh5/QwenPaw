"""The final original WeCom card metadata oracle, unchanged."""

from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "plugins/channel/wecom"))
from qwenpaw_channel_wecom.cards import tool_guard as tg

class TestModuleMetadata:
    def test_dispatcher_metadata(self):
        assert tg.NAME == "tool_guard_approval"
        assert tg.MESSAGE_TYPE == "tool_guard_approval"
        assert tg.TASK_ID_PREFIX == "tg_approval_"
        assert (tg.APPROVE_KEY, tg.DENY_KEY) == ("approve", "deny")
