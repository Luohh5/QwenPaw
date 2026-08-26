# -*- coding: utf-8 -*-
"""Small, shared factories for Pawport unit tests."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path

import pytest

from qwenpaw.portability.models import ProviderInventory


class _InventoryProvider:
    def __init__(self, inventory: ProviderInventory) -> None:
        self._inventory = inventory
        self.provider_id = inventory.provider_id

    async def inventory(
        self,
        *,
        limit: int,
        progress=None,
    ) -> ProviderInventory:
        assert limit >= 1
        if progress is not None:
            await progress("provider inventory")
        return self._inventory


@pytest.fixture
def bind_import_inventory(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[ProviderInventory], None]:
    """Bind one inventory to the importer without repeating provider mocks."""

    def bind(inventory: ProviderInventory) -> None:
        monkeypatch.setattr(
            "qwenpaw.portability.importer.create_migration_provider",
            lambda _source, _workspace: _InventoryProvider(inventory),
        )

    return bind


@pytest.fixture
def write_tree() -> Callable[[Path, Mapping[str, str]], None]:
    """Write a compact text fixture tree rooted at ``root``."""

    def write(root: Path, files: Mapping[str, str]) -> None:
        for relative, content in files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

    return write
