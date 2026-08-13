# -*- coding: utf-8 -*-
"""Safe command-facing wrapper around the existing backup import system."""

from __future__ import annotations

import os
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from ..backup import import_backup
from ..backup._utils.constants import META_FILE
from ..backup.models import BackupMeta, BackupTrustMode
from ..constant import BACKUP_DIR
from ..utils.io_utils import run_sync_io

_MAX_ARCHIVE_BYTES = 4 * 1024 * 1024 * 1024
_MAX_UNCOMPRESSED_BYTES = 16 * 1024 * 1024 * 1024
_MAX_SINGLE_FILE_BYTES = 4 * 1024 * 1024 * 1024
_MAX_ENTRIES = 100_000
_MAX_COMPRESSION_RATIO = 1000


def _validate_member(info: zipfile.ZipInfo) -> None:
    name = info.filename
    if not name or "\x00" in name or "\\" in name:
        raise ValueError(f"Backup contains an unsafe path: {name!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"Backup path escapes the archive root: {name!r}")
    mode = info.external_attr >> 16
    kind = stat.S_IFMT(mode)
    if kind not in (0, stat.S_IFREG, stat.S_IFDIR):
        raise ValueError(
            f"Backup contains an unsupported special file: {name!r}",
        )
    if info.file_size > _MAX_SINGLE_FILE_BYTES:
        raise ValueError(f"Backup entry is too large: {name!r}")
    if (
        info.compress_size > 0
        and info.file_size > 1024 * 1024
        and info.file_size / info.compress_size > _MAX_COMPRESSION_RATIO
    ):
        raise ValueError(
            f"Backup entry has an unsafe compression ratio: {name!r}",
        )


def inspect_backup_archive(
    path: Path,
    *,
    require_zip_suffix: bool = True,
) -> BackupMeta:
    """Validate archive structure and return its QwenPaw metadata.

    This supplements the existing signature/version validation with resource
    bounds before an explicitly supplied local file enters the backup store.
    """
    source = path.expanduser().resolve(strict=True)
    if not source.is_file():
        raise ValueError(f"Import source is not a regular file: {source}")
    if require_zip_suffix and source.suffix.lower() != ".zip":
        raise ValueError("Only QwenPaw backup .zip files can be imported.")
    if source.stat().st_size > _MAX_ARCHIVE_BYTES:
        raise ValueError("Backup archive exceeds the 4 GiB safety limit.")
    if not zipfile.is_zipfile(source):
        raise ValueError("Import source is not a valid ZIP archive.")

    total_size = 0
    names: set[str] = set()
    with zipfile.ZipFile(source, "r") as archive:
        infos = archive.infolist()
        if len(infos) > _MAX_ENTRIES:
            raise ValueError("Backup archive contains too many entries.")
        for info in infos:
            _validate_member(info)
            if info.filename in names:
                raise ValueError(
                    f"Backup contains a duplicate path: {info.filename!r}",
                )
            names.add(info.filename)
            total_size += info.file_size
            if total_size > _MAX_UNCOMPRESSED_BYTES:
                raise ValueError(
                    "Backup uncompressed size exceeds the 16 GiB limit.",
                )
        if META_FILE not in names:
            raise ValueError("ZIP does not contain QwenPaw meta.json.")
        try:
            return BackupMeta.model_validate_json(archive.read(META_FILE))
        except Exception as exc:
            raise ValueError("Backup meta.json is invalid.") from exc


def _copy_to_staging(source: Path) -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(
        dir=BACKUP_DIR,
        suffix=".import_tmp",
    )
    staged = Path(name)
    try:
        with source.open("rb") as src, os.fdopen(fd, "wb") as dst:
            total = 0
            while chunk := src.read(1024 * 1024):
                total += len(chunk)
                if total > _MAX_ARCHIVE_BYTES:
                    raise ValueError(
                        "Backup archive exceeds the 4 GiB safety limit.",
                    )
                dst.write(chunk)
            dst.flush()
            os.fsync(dst.fileno())
        staged.chmod(0o600)
        return staged
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        staged.unlink(missing_ok=True)
        raise


async def import_backup_path(
    path: Path,
    *,
    overwrite: bool = False,
    trust_mode: BackupTrustMode | None = None,
) -> BackupMeta:
    """Copy and import a ZIP without ever moving the user's source file."""
    source = path.expanduser().resolve(strict=True)
    if not source.is_file():
        raise ValueError(f"Import source is not a regular file: {source}")
    if source.suffix.lower() != ".zip":
        raise ValueError("Only QwenPaw backup .zip files can be imported.")
    if source.stat().st_size > _MAX_ARCHIVE_BYTES:
        raise ValueError("Backup archive exceeds the 4 GiB safety limit.")
    staged = await run_sync_io(_copy_to_staging, source)
    try:
        await run_sync_io(
            inspect_backup_archive,
            staged,
            require_zip_suffix=False,
        )
        return await import_backup(
            staged,
            overwrite=overwrite,
            trust_mode=trust_mode,
        )
    finally:
        staged.unlink(missing_ok=True)


__all__ = ["import_backup_path", "inspect_backup_archive"]
