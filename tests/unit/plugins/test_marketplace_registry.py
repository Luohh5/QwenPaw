# -*- coding: utf-8 -*-
from __future__ import annotations

import json

import pytest

from qwenpaw.plugins.marketplace_registry import ExternalMarketplaceRegistry


@pytest.mark.asyncio
async def test_marketplace_registry_scrubs_url_credentials_and_is_idempotent(
    tmp_path,
):
    path = tmp_path / "marketplaces.json"
    registry = ExternalMarketplaceRegistry(path)

    first = await registry.register(
        provider="codex",
        source_id="codex:private",
        name="private",
        source="https://user:secret@example.com/plugins.zip?token=secret#x",
        source_type="url",
    )
    second = await registry.register(
        provider="codex",
        source_id="codex:private",
        name="private",
        source="https://user:secret@example.com/plugins.zip?token=secret#x",
        source_type="url",
    )

    assert first == (True, True)
    assert second == (False, True)
    payload = json.loads(path.read_text(encoding="utf-8"))
    record = payload["sources"]["codex:codex:private"]
    assert record["source"] == "https://example.com/plugins.zip"
    assert "secret" not in path.read_text(encoding="utf-8")
