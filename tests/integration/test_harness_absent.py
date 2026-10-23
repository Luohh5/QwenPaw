# -*- coding: utf-8 -*-
"""A native chat stays independent of installed but disabled Harness code."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


REPO_ROOT = Path(__file__).resolve().parents[2]

NATIVE_CHILD = r"""
import asyncio
import builtins
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
from unittest.mock import patch

forbidden = (
    "qwenpaw.harnesses", "openai_codex", "codex_cli_bin", "qoder_agent_sdk"
)
original_import = builtins.__import__
original_which = shutil.which
forbidden_attempts = []
vendor_probe_attempts = []
process_attempts = []

def guarded_import(name, *args, **kwargs):
    if any(name == root or name.startswith(root + ".") for root in forbidden):
        forbidden_attempts.append(name)
        raise AssertionError(name)
    return original_import(name, *args, **kwargs)

def guarded_which(command, *args, **kwargs):
    if Path(str(command)).name.lower() in {"codex", "qoder", "qodercli"}:
        vendor_probe_attempts.append(str(command))
        raise AssertionError("Native path attempted vendor binary probe")
    return original_which(command, *args, **kwargs)

def no_process(event, _args):
    if event in {"subprocess.Popen", "os.system", "os.posix_spawn", "os.fork"}:
        process_attempts.append(event)
        raise AssertionError("Native path attempted process launch")

builtins.__import__ = guarded_import
shutil.which = guarded_which
sys.addaudithook(no_process)

from agentscope.formatter import OpenAIChatFormatter
from agentscope.model import ChatModelBase
from agentscope.model._model_response import ChatResponse
from qwenpaw.app.agent_startup import AgentStartupStatus
from qwenpaw.app import _app as actual_app
from qwenpaw.app.routers.agents import router as agents_router
from qwenpaw.config.utils import load_config
from qwenpaw.plugins.settings import persist_plugin_settings
from qwenpaw.plugins.testing import PluginTestHost

class Model(ChatModelBase):
    def __init__(self):
        super().__init__(
            credential=None,
            model="mock-native",
            parameters=ChatModelBase.Parameters(),
            stream=False,
            context_size=32768,
        )
        self.formatter = OpenAIChatFormatter()

    async def __call__(self, *args, **kwargs):
        return ChatResponse(
            content=[{"type": "text", "text": "native-answer"}],
            is_last=True,
        )

async def main():
    root = Path(sys.argv[1])
    entry_marker = root / "disabled-entry-executed"
    assert actual_app.app is not None
    assert not (
        forbidden_attempts or vendor_probe_attempts or process_attempts
    )
    if os.environ.get("QWENPAW_A6_EXPECT_SDKS") == "1":
        assert importlib.util.find_spec("codex_cli_bin") is not None
        assert importlib.util.find_spec("qoder_agent_sdk") is not None
    with patch(
        "qwenpaw.agents.model_factory.create_model_and_formatter",
        side_effect=lambda **_kwargs: (Model(), OpenAIChatFormatter()),
    ):
        async with PluginTestHost(root) as host:
            persist_plugin_settings("qwenpaw-harness-codex", enabled=False)
            await host._loader.load_all_plugins(configs=load_config().plugins)
            assert not entry_marker.exists()
            assert not any(
                row["status"] == "available" for row in host.list_harnesses()
            )
            host._workspace_manager.get_agent_startup_status = (
                lambda _id, *, enabled: AgentStartupStatus.RUNNING
                if enabled else AgentStartupStatus.DISABLED
            )
            host._http_app.include_router(agents_router, prefix="/api")
            events = [event async for event in host.stream_chat("hello")]
            assert events[-1].status == "completed"
            listed = await host.request("GET", "/api/agents")
            assert listed.status == 200
            native = next(
                row for row in listed.json()["agents"] if row["id"] == "test"
            )
            assert native["backend"] == "qwenpaw"
            assert native["backend_status"] == "available"
            history = root / "workspaces/test/sessions/console/test.json"
            saved = json.loads(history.read_text(encoding="utf-8"))
            assert any(
                block.get("text") == "native-answer"
                for message in saved["agent"]["state"]["context"]
                if message["role"] == "assistant"
                for block in message["content"]
            )
            assert "qwenpaw.runtime.runtime" in sys.modules
            assert "qwenpaw.agents.react_agent" in sys.modules
    assert importlib.util.find_spec("qwenpaw.harnesses") is None
    assert not entry_marker.exists()
    assert not any(
        name == root or name.startswith(root + ".")
        for name in sys.modules
        for root in forbidden
    )
    assert not (
        forbidden_attempts or vendor_probe_attempts or process_attempts
    )
    print(json.dumps({"native_history": True, "vendor_probes": 0}))

asyncio.run(main())
"""


def test_native_chat_and_history_need_no_harness_runtime(
    tmp_path: Path,
) -> None:
    root = tmp_path / "user"
    plugin = root / "plugins" / "qwenpaw-harness-codex"
    plugin.mkdir(parents=True)
    (plugin / "plugin.json").write_text(
        json.dumps(
            {
                "id": "qwenpaw-harness-codex",
                "version": "1.0.0",
                "entry": {"backend": "main.py"},
                "sdk": {"version": "1.1.0", "capabilities": ["harnesses"]},
                "meta": {"harnesses": [{"id": "codex", "label": "Codex"}]},
            },
        ),
        encoding="utf-8",
    )
    (plugin / "main.py").write_text(
        "from pathlib import Path\n"
        'Path(__file__).parents[2].joinpath("disabled-entry-executed")'
        '.write_text("executed")\n'
        'raise AssertionError("Disabled Harness entry executed")\n',
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "QWENPAW_WORKING_DIR": str(root),
        "QWENPAW_SECRET_DIR": str(root / "secrets"),
        "QWENPAW_RUNTIME_ID": "a6-native-absent",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    (tmp_path / "home").mkdir()
    process = subprocess.run(
        [sys.executable, "-c", NATIVE_CHILD, str(root)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert process.returncode == 0, process.stderr
    assert json.loads(process.stdout) == {
        "native_history": True,
        "vendor_probes": 0,
    }
