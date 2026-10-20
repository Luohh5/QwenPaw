# -*- coding: utf-8 -*-
"""Real temporary handles must close before ffmpeg and Windows deletion."""

import errno
import importlib.util
import os
from pathlib import Path
import subprocess

import pytest


@pytest.mark.parametrize("ffmpeg_fails", [False, True])
def test_signalstats_closes_temporary_handle_and_removes_file(
    tmp_path,
    monkeypatch,
    ffmpeg_fails,
):
    source = (
        Path(__file__).resolve().parents[2]
        / "vendor/media_toolkit/frame_stats.py"
    )
    spec = importlib.util.spec_from_file_location(
        "frame_stats_cleanup",
        source,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    real_mkstemp, real_unlink = module.tempfile.mkstemp, Path.unlink
    created, open_at_run = [], []

    def closed(descriptor):
        try:
            os.fstat(descriptor)
        except OSError as error:
            assert error.errno == errno.EBADF
            return True
        return False

    def mkstemp(*args, **kwargs):
        descriptor, name = real_mkstemp(*args, dir=tmp_path, **kwargs)
        created.append((descriptor, Path(name)))
        return descriptor, name

    def ffmpeg(*_args, **_kwargs):
        descriptor, path = created[0]
        open_at_run.append(not closed(descriptor))
        if ffmpeg_fails:
            raise subprocess.CalledProcessError(1, "ffmpeg")
        path.write_text(
            "lavfi.signalstats.YAVG=128\n"
            "lavfi.signalstats.YMIN=0\n"
            "lavfi.signalstats.YMAX=255\n"
            "lavfi.signalstats.SATAVG=64\n",
        )

    def windows_unlink(path, *args, **kwargs):
        if created and path == created[0][1] and not closed(created[0][0]):
            error = PermissionError(errno.EACCES, "file still open")
            error.winerror = 32
            raise error
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(module.tempfile, "mkstemp", mkstemp)
    monkeypatch.setattr(module.subprocess, "run", ffmpeg)
    monkeypatch.setattr(Path, "unlink", windows_unlink)
    try:
        if ffmpeg_fails:
            with pytest.raises(
                module.FrameStatsError,
                match="sampling failed",
            ):
                module.image_stats(tmp_path / "image.png")
        else:
            assert module.image_stats(tmp_path / "image.png") == {
                "y_mean": 128 / 255,
                "y_range": 1.0,
                "sat_mean": 64 / 255,
            }
        assert open_at_run == [False]
        assert len(created) == 1
        descriptor, path = created[0]
        assert closed(descriptor)
        assert not path.exists()
    finally:
        for descriptor, path in created:
            if not closed(descriptor):
                os.close(descriptor)
            real_unlink(path, missing_ok=True)
