# -*- coding: utf-8 -*-
"""Base classes for control command handlers.

Control commands are high-priority commands like /stop that require
immediate response and special handling outside the normal agent flow.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict

if TYPE_CHECKING:
    from ....app.channels.base import BaseChannel
    from ....app.workspace import Workspace


@dataclass
class ControlContext:
    """Context for control command execution.

    Attributes:
        workspace: Current workspace instance (for task_tracker, etc.)
        payload: Original message payload (native dict or AgentRequest)
        channel: Channel instance
        session_id: Normalized session ID (e.g. "console:user1")
        user_id: User ID from request
        agent_id: Agent ID for permission checks
        args: Parsed command arguments (command-specific)
        progress_reporter: Optional long-running command progress callback
    """

    workspace: "Workspace"
    payload: Any
    channel: "BaseChannel | None"
    session_id: str
    user_id: str
    agent_id: str
    args: Dict[str, Any]
    progress_reporter: Callable[[str], Awaitable[None]] | None = None

    async def report_progress(self, message: str) -> None:
        """Emit best-effort progress when the transport supports it."""
        text = str(message or "").strip()
        if self.progress_reporter is not None and text:
            await self.progress_reporter(text)


class BaseControlCommandHandler(ABC):
    """Abstract base class for control command handlers.

    Subclasses implement specific commands (e.g. /stop, /pause).

    Example:
        class StopCommandHandler(BaseControlCommandHandler):
            command_name = "/stop"
            description = "Stop the current task"

            async def handle(self, context: ControlContext) -> str:
                # Implementation
                return "Task stopped"
    """

    command_name: str = ""
    # Human-readable summary, used when advertising commands to clients
    # (e.g. the ACP ``available_commands_update`` notification).
    description: str = ""

    @abstractmethod
    async def handle(self, context: ControlContext) -> str:
        """Handle the control command.

        Args:
            context: Control command context

        Returns:
            Response text to send to user

        Raises:
            Exception: If command execution fails
        """
        raise NotImplementedError
