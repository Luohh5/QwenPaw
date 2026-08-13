# -*- coding: utf-8 -*-
"""Persistent provenance registry for externally imported Marketplaces.

QwenPaw's public Market providers are fixed search adapters.  This registry is
deliberately separate: it records third-party plugin sources restored by
``/import`` so native installation can be attempted without copying another
harness's installed cache.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from ..config.utils import get_plugins_dir
from ..utils.io_utils import (
    get_path_lock,
    read_json_async,
    write_json_atomic_async,
)


def _credential_free_source(source: str) -> tuple[str, bool]:
    """Remove URL credentials/query/fragment before persisting a source."""
    value = str(source or "").strip()
    if not value.startswith(("http://", "https://")):
        return value, False
    parsed = urlsplit(value)
    host = parsed.hostname or ""
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    cleaned = urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    changed = cleaned != value
    return cleaned, changed


class ExternalMarketplaceRegistry:
    """Small JSON registry of provider-owned Marketplace declarations."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (get_plugins_dir() / "marketplaces.json")

    async def read(self) -> dict[str, Any]:
        """Return a normalized registry, tolerating an absent old file."""
        if not self.path.is_file():
            return {"schema_version": "1", "sources": {}}
        try:
            value = await read_json_async(self.path)
        except (OSError, ValueError, TypeError):
            return {"schema_version": "1", "sources": {}}
        if not isinstance(value, dict):
            return {"schema_version": "1", "sources": {}}
        sources = value.get("sources")
        if not isinstance(sources, dict):
            sources = {}
        return {"schema_version": "1", "sources": sources}

    # pylint: disable-next=too-many-arguments
    async def register(
        self,
        *,
        provider: str,
        source_id: str,
        name: str,
        source: str,
        source_type: str,
        ref_name: str = "",
    ) -> tuple[bool, bool]:
        """Add/update a source; return ``(changed, credentials_removed)``."""
        async with get_path_lock(self.path):
            payload = await self.read()
            cleaned_source, credentials_removed = _credential_free_source(
                source,
            )
            key = f"{provider}:{source_id}"
            record = {
                "provider": provider,
                "source_id": source_id,
                "name": name,
                "source": cleaned_source,
                "source_type": source_type,
                "ref_name": ref_name,
                "status": (
                    "available" if cleaned_source else "source_unavailable"
                ),
            }
            if payload["sources"].get(key) == record:
                return False, credentials_removed
            payload["sources"][key] = record
            self.path.parent.mkdir(parents=True, exist_ok=True)
            await write_json_atomic_async(
                self.path,
                payload,
                sort_keys=True,
                new_file_mode=0o600,
            )
            return True, credentials_removed


__all__ = ["ExternalMarketplaceRegistry"]
