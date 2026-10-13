# -*- coding: utf-8 -*-
"""Setuptools hooks for the dormant official channel resources."""

from pathlib import Path
import runpy

from setuptools import setup
from setuptools.command.build_py import build_py
from setuptools.command.sdist import sdist


def bundle(destination, version):
    root = Path(__file__).resolve().parent
    builder = runpy.run_path(
        str(root / "scripts/pack/generate_plugin_metadata.py"),
    )["build_official_channel_bundle"]
    builder(root, Path(destination), host_version=version)


class BuildPy(build_py):
    """Generate into build output, never into the checkout."""

    def run(self):
        super().run()
        bundle(
            Path(self.build_lib) / "qwenpaw/official_channels",
            self.distribution.get_version(),
        )

    def get_outputs(self, include_bytecode=1):
        outputs = super().get_outputs(include_bytecode)
        directory = Path(self.build_lib) / "qwenpaw/official_channels"
        return sorted(
            set(outputs) | {str(path) for path in directory.glob("*")},
        )


class SDist(sdist):
    """Ship both private resources and their reproducible source inputs."""

    def make_release_tree(self, base_dir, files):
        super().make_release_tree(base_dir, files)
        bundle(
            Path(base_dir) / "src/qwenpaw/official_channels",
            self.distribution.get_version(),
        )


setup(cmdclass={"build_py": BuildPy, "sdist": SDist})
