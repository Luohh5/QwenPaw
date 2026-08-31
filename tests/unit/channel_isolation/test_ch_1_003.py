# -*- coding: utf-8 -*-
"""Tests for CH-1-003 immutable environment installation."""

from __future__ import annotations

import base64
import errno
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import threading
from types import SimpleNamespace
import zipfile

import pytest
from packaging.tags import sys_tags

import qwenpaw.app.channels.environment_installer as installer_module
import qwenpaw.app.channels.env_manager as env_manager_module

from qwenpaw.app.channels.environment_installer import (
    DEFAULT_ALIYUN_INDEX_URL,
    DependencySource,
    EnvironmentInstallError,
    EnvironmentInstaller,
    _TargetPython,
    _active_packages,
    _cleanup_path,
    _select_wheel,
)
from qwenpaw.app.channels.env_manager import (
    compute_venv_tree_sha256,
    EnvironmentSpecManifest,
    _validate_venv_launchers,
    validate_installed_environment,
)
from qwenpaw.channel_protocol import (
    EnvironmentSpecIdentity,
    LockFile,
    LockPackage,
    RELEASE_TARGET_PLATFORM_TAGS,
    WheelFile,
    condition_set_sha256,
    current_python_abi,
    write_canonical_json,
)


PLATFORM_TAG = next(
    tag
    for tag in sorted(RELEASE_TARGET_PLATFORM_TAGS)
    if tag in {item.platform for item in sys_tags()}
)
EMPTY_CONDITION_DIGEST = condition_set_sha256({})


def _empty_release() -> tuple[EnvironmentSpecManifest, LockFile]:
    """Build one valid empty lock and its matching environment spec."""
    lock = LockFile(
        channel_key="fixture",
        python_abi=current_python_abi(),
        platform_tag=PLATFORM_TAG,
        condition_set_sha256=EMPTY_CONDITION_DIGEST,
        direct_requirements=(),
        packages=(),
    )
    identity = EnvironmentSpecIdentity.create(
        channel_key="fixture",
        lock_sha256=lock.sha256(),
        python_abi=current_python_abi(),
        platform_tag=PLATFORM_TAG,
        condition_set={},
        allowed_platform_tags=RELEASE_TARGET_PLATFORM_TAGS,
    )
    return EnvironmentSpecManifest.from_identity(identity), lock


def _wheel(path: Path) -> str:
    """Return the SHA-256 digest of one wheel fixture."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_wheel(path: Path, *, with_console_script: bool = False) -> None:
    """Write a minimal installable pure-Python wheel."""
    dist_info = "demo_package-1.0.dist-info"
    files = {
        "demo_package/__init__.py": (
            b"VALUE = 1\n" b"\n" b"def main():\n" b"    print('console-ok')\n"
            if with_console_script
            else b"VALUE = 1\n"
        ),
        f"{dist_info}/METADATA": (
            b"Metadata-Version: 2.1\n"
            b"Name: demo-package\n"
            b"Version: 1.0\n"
        ),
        f"{dist_info}/WHEEL": (
            b"Wheel-Version: 1.0\n"
            b"Generator: qwenpaw-test\n"
            b"Root-Is-Purelib: true\n"
            b"Tag: py3-none-any\n"
        ),
    }
    if with_console_script:
        files[f"{dist_info}/entry_points.txt"] = (
            b"[console_scripts]\n" b"demo-cli = demo_package:main\n"
        )
    rows: list[tuple[str, str, str]] = []
    for relative, content in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest())
        rows.append(
            (
                relative,
                f"sha256={digest.rstrip(b'=').decode()}",
                str(len(content)),
            ),
        )
    record = f"{dist_info}/RECORD"
    rows.append((record, "", ""))
    files[record] = "".join(
        f"{relative},{digest},{size}\n" for relative, digest, size in rows
    ).encode()
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for relative, content in files.items():
            archive.writestr(relative, content)


def _package(wheel_path: Path) -> LockPackage:
    """Build a lock package for the local fixture wheel."""
    filename = wheel_path.name
    return LockPackage(
        name="demo-package",
        version="1.0",
        marker=None,
        direct=True,
        wheels=(
            WheelFile(
                filename=filename,
                url=None,
                sha256=_wheel(wheel_path),
            ),
        ),
    )


def _release_with_wheel(
    wheel_path: Path,
) -> tuple[EnvironmentSpecManifest, LockFile]:
    """Build a spec and lock that require one cached wheel."""
    package = _package(wheel_path)
    lock = LockFile(
        channel_key="fixture",
        python_abi=current_python_abi(),
        platform_tag=PLATFORM_TAG,
        condition_set_sha256=EMPTY_CONDITION_DIGEST,
        direct_requirements=("demo-package==1.0",),
        packages=(package,),
    )
    identity = EnvironmentSpecIdentity.create(
        channel_key="fixture",
        lock_sha256=lock.sha256(),
        python_abi=current_python_abi(),
        platform_tag=PLATFORM_TAG,
        condition_set={},
        allowed_platform_tags=RELEASE_TARGET_PLATFORM_TAGS,
    )
    return EnvironmentSpecManifest.from_identity(identity), lock


def test_empty_environment_is_published_read_only_and_reused(
    tmp_path: Path,
) -> None:
    """An empty lock still creates a validated immutable environment."""
    spec, lock = _empty_release()
    installer = EnvironmentInstaller(tmp_path / "environments")

    first = installer.install(
        spec=spec,
        lock=lock,
        source=DependencySource.offline(),
    )
    second = installer.install(
        spec=spec,
        lock=lock,
        source=DependencySource.offline(),
    )

    assert first.environment_id == second.environment_id
    assert second.reused
    assert not first.manifest.packages
    assert (first.environment_directory / "install.json").is_file()
    assert not (
        first.environment_directory.joinpath("install.json").stat().st_mode
        & stat.S_IWUSR
    )
    result = validate_installed_environment(
        environment_directory=first.environment_directory,
        interpreter=first.interpreter,
        expected_spec=spec,
        expected_environment_id=first.environment_id,
        expected_lock=lock,
        allowed_platform_tags=RELEASE_TARGET_PLATFORM_TAGS,
    )
    assert result.valid


def test_cached_wheel_is_installed_and_only_locked_distribution_remains(
    tmp_path: Path,
) -> None:
    """The installer consumes an exact cached wheel without network access."""
    cache = tmp_path / "cache"
    cache.mkdir()
    wheel_path = cache / "demo_package-1.0-py3-none-any.whl"
    _write_wheel(wheel_path)
    spec, lock = _release_with_wheel(wheel_path)

    result = EnvironmentInstaller(
        tmp_path / "environments",
        wheel_cache=cache,
    ).install(
        spec=spec,
        lock=lock,
        source=DependencySource.offline(),
    )

    probe = subprocess.run(
        [
            str(result.interpreter),
            "-I",
            "-c",
            "import demo_package; print(demo_package.VALUE)",
        ],
        capture_output=True,
        check=True,
        text=True,
    )
    assert probe.stdout.strip() == "1"
    assert [item.name for item in result.manifest.packages] == [
        "demo-package",
    ]


def test_wrong_cached_hash_fails_without_publishing(tmp_path: Path) -> None:
    """A cache digest mismatch never creates a bootable environment."""
    cache = tmp_path / "cache"
    cache.mkdir()
    wheel_path = cache / "demo_package-1.0-py3-none-any.whl"
    _write_wheel(wheel_path)
    spec, lock = _release_with_wheel(wheel_path)
    wheel_path.write_bytes(b"tampered")

    with pytest.raises(EnvironmentInstallError) as error:
        EnvironmentInstaller(
            tmp_path / "environments",
            wheel_cache=cache,
        ).install(
            spec=spec,
            lock=lock,
            source=DependencySource.offline(),
        )

    assert error.value.code == "wheel_hash_mismatch"
    environment_root = tmp_path / "environments"
    assert not list(environment_root.rglob("install.json"))
    assert not list(environment_root.glob(".*.staging-*"))


def test_source_never_falls_back_to_default(tmp_path: Path) -> None:
    """An unavailable selected source is reported without trying Aliyun."""
    source = DependencySource.custom("https://source.invalid/simple")
    assert source.base_url == "https://source.invalid/simple/"
    assert DEFAULT_ALIYUN_INDEX_URL not in str(source)

    wheel_path = tmp_path / "demo_package-1.0-py3-none-any.whl"
    _write_wheel(wheel_path)
    spec, lock = _release_with_wheel(wheel_path)
    with pytest.raises(EnvironmentInstallError) as error:
        EnvironmentInstaller(tmp_path / "environments").install(
            spec=spec,
            lock=lock,
            source=source,
        )
    assert error.value.code == "source_unavailable"
    assert DEFAULT_ALIYUN_INDEX_URL not in str(error.value)


def test_custom_simple_index_downloads_wheel_with_resolved_credential(
    tmp_path: Path,
) -> None:
    """The selected simple index supplies the exact locked wheel."""
    wheel_path = tmp_path / "demo_package-1.0-py3-none-any.whl"
    _write_wheel(wheel_path)
    spec, lock = _release_with_wheel(wheel_path)
    authorization: list[str | None] = []

    class Handler(BaseHTTPRequestHandler):
        """Serve one package page and its wheel for the installer."""

        def do_GET(self) -> None:  # noqa: N802
            authorization.append(self.headers.get("Authorization"))
            if self.path == "/simple/demo-package/":
                body = (
                    f'<a href="/files/{wheel_path.name}">'
                    f"{wheel_path.name}</a>"
                ).encode("utf-8")
                status = 200
            elif self.path == f"/files/{wheel_path.name}":
                body = wheel_path.read_bytes()
                status = 200
            else:
                body = b"not found"
                status = 404
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format_string: str, *args: object) -> None:
            """Keep fixture requests out of test output."""

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        source = DependencySource.custom(
            f"http://127.0.0.1:{server.server_port}/simple",
            credential_ref="credential/private-index",
        )
        result = EnvironmentInstaller(
            tmp_path / "environments",
            credential_resolver=lambda ref: "top-secret" if ref else None,
        ).install(spec=spec, lock=lock, source=source)
    finally:
        server.shutdown()
        thread.join()

    assert authorization == ["Bearer top-secret", "Bearer top-secret"]
    manifest_text = (result.environment_directory / "install.json").read_text()
    assert "top-secret" not in manifest_text
    assert result.manifest.source.base_url == source.base_url


# pylint: disable=protected-access
def test_custom_credential_is_not_persisted_or_in_error(
    tmp_path: Path,
) -> None:
    """Credentials enter only through the resolver boundary."""
    source = DependencySource.custom(
        "https://source.invalid/simple",
        credential_ref="credential/private-index",
    )
    installer = EnvironmentInstaller(
        tmp_path / "environments",
        credential_resolver=lambda ref: "top-secret" if ref else None,
    )
    headers = installer._request_headers(source.base_url or "", source)
    assert headers["Authorization"] == "Bearer top-secret"
    assert "top-secret" not in json.dumps(source.__dict__)


def test_credentials_require_complete_origin_match(tmp_path: Path) -> None:
    """Credentials are withheld for protocol and effective-port changes."""
    source = DependencySource.custom(
        "https://source.invalid/simple",
        credential_ref="credential/private-index",
    )
    installer = EnvironmentInstaller(
        tmp_path / "environments",
        credential_resolver=lambda ref: "top-secret" if ref else None,
    )
    assert "Authorization" in installer._request_headers(
        "https://source.invalid:443/files/demo.whl",
        source,
    )
    assert "Authorization" not in installer._request_headers(
        "https://source.invalid:444/files/demo.whl",
        source,
    )
    assert "Authorization" not in installer._request_headers(
        "http://source.invalid/files/demo.whl",
        source,
    )


def test_parallel_installers_publish_one_environment(tmp_path: Path) -> None:
    """Concurrent callers serialize and the second caller reuses the result."""
    spec, lock = _empty_release()
    installer = EnvironmentInstaller(tmp_path / "environments")
    results: list[str] = []
    errors: list[BaseException] = []

    def run() -> None:
        try:
            results.append(
                installer.install(
                    spec=spec,
                    lock=lock,
                    source=DependencySource.offline(),
                ).environment_id,
            )
        except BaseException as exc:  # pragma: no cover - test guard
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    assert len(results) == 2
    assert len(set(results)) == 1
    published = list(
        (tmp_path / "environments").rglob("install.json"),
    )
    assert len(published) == 1


def test_source_models_reject_embedded_credentials() -> None:
    """Source URLs cannot carry credentials or query values."""
    with pytest.raises(ValueError):
        DependencySource.custom("https://user:password@example.invalid/simple")
    with pytest.raises(ValueError):
        DependencySource.custom("https://example.invalid/simple?token=secret")


def test_redirect_to_other_origin_drops_authorization(
    tmp_path: Path,
) -> None:
    """A wheel redirect to another port never receives source credentials."""
    wheel_path = tmp_path / "demo_package-1.0-py3-none-any.whl"
    _write_wheel(wheel_path)
    spec, lock = _release_with_wheel(wheel_path)
    source_authorization: list[str | None] = []
    target_authorization: list[str | None] = []

    class TargetHandler(BaseHTTPRequestHandler):
        """Serve the redirected wheel and record its authorization header."""

        def do_GET(self) -> None:  # noqa: N802
            target_authorization.append(self.headers.get("Authorization"))
            body = wheel_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format_string: str, *args: object) -> None:
            """Keep fixture requests out of test output."""

    target_server = ThreadingHTTPServer(("127.0.0.1", 0), TargetHandler)

    class SourceHandler(BaseHTTPRequestHandler):
        """Serve a simple index and redirect the wheel cross-origin."""

        def do_GET(self) -> None:  # noqa: N802
            source_authorization.append(self.headers.get("Authorization"))
            if self.path == "/simple/demo-package/":
                body = (
                    f'<a href="/redirect/{wheel_path.name}">'
                    f"{wheel_path.name}</a>"
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(302)
            self.send_header(
                "Location",
                f"http://127.0.0.1:{target_server.server_port}/wheel",
            )
            self.end_headers()

        def log_message(self, format_string: str, *args: object) -> None:
            """Keep fixture requests out of test output."""

    source_server = ThreadingHTTPServer(("127.0.0.1", 0), SourceHandler)
    target_thread = threading.Thread(
        target=target_server.serve_forever,
        daemon=True,
    )
    source_thread = threading.Thread(
        target=source_server.serve_forever,
        daemon=True,
    )
    target_thread.start()
    source_thread.start()
    try:
        source = DependencySource.custom(
            f"http://127.0.0.1:{source_server.server_port}/simple",
            credential_ref="credential/private-index",
        )
        EnvironmentInstaller(
            tmp_path / "environments",
            credential_resolver=lambda ref: "top-secret" if ref else None,
        ).install(spec=spec, lock=lock, source=source)
    finally:
        source_server.shutdown()
        target_server.shutdown()
        source_thread.join()
        target_thread.join()

    assert source_authorization == ["Bearer top-secret", "Bearer top-secret"]
    assert target_authorization == [None]


def test_console_script_runs_after_atomic_publish(tmp_path: Path) -> None:
    """Entry points use the canonical post-publish interpreter."""
    cache = tmp_path / "cache"
    cache.mkdir()
    wheel_path = cache / "demo_package-1.0-py3-none-any.whl"
    _write_wheel(wheel_path, with_console_script=True)
    spec, lock = _release_with_wheel(wheel_path)

    result = EnvironmentInstaller(
        tmp_path / "environments",
        wheel_cache=cache,
    ).install(
        spec=spec,
        lock=lock,
        source=DependencySource.offline(),
    )

    scripts = "Scripts" if os.name == "nt" else "bin"
    suffix = ".exe" if os.name == "nt" else ""
    entrypoint = (
        result.environment_directory / "venv" / scripts / (f"demo-cli{suffix}")
    )
    probe = subprocess.run(
        [str(entrypoint)],
        capture_output=True,
        check=True,
        text=True,
    )
    assert probe.stdout.strip() == "console-ok"


def test_stale_console_script_environment_requires_repair(
    tmp_path: Path,
) -> None:
    """An old staging shebang cannot make an environment reusable."""
    cache = tmp_path / "cache"
    cache.mkdir()
    wheel_path = cache / "demo_package-1.0-py3-none-any.whl"
    _write_wheel(wheel_path, with_console_script=True)
    spec, lock = _release_with_wheel(wheel_path)
    installer = EnvironmentInstaller(
        tmp_path / "environments",
        wheel_cache=cache,
    )

    first = installer.install(
        spec=spec,
        lock=lock,
        source=DependencySource.offline(),
    )
    scripts = "Scripts" if os.name == "nt" else "bin"
    launcher_name = "demo-cli.exe" if os.name == "nt" else "demo-cli"
    launcher = first.environment_directory / "venv" / scripts / launcher_name
    original = launcher.read_bytes()
    canonical_venv = first.environment_directory / "venv"
    stale_venv = tmp_path / "old-staging" / "venv"
    updated = original.replace(
        str(canonical_venv).encode(),
        str(stale_venv).encode(),
    )
    assert updated != original
    launcher.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    launcher.write_bytes(updated)
    for record_path in (first.environment_directory / "venv").rglob("RECORD"):
        record_path.chmod(record_path.stat().st_mode | stat.S_IWUSR)
    installer_module._refresh_venv_records(
        first.environment_directory / "venv",
    )
    manifest_path = first.environment_directory / "install.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["venv_tree_sha256"] = compute_venv_tree_sha256(
        first.environment_directory / "venv",
    )
    manifest_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    write_canonical_json(manifest_path, manifest)
    installer_module._make_read_only(first.environment_directory)

    validation = validate_installed_environment(
        environment_directory=first.environment_directory,
        interpreter=first.interpreter,
        expected_spec=spec,
        expected_environment_id=first.environment_id,
        expected_lock=lock,
        allowed_platform_tags=RELEASE_TARGET_PLATFORM_TAGS,
    )
    assert validation.status == "repair_required"
    assert any("launcher" in reason for reason in validation.reasons)
    assert (
        installer._find_existing(
            first.environment_directory.parent,
            spec,
            lock,
        )
        is None
    )


def test_windows_exe_launcher_requires_canonical_interpreter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows exe launchers are checked without a companion script."""
    monkeypatch.setattr(
        env_manager_module,
        "os",
        SimpleNamespace(name="nt", path=os.path, sep=os.sep),
    )
    venv_root = tmp_path / "venv"
    scripts_root = venv_root / "Scripts"
    scripts_root.mkdir(parents=True)
    interpreter = scripts_root / "python.exe"
    interpreter.write_bytes(b"python")
    launcher = scripts_root / "demo-cli.exe"
    archive_data = io.BytesIO()
    with zipfile.ZipFile(archive_data, "w") as archive:
        archive.writestr("__main__.py", "print('console-ok')")
    launcher.write_bytes(
        b"MZ"
        + b"\0" * 64
        + b"#!"
        + str(interpreter).encode()
        + b"\r\n"
        + archive_data.getvalue(),
    )

    assert not launcher.with_name("demo-cli-script.py").exists()
    assert not _validate_venv_launchers(venv_root, interpreter)

    (scripts_root / "native-tool.exe").write_bytes(b"MZ" + b"native-tool")
    assert not _validate_venv_launchers(venv_root, interpreter)

    stale = tmp_path / "old-staging" / "venv" / "Scripts" / "python.exe"
    launcher.write_bytes(
        b"MZ"
        + b"\0" * 64
        + b"#!"
        + str(stale).encode()
        + b"\r\n"
        + archive_data.getvalue(),
    )
    reasons = _validate_venv_launchers(venv_root, interpreter)
    assert any("demo-cli.exe" in reason for reason in reasons)


def test_target_probe_controls_marker_and_wheel_selection() -> None:
    """Package and wheel selection uses target probe data, not host globals."""
    marker_package = LockPackage(
        name="marker-package",
        version="1.0",
        marker='sys_platform == "target-os"',
        direct=False,
        wheels=(
            WheelFile(
                filename="marker_package-1.0-py3-none-any.whl",
                url=None,
                sha256="a" * 64,
            ),
        ),
    )
    skipped_package = LockPackage(
        name="skipped-package",
        version="1.0",
        marker='sys_platform == "other-os"',
        direct=False,
        wheels=(),
    )
    target = _TargetPython(
        abi="cp311-cp311",
        platform_tags=frozenset({"any"}),
        marker_environment={"sys_platform": "target-os"},
        supported_tags=frozenset({"py3-none-any"}),
    )
    lock = LockFile(
        channel_key="fixture",
        python_abi="cp311-cp311",
        platform_tag=PLATFORM_TAG,
        condition_set_sha256=EMPTY_CONDITION_DIGEST,
        direct_requirements=(),
        packages=(marker_package, skipped_package),
    )

    active = _active_packages(lock, target)
    assert set(active) == {"marker-package"}
    assert _select_wheel(marker_package, target) == marker_package.wheels[0]


def test_publish_freezes_staging_before_final_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The final directory is absent while staging is frozen and validated."""
    cache = tmp_path / "cache"
    cache.mkdir()
    wheel_path = cache / "demo_package-1.0-py3-none-any.whl"
    _write_wheel(wheel_path)
    spec, lock = _release_with_wheel(wheel_path)
    final_spec = (
        tmp_path
        / "environments"
        / installer_module.dir_key(
            spec.environment_spec_id,
        )
    )
    observed: list[Path] = []
    original_make_read_only = installer_module._make_read_only

    def freeze(path: Path) -> None:
        assert not final_spec.exists()
        observed.append(path)
        original_make_read_only(path)

    monkeypatch.setattr(installer_module, "_make_read_only", freeze)
    EnvironmentInstaller(
        tmp_path / "environments",
        wheel_cache=cache,
    ).install(
        spec=spec,
        lock=lock,
        source=DependencySource.offline(),
    )
    assert observed


def test_destination_race_does_not_delete_competitor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed publish only removes this install's staging directory."""
    spec, lock = _empty_release()
    root = tmp_path / "environments"
    root.mkdir()
    spec_directory = root / installer_module.dir_key(spec.environment_spec_id)
    installer = EnvironmentInstaller(root)
    spec_directory.mkdir()
    installer_module._write_canonical_atomic(
        spec_directory / "environment_spec.json",
        spec.to_mapping(),
    )
    target = installer._probe_target_python(installer.base_python, spec)
    real_replace = installer_module.os.replace
    injected = False
    competitor: Path | None = None

    def race(
        source: str | os.PathLike[str],
        destination: str | os.PathLike[str],
    ) -> None:
        nonlocal competitor, injected
        destination_path = Path(destination)
        if (
            destination_path.parent == spec_directory
            and destination_path.name.startswith("dir1_")
            and not injected
        ):
            competitor = destination_path
            destination_path.mkdir()
            injected = True
            raise OSError(errno.EEXIST, "destination appeared")
        real_replace(source, destination)

    monkeypatch.setattr(installer_module.os, "replace", race)
    with pytest.raises(EnvironmentInstallError):
        installer._install_staged(
            spec_directory=spec_directory,
            spec=spec,
            lock=lock,
            source=DependencySource.offline(),
            wheel_cache=None,
            base_python=installer.base_python,
            target_python=target,
        )
    assert competitor is not None and competitor.is_dir()


def test_cleanup_retries_and_reports_orphan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Cleanup retries transient failures and diagnoses permanent orphans."""
    path = tmp_path / "staging"
    path.mkdir()
    calls = 0
    original_rmtree = installer_module.shutil.rmtree

    def flaky(target: Path) -> None:
        nonlocal calls
        calls += 1
        if calls < installer_module._CLEANUP_RETRIES:
            raise OSError(errno.EACCES, "busy")
        original_rmtree(target)

    monkeypatch.setattr(installer_module.shutil, "rmtree", flaky)
    _cleanup_path(path)
    assert calls == installer_module._CLEANUP_RETRIES
    assert not path.exists()

    path.mkdir()
    monkeypatch.setattr(
        installer_module.shutil,
        "rmtree",
        lambda target: (_ for _ in ()).throw(OSError(errno.EACCES, "busy")),
    )
    _cleanup_path(path)
    assert "orphan_staging" in caplog.text
