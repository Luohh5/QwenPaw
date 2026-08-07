# -*- coding: utf-8 -*-
"""Mail F1 exploration mode activation tool."""
from __future__ import annotations

import logging

from agentscope.message import TextBlock
from agentscope.message import ToolResultState
from agentscope.tool import ToolChunk

from ...config.context import (
    activate_f1_for_session,
    get_current_session_id,
)
from ...runtime.tool_registry import tool_descriptor

logger = logging.getLogger(__name__)


@tool_descriptor(
    async_execution=True,
    tool_type="internal",
    policy_name="ActivateF1ExplorationMode",
    ui_description=(
        "Activate F1 exploration mode for step-by-step mail approval"
    ),
    ui_icon="🔍",
)
async def activate_f1_exploration_mode() -> ToolChunk:
    """Activate F1 exploration mode. Call this when an email cannot be
    classified by the triage tree (MAIL_TRIAGE.md) and you need to
    attempt handling it with per-tool user approval.

    After activation, the SYSTEM automatically intercepts every tool
    call (mail read/write, file ops, browser use, shell, etc.) and asks
    the user for approval before execution, for the remainder of this
    request.

    IMPORTANT: Do NOT ask the user for approval yourself in your chat
    output. Just call the tools you need as usual; approval is handled
    automatically by the system. If the user approves, the tool returns
    its normal result; if the user denies, the tool returns a denial
    message and you should retry with a different approach.

    Returns:
        `ToolChunk`: Confirmation that F1 mode is now active.
    """
    # The session_id ContextVar is set in PRE_DISPATCH (before the tool
    # coordinator spawns per-tool tasks), so it is readable here even
    # though this coroutine runs in its own asyncio task.
    session_id = get_current_session_id()
    if not session_id:
        logger.warning(
            "activate_f1_exploration_mode: no session_id in context; "
            "F1 mode NOT activated.",
        )
        return ToolChunk(
            is_last=True,
            state=ToolResultState.SUCCESS,
            content=[
                TextBlock(
                    type="text",
                    text=(
                        "F1 探索模式激活失败：当前请求缺少 session_id，"
                        "无法登记逐步审批状态。请按最严格标准自行处理"
                        "（不确定的操作一律不要执行）。"
                    ),
                ),
            ],
        )
    activate_f1_for_session(session_id)
    return ToolChunk(
        is_last=True,
        state=ToolResultState.SUCCESS,
        content=[
            TextBlock(
                type="text",
                text=(
                    "F1 探索模式已激活。系统将自动拦截你后续的每个工具调用"
                    "（邮件读写/文件操作/浏览器等）并向用户请求审批。"
                    "重要：你无需也不应在对话中自行询问用户是否批准——"
                    "请直接正常调用所需工具，审批由系统自动处理："
                    "用户同意则工具正常返回结果；用户拒绝则工具返回拒绝信息，"
                    "此时你应换一种思路重试。"
                ),
            ),
        ],
    )
