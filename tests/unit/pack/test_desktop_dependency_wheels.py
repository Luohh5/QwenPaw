# -*- coding: utf-8 -*-
"""Exercise release dependency commands with a real, offline pip resolver."""

import base64
import csv
import hashlib
import importlib.metadata
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tarfile
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "scripts/pack-tauri"
BACKEND = r"""
import json
from pathlib import Path
import zipfile

def build_wheel(
    wheel_directory, config_settings=None, metadata_directory=None,
):
    data = json.loads(Path("package.json").read_text())
    name, version = data["name"].replace("-", "_"), data["version"]
    filename = f"{name}-{version}-py3-none-any.whl"
    info = f"{name}-{version}.dist-info"
    fields = ["Metadata-Version: 2.1", f"Name: {name}",
              f"Version: {version}", "Requires-Python: >=3.10"]
    fields += [f"Provides-Extra: {extra}" for extra in data.get("extras", [])]
    fields += [f"Requires-Dist: {req}" for req in data.get("requires", [])]
    files = {f"{name}/__init__.py": f"VERSION = {version!r}\n",
             f"{info}/METADATA": "\n".join(fields) + "\n",
             f"{info}/WHEEL": "Wheel-Version: 1.0\n"
             "Generator: desktop-regression\nRoot-Is-Purelib: true\n"
             "Tag: py3-none-any\n"}
    files[f"{info}/RECORD"] = "".join(f"{path},,\n" for path in files)
    files[f"{info}/RECORD"] += f"{info}/RECORD,,\n"
    with zipfile.ZipFile(Path(wheel_directory) / filename, "w") as archive:
        for path, content in files.items():
            archive.writestr(path, content)
    return filename

build_editable = build_wheel
"""


def _run(command, *, cwd, env, success=True):
    result = subprocess.run(
        [str(value) for value in command],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert (result.returncode == 0) is success, result.stdout + result.stderr
    return result


def _project(path, name, requires=(), extras=()):
    path.mkdir(parents=True)
    (path / "backend.py").write_text(BACKEND)
    (path / "package.json").write_text(
        json.dumps(
            {
                "name": name,
                "version": "1.0.0",
                "requires": requires,
                "extras": extras,
            },
        ),
    )
    (path / "pyproject.toml").write_text(
        '[build-system]\nrequires=[]\nbuild-backend="backend"\n'
        'backend-path=["."]\n',
    )
    return path


def _wheel(project, destination):
    namespace = {}
    exec(BACKEND, namespace)  # pylint: disable=exec-used
    previous = Path.cwd()
    try:
        os.chdir(project)
        return destination / namespace["build_wheel"](str(destination))
    finally:
        os.chdir(previous)


def _section(script, start, end):
    return script.split(start, 1)[1].partition("\n")[2].split(end, 1)[0]


@pytest.fixture
def runtime_stager():
    spec = importlib.util.spec_from_file_location(
        "desktop_channel_runtime_stager",
        SCRIPTS / "stage_channel_runtime.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def staged_runtime_inputs(tmp_path, monkeypatch, runtime_stager):
    from qwenpaw.__version__ import __version__

    root = tmp_path / "resources"
    executable = root / "python/bin/python3.11"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"python runtime")
    entry = executable.with_name("python3")
    try:
        entry.symlink_to(executable.name)
    except OSError as exc:
        if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
            pytest.skip("Creating symlinks requires Windows privilege")
        raise
    wheel = tmp_path / f"qwenpaw-{__version__}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            f"qwenpaw-{__version__}.dist-info/METADATA",
            f"Name: qwenpaw\nVersion: {__version__}\n",
        )
        archive.writestr("qwenpaw/official_channels/index.json", "{}")
    identity = {
        "version": [3, 11],
        "implementation": "cpython",
        "cache_tag": "cpython-311",
        "sys_platform": "darwin",
        "system": "Darwin",
        "machine": "arm64",
        "supported_tags": ["py3-none-any"],
    }
    monkeypatch.setattr(
        runtime_stager,
        "_snapshot",
        lambda *args: {"distributions": [], "python": identity},
    )
    monkeypatch.setattr(
        runtime_stager,
        "_validate_closure",
        lambda *args: None,
    )
    monkeypatch.setattr(
        runtime_stager,
        "_validate_official_resources",
        lambda *args: None,
    )
    return root, entry, wheel


def test_runtime_entry_survives_symlink_materialization(
    tmp_path,
    monkeypatch,
    runtime_stager,
    staged_runtime_inputs,
):
    from qwenpaw.plugins.channel_bootstrap import runtime_manifest

    root, entry, wheel = staged_runtime_inputs
    manifest = runtime_stager.stage_channel_runtime(
        python=entry,
        wheel=wheel,
        resource_dir=root,
    )
    assert json.loads(manifest.read_text())["python_executable"] == (
        "python/bin/python3"
    )
    installed = shutil.copytree(root, tmp_path / "installed", symlinks=False)
    entry = installed / "python/bin/python3"
    support = installed / "binaries/channel-runtime/site"
    assert entry.is_file() and not entry.is_symlink()
    assert entry.resolve() != entry.with_name("python3.11").resolve()
    for name, value in {
        "QWENPAW_TAURI_RESOURCE_DIR": str(installed),
        "QWENPAW_CHANNEL_RUNTIME_MANIFEST": str(
            installed / manifest.relative_to(root),
        ),
        "QWENPAW_DESKTOP_PY_RUNTIME": str(entry),
        "QWENPAW_CHANNEL_SUPPORT_ROOT": str(support),
        "QWENPAW_CHANNEL_DEPENDENCY_PATHS": json.dumps([str(support)]),
        "QWENPAW_CHANNEL_PRESET": "light",
    }.items():
        monkeypatch.setenv(name, value)
    executable, actual_support, sites, _, preset = runtime_manifest()
    assert executable == entry.resolve()
    assert actual_support == support.resolve()
    assert sites == (support.resolve(),)
    assert preset == "light"


def test_runtime_entry_rejects_symlink_outside_resources(
    tmp_path,
    runtime_stager,
    staged_runtime_inputs,
):
    root, entry, wheel = staged_runtime_inputs
    outside = tmp_path / "outside-python"
    outside.write_bytes(b"outside runtime")
    entry.unlink()
    entry.symlink_to(outside)
    with pytest.raises(ValueError):
        runtime_stager.stage_channel_runtime(
            python=entry,
            wheel=wheel,
            resource_dir=root,
        )
    assert not (root / "binaries/channel-runtime").exists()


def test_wheel_metadata_ignores_vendored_distributions(
    tmp_path,
    runtime_stager,
):
    wheel = tmp_path / "setuptools-81.0.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            "setuptools-81.0.0.dist-info/METADATA",
            "Name: setuptools\nVersion: 81.0.0\n",
        )
        archive.writestr(
            "setuptools/_vendor/packaging-24.2.dist-info/METADATA",
            "Name: packaging\nVersion: 24.2\n",
        )
    assert runtime_stager._wheel_metadata(wheel) == ("setuptools", "81.0.0")


def test_wheel_metadata_rejects_multiple_root_distributions(
    tmp_path,
    runtime_stager,
):
    wheel = tmp_path / "invalid-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name in ("first", "second"):
            archive.writestr(
                f"{name}-1.0.dist-info/METADATA",
                f"Name: {name}\nVersion: 1.0\n",
            )
    with pytest.raises(ValueError, match="Wheel must have one METADATA"):
        runtime_stager._wheel_metadata(wheel)


@pytest.mark.parametrize("library", ["purelib", "platlib"])
def test_extract_wheel_relocates_libraries_and_record(
    tmp_path,
    runtime_stager,
    library,
):
    wheel = tmp_path / "relocation_demo-1.0-py3-none-any.whl"
    info = "relocation_demo-1.0.dist-info"
    source = f"relocation_demo-1.0.data/{library}/relocation_demo/native.py"
    relocated = "relocation_demo/native.py"
    files = {
        "relocation_demo/__init__.py": b"ROOT = True\n",
        source: b"NATIVE = True\n",
        f"{info}/METADATA": b"Name: relocation-demo\nVersion: 1.0\n",
    }
    rows = [
        [
            name,
            "sha256="
            + base64.urlsafe_b64encode(hashlib.sha256(data).digest())
            .rstrip(b"=")
            .decode(),
            str(len(data)),
        ]
        for name, data in files.items()
    ]
    record = f"{info}/RECORD"
    rows.append([record, "", ""])
    stream = io.StringIO(newline="")
    csv.writer(stream).writerows(rows)
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
        archive.writestr(record, stream.getvalue())

    site = tmp_path / "site"
    runtime_stager._extract_wheel(wheel, site)

    expected = [
        [relocated if name == source else name, digest, size]
        for name, digest, size in rows
    ]
    with (site / record).open(newline="") as installed_record:
        assert list(csv.reader(installed_record)) == expected
    distribution = next(importlib.metadata.distributions(path=[str(site)]))
    assert distribution.metadata["Name"] == "relocation-demo"
    assert {str(path) for path in distribution.files} == {
        row[0] for row in expected
    }
    for name, data in files.items():
        destination = relocated if name == source else name
        assert distribution.locate_file(destination).read_bytes() == data
    assert not (site / "relocation_demo-1.0.data").exists()


def test_extract_wheel_relocated_library_cannot_replace_existing_file(
    tmp_path,
    runtime_stager,
):
    wheel = tmp_path / "example-1.0-py3-none-any.whl"
    site = tmp_path / "site"
    target = site / "example.py"
    site.mkdir()
    target.write_bytes(b"original")
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("example-1.0.data/platlib/example.py", b"replacement")
    with pytest.raises(ValueError, match="Overlapping wheel payload"):
        runtime_stager._extract_wheel(wheel, site)
    assert target.read_bytes() == b"original"


def test_extract_wheel_without_relocation_preserves_record_bytes(
    tmp_path,
    runtime_stager,
):
    wheel = tmp_path / "example-1.0-py3-none-any.whl"
    site = tmp_path / "site"
    record = "example-1.0.dist-info/RECORD"
    original = b'"example.py",sha256=unchanged,4\r\n'
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("example.py", b"data")
        archive.writestr(record, original)
    runtime_stager._extract_wheel(wheel, site)
    assert (site / record).read_bytes() == original


@pytest.fixture
def desktop_packages(tmp_path):
    index = tmp_path / "index"
    index.mkdir()
    source = _project(
        tmp_path / "source",
        "qwenpaw",
        (
            "pywebview>=4",
            'channel-sdk==1.0.0; extra == "channels" or extra == "full"',
            'desktop-only==1.0.0; extra == "full"',
        ),
        ("channels", "full"),
    )
    (source / "scripts/pack-tauri").mkdir(parents=True)
    requirements = source / "scripts/pack-tauri/native-host-requirements.txt"
    requirements.write_text("websockets==15.0.1\n")
    source_only = _project(tmp_path / "proxy_tools", "proxy_tools")
    with tarfile.open(index / "proxy_tools-1.0.0.tar.gz", "w:gz") as archive:
        archive.add(source_only, arcname="proxy_tools-1.0.0")
    for name, requires in (
        ("pywebview", ("proxy_tools==1.0.0",)),
        ("channel-sdk", ()),
        ("desktop-only", ()),
        ("wheel", ()),
        ("setuptools", ()),
        ("websockets", ()),
    ):
        package = _project(tmp_path / name, name, requires)
        data = json.loads((package / "package.json").read_text())
        if name == "pywebview":
            data["version"] = "4.0.0"
        elif name == "setuptools":
            data["version"] = "81.0.0"
        elif name == "websockets":
            data["version"] = "15.0.1"
        (package / "package.json").write_text(json.dumps(data))
        _wheel(package, index)
    interpreters = []
    for name in ("build", "native"):
        environment = tmp_path / name
        subprocess.run(
            [sys.executable, "-m", "venv", environment],
            check=True,
            capture_output=True,
            timeout=60,
        )
        interpreters.append(
            environment
            / ("Scripts/python.exe" if os.name == "nt" else "bin/python"),
        )
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PIP_", "UV_", "PYTHON"))
    }
    env.update(
        PIP_NO_INDEX="1",
        PIP_FIND_LINKS=str(index),
        PIP_NO_CACHE_DIR="1",
        PIP_DISABLE_PIP_VERSION_CHECK="1",
    )
    return source, index, interpreters, env


@pytest.mark.parametrize("builder", ["bash", "powershell"])
def test_release_builds_source_only_dependency_and_reuses_offline_wheels(
    desktop_packages,
    builder,
):
    executable = (
        shutil.which(
            "powershell" if os.name == "nt" else "pwsh",
        )
        if builder == "powershell"
        else shutil.which("bash")
    )
    if executable is None or (builder == "bash" and os.name == "nt"):
        pytest.skip(f"{builder} release builder unavailable")
    source, index, (build_python, native_python), env = desktop_packages
    original = subprocess.run(
        [
            str(native_python),
            "-m",
            "pip",
            "download",
            "--only-binary=:all:",
            "--dest",
            str(source / "original"),
            f"{_wheel(source, index)}[channels]",
        ],
        cwd=source,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert original.returncode != 0
    assert "No matching distribution found for proxy_tools" in original.stderr
    print(original.stderr)
    suffix = "sh" if builder == "bash" else "ps1"
    script = (SCRIPTS / f"build_pyinstaller.{suffix}").read_text()
    dependency_block = _section(
        script,
        "# Install project dependencies",
        "# Fix agent-client-protocol",
    )
    helper_block = _section(
        script,
        "== Installing bundled Python helper dependencies ==",
        (
            '"$NATIVE_HOST_PYTHON" \\\n'
            if builder == "bash"
            else "& $NATIVE_HOST_PYTHON `\n"
        ),
    )
    channel_block = _section(
        script,
        "== Building readable channel runtime ==",
        (
            '"$PYTHON_BIN" "${REPO_ROOT}/scripts/pack-tauri/'
            'stage_channel_runtime.py"'
            if builder == "bash"
            else "$RUNTIME_RELEASE ="
        ),
    )
    # Contract validation has separate tests; keep pip commands verbatim.
    channel_block = "\n".join(
        line
        for line in channel_block.splitlines()
        if "validate_channel_contracts.py" not in line
    )
    variables = {
        "REPO_ROOT": source,
        "DIST": source / "dist",
        "BINARIES_DIR": source / "resources",
        "PYTHON_BIN": build_python,
        "NATIVE_HOST_PYTHON": native_python,
    }
    if builder == "bash":
        prelude = "set -e\n" + "\n".join(
            f"{key}={shlex.quote(str(value))}"
            for key, value in variables.items()
        )
        prelude += '\ninstall_python_packages() { "$PYTHON_BIN" -m pip '
        prelude += 'install "$@"; }\n'
        install_marker = 'echo "== Installing project dependencies =="'
        remove_index = f"rm -rf {shlex.quote(str(index))}\n"
        invocation = [executable, "-c"]
    else:

        def quote(value):
            return "'" + str(value).replace("'", "''") + "'"

        prelude = "$ErrorActionPreference = 'Stop'\n" + "\n".join(
            f"${key}={quote(value)}" for key, value in variables.items()
        )
        prelude += """
function Assert-LastExit([string]$Message) {
    if ($LASTEXITCODE -ne 0) { throw $Message }
}
function Install-PythonPackages([string[]]$Packages) {
    & $PYTHON_BIN -m pip install @Packages
    Assert-LastExit 'Host dependency installation failed'
}
"""
        install_marker = 'Write-Host "== Installing project dependencies =="'
        remove_index = (
            f"Remove-Item -Recurse -Force -LiteralPath {quote(index)}\n"
        )
        invocation = [executable, "-NoProfile", "-NonInteractive", "-Command"]
    # Remove all source artifacts before the host installation starts. Both
    # consumers must use the resolved wheel pool, not a cache or source index.
    dependency_block = dependency_block.replace(
        install_marker,
        remove_index + install_marker,
    )
    _run(
        [
            *invocation,
            prelude + dependency_block + helper_block + channel_block,
        ],
        cwd=source,
        env=env,
    )
    desktop = source / "dist/desktop-wheels"
    assert list(desktop.glob("proxy_tools-*.whl"))
    assert list(desktop.glob("desktop_only-*.whl"))
    assert not index.exists()
    channels = source / "dist/channel-wheels"
    names = {path.name.split("-")[0] for path in channels.glob("*.whl")}
    assert names == {"qwenpaw", "pywebview", "proxy_tools", "channel_sdk"}
    for artifact in channels.glob("*.whl"):
        assert artifact.read_bytes() == (desktop / artifact.name).read_bytes()
    for interpreter, expected in (
        (
            build_python,
            {
                "qwenpaw": "1.0.0",
                "channel-sdk": "1.0.0",
                "desktop-only": "1.0.0",
                "pywebview": "4.0.0",
                "proxy_tools": "1.0.0",
                "setuptools": "81.0.0",
            },
        ),
        (native_python, {"websockets": "15.0.1"}),
    ):
        _run(
            [
                interpreter,
                "-I",
                "-c",
                "from importlib.metadata import version; "
                f"assert all(version(n) == v for n, v "
                f"in {expected!r}.items())",
            ],
            cwd=source,
            env=env,
        )
    # Re-run only the production subset resolver against incomplete or
    # incompatible artifacts. It must fail closed without a source fallback.
    host_wheel = next(desktop.glob("qwenpaw-*.whl"))
    if builder == "bash":
        resume = prelude + (
            f"DESKTOP_WHEELS={shlex.quote(str(desktop))}\n"
            f"HOST_WHEEL={shlex.quote(str(host_wheel))}\n"
        )
    else:
        resume = prelude + (
            f"$DESKTOP_WHEELS={quote(desktop)}\n"
            f"$HOST_WHEEL={quote(host_wheel)}\n"
        )
    proxy = next(desktop.glob("proxy_tools-*.whl"))
    for replacement in (
        source / "absent.whl",
        desktop / "proxy_tools-0.0.0-py3-none-any.whl",
        desktop / "proxy_tools-1.0.0-cp39-cp39-win32.whl",
    ):
        proxy.rename(replacement)
        try:
            failure = _run(
                [*invocation, resume + channel_block],
                cwd=source,
                env=env,
                success=False,
            )
            output = failure.stdout + failure.stderr
            assert "No matching distribution found for proxy_tools" in output
            assert not list(channels.glob("proxy_tools-*.whl"))
        finally:
            replacement.rename(proxy)
