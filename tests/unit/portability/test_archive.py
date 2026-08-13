# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import stat
import zipfile
from pathlib import Path

import pytest

from qwenpaw.backup._ops import storage
from qwenpaw.backup._utils import constants
from qwenpaw.backup._utils.signing import key as signing_key
from qwenpaw.backup.models import BackupMeta
from qwenpaw.portability import archive


def _patch_backup_dir(monkeypatch, path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(archive, "BACKUP_DIR", path)
    monkeypatch.setattr(storage, "BACKUP_DIR", path)
    monkeypatch.setattr(constants, "BACKUP_DIR", path)
    monkeypatch.setattr(signing_key, "BACKUP_DIR", path)
    monkeypatch.setattr(signing_key, "_cached_key", None)
    monkeypatch.setattr(signing_key, "_cached_mtime_ns", None)


def _backup(path: Path, *, backup_id: str = "portable-test") -> BackupMeta:
    meta = BackupMeta(id=backup_id, name="Portable test")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("meta.json", meta.model_dump_json())
        zf.writestr("data/config.json", json.dumps({"agents": {}}))
    return meta


def test_inspect_rejects_path_traversal(tmp_path: Path) -> None:
    path = tmp_path / "bad.zip"
    meta = BackupMeta(id="bad", name="Bad")
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("meta.json", meta.model_dump_json())
        zf.writestr("../outside", "bad")

    with pytest.raises(ValueError, match="escapes"):
        archive.inspect_backup_archive(path)


def test_inspect_rejects_duplicate_members(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.zip"
    meta = BackupMeta(id="duplicate", name="Duplicate")
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("meta.json", meta.model_dump_json())
        with pytest.warns(UserWarning, match="Duplicate name"):
            zf.writestr("meta.json", meta.model_dump_json())

    with pytest.raises(ValueError, match="duplicate"):
        archive.inspect_backup_archive(path)


def test_inspect_rejects_symbolic_link_members(tmp_path: Path) -> None:
    path = tmp_path / "symlink.zip"
    meta = BackupMeta(id="symlink", name="Symlink")
    link = zipfile.ZipInfo("data/link")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("meta.json", meta.model_dump_json())
        zf.writestr(link, "../../outside")

    with pytest.raises(ValueError, match="special file"):
        archive.inspect_backup_archive(path)


@pytest.mark.asyncio
async def test_import_copies_source_before_existing_backup_import(
    tmp_path: Path,
    monkeypatch,
) -> None:
    backup_dir = tmp_path / "backups"
    _patch_backup_dir(monkeypatch, backup_dir)
    source = tmp_path / "user-owned.zip"
    _backup(source)
    before = source.read_bytes()

    result = await archive.import_backup_path(source, trust_mode="legacy")

    assert result.id == "portable-test"
    assert source.read_bytes() == before
    assert (backup_dir / "portable-test.zip").is_file()
    assert not list(backup_dir.glob("*.import_tmp"))


@pytest.mark.asyncio
async def test_invalid_import_cleans_staging_and_preserves_source(
    tmp_path: Path,
    monkeypatch,
) -> None:
    backup_dir = tmp_path / "backups"
    _patch_backup_dir(monkeypatch, backup_dir)
    source = tmp_path / "invalid.zip"
    source.write_bytes(b"not a zip")

    with pytest.raises(ValueError, match="valid ZIP"):
        await archive.import_backup_path(source)

    assert source.read_bytes() == b"not a zip"
    assert not list(backup_dir.glob("*.import_tmp"))
