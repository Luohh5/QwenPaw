"""One offline interpreter with unique SDK origins for real worker faults."""

import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sysconfig
import tempfile
from types import SimpleNamespace
import venv

from packaging.markers import default_environment
from packaging.requirements import Requirement
import pytest


@pytest.fixture(scope="session")
def fault_runtime():
    root = Path(tempfile.mkdtemp(prefix="qwenpaw-g2-sdk-fault-")).resolve()
    try:
        builder = venv.EnvBuilder(with_pip=False, symlinks=os.name == "posix")
        context = builder.ensure_directories(str(root))
        builder.create(root)
        python = Path(context.env_exec_cmd)
        site = Path(
            sysconfig.get_path(
                "purelib", scheme="venv", vars={"base": str(root)}
            )
        )
        pending = [
            "packaging",
            "jsonschema",
            "pydantic",
            "paho-mqtt",
            "httpx",
            "websockets",
        ]
        copied = {}
        environment = {**default_environment(), "extra": ""}
        while pending:
            distribution = importlib.metadata.distribution(pending.pop())
            name = distribution.metadata["Name"].lower().replace("_", "-")
            if name in copied:
                continue
            copied[name] = distribution.version
            for requirement in distribution.requires or ():
                dependency = Requirement(requirement)
                if dependency.marker is None or dependency.marker.evaluate(
                    environment
                ):
                    pending.append(dependency.name)
            for relative in distribution.files or ():
                if (
                    relative.is_absolute()
                    or ".." in relative.parts
                    or "__pycache__" in relative.parts
                ):
                    continue
                source = Path(distribution.locate_file(relative))
                if source.is_file():
                    target = site / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
        result = subprocess.run(
            [
                str(python),
                "-I",
                "-c",
                "import json,platform,sys; from packaging.tags import sys_tags; print(json.dumps(dict(version=list(sys.version_info[:2]),implementation=sys.implementation.name,cache_tag=sys.implementation.cache_tag,sys_platform=sys.platform,system=platform.system(),machine=platform.machine(),supported_tags=[str(t) for t in sys_tags()])))",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        yield SimpleNamespace(
            python=python,
            site=site,
            identity=json.loads(result.stdout),
            packages=copied,
        )
    finally:
        shutil.rmtree(root)
