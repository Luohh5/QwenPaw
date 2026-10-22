# -*- coding: utf-8 -*-
"""Signalstats metadata must avoid temporary handles and Windows paths."""

import importlib.util
from pathlib import Path
import subprocess
import tempfile

import pytest


@pytest.mark.parametrize("ffmpeg_fails", [False, True])
def test_signalstats_reads_stdout_without_temporary_files(
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
    calls = []
    failure = subprocess.CalledProcessError(1, "ffmpeg")

    def temporary_file(*_args, **_kwargs):
        pytest.fail("signalstats must not allocate temporary files or handles")

    def ffmpeg(command, **kwargs):
        calls.append((command, kwargs))
        if ffmpeg_fails:
            raise failure
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=(
                b"lavfi.signalstats.YAVG=128\n"
                b"lavfi.signalstats.YMIN=0\n"
                b"lavfi.signalstats.YMAX=255\n"
                b"lavfi.signalstats.SATAVG=64\n"
            ),
        )

    for name in ("mkstemp", "NamedTemporaryFile", "TemporaryFile"):
        monkeypatch.setattr(tempfile, name, temporary_file)
    monkeypatch.setattr(module.subprocess, "run", ffmpeg)
    image = tmp_path / "image.png"
    if ffmpeg_fails:
        with pytest.raises(
            module.FrameStatsError,
            match="sampling failed",
        ) as caught:
            module.image_stats(image)
        assert caught.value.__cause__ is failure
    else:
        assert module.image_stats(image) == {
            "y_mean": 128 / 255,
            "y_range": 1.0,
            "sat_mean": 64 / 255,
        }
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command[command.index("-vf") + 1] == (
        "scale=iw:ih,signalstats,metadata=print:file=-"
    )
    assert command[command.index("-i") + 1] == str(image)
    assert kwargs["check"] is True
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["stdout"] == subprocess.PIPE
    assert kwargs["stderr"] == subprocess.DEVNULL
    assert kwargs["timeout"] > 0
    assert list(tmp_path.iterdir()) == []
