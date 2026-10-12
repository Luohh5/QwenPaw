# -*- coding: utf-8 -*-
"""Guard real public imports before the isolated test suite is collected."""

import importlib.abc
import sys

import pytest

BLOCKED = (
    "qwenpaw.app",
    "qwenpaw.channel_protocol",
    "qwenpaw.plugins.loader",
    "qwenpaw.plugins.api",
    "qwenpaw.plugins.registry",
    "lark_oapi",
    "dingtalk_stream",
    "alibabacloud_dingtalk",
)
ATTEMPTS = []


def blocked(name):
    return any(
        name == prefix or name.startswith(prefix + ".") for prefix in BLOCKED
    )


class ImportGuard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, _path=None, _target=None):
        if blocked(fullname):
            ATTEMPTS.append(fullname)
            raise AssertionError(f"Generic execution imported {fullname}")


assert not any(blocked(name) for name in sys.modules)
sys.meta_path.insert(0, ImportGuard())


@pytest.fixture(autouse=True)
def verify_import_boundary():
    yield
    assert not ATTEMPTS, f"Caught forbidden imports: {ATTEMPTS}"
