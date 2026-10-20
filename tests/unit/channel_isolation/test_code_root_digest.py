# -*- coding: utf-8 -*-
"""Source integrity remains independent of host metadata and Python caches."""

import pytest

from qwenpaw.channel_protocol.artifacts import code_root_digest
from qwenpaw.channel_protocol.errors import ArtifactValidationError


def test_digest_ignores_only_requested_metadata_and_python_caches(tmp_path):
    (tmp_path / "driver.py").write_text("VALUE = 1\n")
    initial = code_root_digest(tmp_path)
    (tmp_path / ".qwenpaw-pawport.json").write_text('{"captured": true}')
    (tmp_path / "driver.pyc").write_bytes(b"compiled")
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    (cache / "driver.cpython-313.pyc").write_bytes(b"cached")

    assert (
        code_root_digest(
            tmp_path,
            ignored_paths=(".qwenpaw-pawport.json",),
        )
        == initial
    )
    assert code_root_digest(tmp_path) != initial
    (tmp_path / "driver.py").write_text("VALUE = 2\n")
    assert (
        code_root_digest(
            tmp_path,
            ignored_paths=(".qwenpaw-pawport.json",),
        )
        != initial
    )


@pytest.mark.parametrize("kind", ["file", "directory", "dangling"])
def test_digest_rejects_symlinks_even_when_path_is_ignored(tmp_path, kind):
    root = tmp_path / "code"
    root.mkdir()
    target = tmp_path / "target"
    if kind == "directory":
        target.mkdir()
    elif kind == "file":
        target.write_text("outside")
    (root / "link").symlink_to(target, target_is_directory=kind == "directory")

    with pytest.raises(ArtifactValidationError, match="symlinks"):
        code_root_digest(root, ignored_paths=("link",))
