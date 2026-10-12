# -*- coding: utf-8 -*-
# pylint: disable=unused-argument
"""Load real plugin modules without the repository's global SDK mocks."""

import importlib
import importlib.abc
import importlib.util
import os
from pathlib import Path
import sys

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[3] / "plugins/channel/feishu"
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


@pytest.fixture
def load_plugin():
    """Mirror the package namespaces used for normal and probe imports."""
    namespaces = {}
    archive_root = os.environ.get("QWENPAW_CHANNEL_PLUGIN_ROOT")
    plugin_root = (
        Path(archive_root).resolve() if archive_root else PLUGIN_DIR.parent
    )

    def load(namespace="plugin_feishu", channel="feishu"):
        plugin_dir = plugin_root / channel
        namespaces[namespace] = plugin_dir
        spec = importlib.util.spec_from_file_location(
            namespace,
            plugin_dir / "__init__.py",
            submodule_search_locations=[str(plugin_dir)],
        )
        package = importlib.util.module_from_spec(spec)
        sys.modules[namespace] = package
        spec.loader.exec_module(package)
        entry = importlib.import_module(f"{namespace}.plugin")
        if archive_root:
            assert Path(entry.__file__).resolve() == plugin_dir / "plugin.py"
        return entry

    yield load
    for namespace, plugin_dir in namespaces.items():
        for name in list(sys.modules):
            if name == namespace or name.startswith(f"{namespace}."):
                module = sys.modules.pop(name)
                if archive_root and getattr(module, "__file__", None):
                    assert (
                        Path(module.__file__)
                        .resolve()
                        .is_relative_to(
                            plugin_dir,
                        )
                    ), f"Archive test loaded source module: {name}"
