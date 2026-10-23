# -*- coding: utf-8 -*-
from setuptools import find_namespace_packages, setup

packages = find_namespace_packages(
    "src",
    exclude=["qwenpaw.harnesses", "qwenpaw.harnesses.*"],
)
for package, directory in (
    ("qwenpawmail_mcp", "packages/qwenpawmail-mcp/src/qwenpawmail_mcp"),
    ("qwenpaw.plugins.bundled.hub", "plugins/hub"),
):
    packages.append(package)
    packages.extend(
        f"{package}.{name}" for name in find_namespace_packages(directory)
    )

setup(packages=packages)
