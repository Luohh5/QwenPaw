# -*- coding: utf-8 -*-
# Pytest injects named fixtures; these tests also inspect agent internals.
# pylint: disable=redefined-outer-name,unused-argument,protected-access
"""Real context components, bounded recovery and per-run isolation."""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from agentscope.message import Msg, TextBlock
from agentscope.tool import ToolResponse

from qwenpaw.agents.context.scroll.history import HistoryStore
from qwenpaw.agents.context.types import LogEntry
from qwenpaw.agents.middlewares import ToolResultPruningMiddleware
from qwenpaw.agents.tools.utils import TRUNCATION_METADATA_KEY
from qwenpaw.selflearn import analyzer
from tests.unit.runtime import test_selflearn_analyze as first

config = first.config
episode = first.episode
result = first.result


@pytest.mark.parametrize(
    "strategy,enabled,scroll",
    [
        ("scroll", True, True),
        ("native", True, False),
        ("scroll", False, False),
    ],
)
async def test_context_settings_are_wired_without_sharing_paths(
    config,
    episode,
    result,
    tmp_path,
    strategy,
    enabled,
    scroll,
):
    lcc = config.running.light_context_config
    lcc.strategy = strategy
    lcc.context_compact_config.enabled = enabled
    lcc.context_compact_config.compact_threshold_ratio = 0.5
    lcc.scroll_config.db_filename = "../production.db"
    lcc.dialog_path = "../production-dialog"
    lcc.tool_result_pruning_config.tool_results_cache = "../production-cache"
    lcc.tool_result_pruning_config.pruning_recent_msg_max_bytes = 2000
    before = config.model_dump()
    agent = analyzer.build_analyzer(
        config,
        first.OfflineToolModel(result),
        "Analyze",
        episode,
        "private",
        [],
        tmp_path / "private",
    )
    try:
        assert config.model_dump() == before
        assert agent.context_config.trigger_ratio == 0.5
        assert (agent._context_manager is not None) == scroll
        assert bool(await agent.toolkit.get_tool("recall_history")) == scroll
        assert await agent.toolkit.get_tool("recall_history_python") is None
        assert await agent.toolkit.get_tool("execute_shell_command") is None
        assert (await agent.toolkit.get_tool("read_file")).is_read_only
        assert agent._workspace_dir == tmp_path / "private"
        assert not (tmp_path / "production.db").exists()
        assert not (tmp_path / "production-cache").exists()
    finally:
        await agent.close()
    if scroll:
        assert agent._context_manager._history.closed


async def test_pruned_long_line_can_be_recovered_exactly(
    config,
    episode,
    result,
    tmp_path,
):
    budget = 1200
    pruning_config = (
        config.running.light_context_config.tool_result_pruning_config
    )
    pruning_config.pruning_recent_msg_max_bytes = budget
    agent = analyzer.build_analyzer(
        config,
        first.OfflineToolModel(result),
        "Analyze",
        episode,
        "private",
        [],
        tmp_path,
    )
    try:
        # Match the tools' multiline JSON wrapper around a very long field.
        text = '{\n"diff": "' + "原文🙂\\n+`code` " * 200 + '"\n}'
        pruning = next(
            mw
            for mw in agent._acting_middlewares
            if isinstance(mw, ToolResultPruningMiddleware)
        )
        response = await pruning.prune_tool_response_async(
            ToolResponse(
                content=[TextBlock(text=text)],
            ),
        )
        info = response.metadata[TRUNCATION_METADATA_KEY]["0"]
        assert response.content[0].text != text
        assert Path(info["file_path"]).read_text() == text
        reader = await agent.toolkit.get_tool("read_file")
        offset, parts = 0, []
        while offset is not None:
            chunk = await reader.call(
                file_path=info["file_path"],
                start_char=offset,
            )
            value = chunk.content[0].text
            assert len(value.encode()) <= budget
            header, part = value.split("\n", 1)
            parts.append(part)
            next_char = header.split("=", 1)[1].split(";", 1)[0]
            offset = None if next_char == "None" else int(next_char)
        assert "".join(parts) == text
    finally:
        await agent.close()


async def test_cache_reader_rejects_other_runs_and_symlinks(tmp_path):
    private = tmp_path / "private"
    cache = private / "tool_results"
    cache.mkdir(parents=True)
    outside = tmp_path / "other-run.txt"
    outside.write_text("other case")
    link = cache / "link.txt"
    link.symlink_to(outside)
    reader = analyzer.recovery_tools(private, 2000, None)[0]
    for path in (outside, link, "../other-run.txt", "history.db"):
        with pytest.raises(ValueError, match="裁剪缓存"):
            await reader.call(file_path=str(path))


async def test_scroll_eviction_is_recallable_only_in_this_run(
    config,
    episode,
    result,
    tmp_path,
    monkeypatch,
):
    model = first.OfflineToolModel(result)
    model.context_size = 1000
    agent = analyzer.build_analyzer(
        config,
        model,
        "Analyze",
        episode,
        "private",
        [],
        tmp_path / "private",
    )
    other = HistoryStore(tmp_path / "other" / "history.db")
    other.append(
        entry=LogEntry(
            kind="context_msg",
            role="user",
            content="OTHER_RUN_SECRET",
        ),
        session_id="other",
        agent_id=config.id,
    )
    other.close()
    try:
        messages = [
            Msg(name=role, role=role, content=[TextBlock(text=text)])
            for role, text in (
                ("user", "EARLIER_EVIDENCE"),
                ("assistant", "Earlier analysis"),
                ("user", "Current task"),
            )
        ]
        agent.state.context = messages
        monkeypatch.setattr(
            model,
            "count_tokens",
            AsyncMock(
                side_effect=[850, 500],
            ),
        )
        monkeypatch.setattr(
            agent,
            "_split_context_for_compression",
            AsyncMock(
                return_value=(messages[:2], messages[2:]),
            ),
        )
        # The compactor's optional generated checkpoint is independent of
        # durable eviction and is covered by Scroll's own model-loop tests.
        monkeypatch.setattr(
            agent._context_manager,
            "_update_continuation_summary",
            AsyncMock(),
        )
        await agent.compress_context()
        assert agent._context_manager.last_compress["evicted"] == 2
        assert "EARLIER_EVIDENCE" not in str(agent.state.context)
        recall = await agent.toolkit.get_tool("recall_history")
        restored = await recall.call(op="search", query="EARLIER_EVIDENCE")
        assert "EARLIER_EVIDENCE" in restored.content[0].text
        outside = await recall.call(
            op="search",
            query="OTHER_RUN_SECRET",
            all_agents=True,
            session_id="other",
        )
        assert outside.content[0].text.startswith("0 rows")
    finally:
        await agent.close()


async def test_real_loop_prunes_and_closes_on_error(
    config,
    episode,
    result,
    tmp_path,
    monkeypatch,
):
    pruning_config = (
        config.running.light_context_config.tool_result_pruning_config
    )
    pruning_config.pruning_recent_msg_max_bytes = 1200
    episode["reviewed_code"]["diff"] = "long line " * 2000
    agents = []
    build = analyzer.build_analyzer

    def capture(*args):
        agent = build(*args)
        agents.append(agent)
        return agent

    monkeypatch.setattr(analyzer, "build_analyzer", capture)

    class FailingModel(first.OfflineToolModel):
        async def __call__(self, **kwargs):
            if self.calls:
                assert "read_file" in str(kwargs["messages"])
                assert episode["reviewed_code"]["diff"] not in str(kwargs)
                raise TimeoutError("stream stalled")
            return await super().__call__(**kwargs)

    with pytest.raises(TimeoutError, match="stalled"):
        await analyzer.analyze_episode(
            config,
            FailingModel(result),
            "Analyze",
            episode,
            tmp_path / "failure.json",
        )
    assert agents[0]._context_manager._history.closed
    assert (tmp_path / "failure.json").exists()
