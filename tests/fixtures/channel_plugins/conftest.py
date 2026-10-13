# -*- coding: utf-8 -*-
# pylint: disable=unused-argument
"""Load real plugin modules without the repository's global SDK mocks."""

import importlib.abc
import os
import sys

import pytest

BUILTIN_PREFIXES = (
    "qwenpaw.app.channels.feishu",
    "qwenpaw.app.channels.dingtalk",
)


def is_builtin_platform(name):
    """Match implementation modules and their children, not host utilities."""
    return any(
        name == prefix or name.startswith(f"{prefix}.")
        for prefix in BUILTIN_PREFIXES
    )


class NoBuiltinImports(importlib.abc.MetaPathFinder):
    """Block legacy implementations even during runtime suite collection."""

    def find_spec(self, fullname, path=None, target=None):
        if is_builtin_platform(fullname):
            raise AssertionError(f"Unexpected builtin import: {fullname}")


if os.environ.get("QWENPAW_CHANNEL_BLOCK_BUILTINS") == "1":
    assert not any(is_builtin_platform(name) for name in sys.modules)
    sys.meta_path.insert(0, NoBuiltinImports())


class NoPlatformImports(importlib.abc.MetaPathFinder):
    """Fail even when application code catches a missing SDK import."""

    def find_spec(self, fullname, path=None, target=None):
        if fullname == "lark_oapi" or fullname.startswith(
            (
                "lark_oapi.",
                "qwenpaw.app.channels.feishu",
                "dingtalk_stream",
                "alibabacloud_",
                "Tea.",
                "qwenpaw.app.channels.dingtalk",
            ),
        ):
            raise AssertionError(f"Unexpected platform import: {fullname}")


@pytest.fixture(autouse=True)
def no_platform_imports(monkeypatch, request):
    """A fresh process prevents cached SDKs from hiding imports."""
    if request.module.__name__.endswith(
        ("feishu_suite", "lifecycle_suite", "dingtalk_suite"),
    ):
        return
    assert "lark_oapi" not in sys.modules
    monkeypatch.setattr(
        sys,
        "meta_path",
        [NoPlatformImports(), *sys.meta_path],
    )
