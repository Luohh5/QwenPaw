# -*- coding: utf-8 -*-
"""Small normalization helpers shared by Migration Providers."""

from __future__ import annotations

from datetime import datetime
from typing import Any


def parse_datetime(value: Any) -> datetime | None:
    """Parse common provider timestamp representations, best effort."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value).astimezone()
        except (OSError, OverflowError, ValueError):
            return None
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


__all__ = ["parse_datetime"]
