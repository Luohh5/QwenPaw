# -*- coding: utf-8 -*-
"""Tests for CH-1-003 immutable environment installation."""

from __future__ import annotations

import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import stat
import subprocess
import threading
import zipfile

import pytest
from packaging.tags import sys_tags

from qwenpaw.app.channels.environment_installer import (
    DEFAULT_ALIYUN_INDEX_URL,
    DependencySource,
    EnvironmentInstallError,
    EnvironmentInstaller,
)
from qwenpaw.app.channels.env_manager import (
    EnvironmentSpecManifest,
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


def _write_wheel(path: Path) -> None:
    """Write a minimal installable pure-Python wheel."""
    dist_info = "demo_package-1.0.dist-info"
    files = {
        "demo_package/__init__.py": b"VALUE = 1\n",
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
