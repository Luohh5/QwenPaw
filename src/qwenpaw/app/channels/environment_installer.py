# -*- coding: utf-8 -*-
"""Install immutable, lock-pinned Channel dependency environments."""

from __future__ import annotations

import base64
import csv
from dataclasses import dataclass
from http.client import HTTPMessage
from html.parser import HTMLParser
import hashlib
from importlib import metadata
import json
import logging
import os
from pathlib import Path
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Collection, Mapping
from typing import IO, Callable, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urljoin, urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    Request,
    build_opener,
)

import packaging
from packaging.utils import canonicalize_name, parse_wheel_filename

from ...channel_protocol.artifacts import LockFile, LockPackage, WheelFile
from ...channel_protocol.canonical import canonical_json
from ...channel_protocol.errors import ArtifactValidationError
from ...channel_protocol.identifiers import (
    DirectoryIdentity,
    EnvironmentIdentity,
    dir_key,
)
from ...channel_protocol.release_target_registry import (
    RELEASE_TARGET_PLATFORM_TAGS,
)
from ...plugins.install_lock import plugin_install_lock
from .env_manager import (
    EnvironmentManifestError,
    EnvironmentSpecManifest,
    InstallManifest,
    InstallSource,
    WheelProvenance,
    compute_venv_tree_sha256,
    validate_installed_environment,
)


DEFAULT_ALIYUN_INDEX_URL = "https://mirrors.aliyun.com/pypi/simple/"
DEFAULT_PYPI_INDEX_URL = "https://pypi.org/simple/"
DEFAULT_PYTHON_INDEX_URL = DEFAULT_ALIYUN_INDEX_URL
_LOGGER = logging.getLogger(__name__)
_CLEANUP_RETRIES = 3
_CLEANUP_RETRY_DELAY_SECONDS = 0.05
_PACKAGING_ROOT = str(Path(packaging.__file__).resolve().parent.parent)

_TARGET_PROBE_SCRIPT = """
import json
import os
import platform
import sys

packaging_root = sys.argv[1]
sys.path.insert(0, packaging_root)
from packaging.tags import sys_tags

version = platform.python_version()
major, minor = sys.version_info[:2]
tags = tuple(sys_tags())
payload = {
    "abi": next(
        (
            f"{tag.interpreter}-{tag.abi}"
            for tag in tags
            if tag.abi != "none"
        ),
        None,
    ),
    "compatible_platform_tags": sorted({tag.platform for tag in tags}),
    "marker_environment": {
        "implementation_name": sys.implementation.name,
        "implementation_version": version,
        "os_name": os.name,
        "platform_machine": platform.machine(),
        "platform_release": platform.release(),
        "platform_system": platform.system(),
        "platform_version": platform.version(),
        "platform_python_implementation": platform.python_implementation(),
        "python_full_version": version,
        "python_version": f"{major}.{minor}",
        "sys_platform": sys.platform,
    },
    "supported_tags": sorted({str(tag) for tag in tags}),
}
print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
"""

DependencySourceKind = Literal["aliyun", "pypi", "custom", "offline"]
CredentialResolver = Callable[
    [str],
    Mapping[str, str] | str | None,
]


class EnvironmentInstallError(RuntimeError):
    """Report one diagnosed environment installation failure."""

    def __init__(self, code: str, reason: str) -> None:
        self.code = code
        self.reason = reason
        super().__init__(f"{code}: {reason}")


@dataclass(frozen=True)
class _TargetPython:
    """Describe the selected base interpreter's packaging environment."""

    abi: str
    platform_tags: frozenset[str]
    marker_environment: Mapping[str, str]
    supported_tags: frozenset[str]


class _OriginRedirectHandler(HTTPRedirectHandler):
    """Strip authorization when urllib follows an origin-changing redirect."""

    def redirect_request(
        self,
        req: Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> Request | None:
        """Build a redirect request with credentials scoped to one origin."""
        redirected = super().redirect_request(
            req,
            fp,
            code,
            msg,
            headers,
            newurl,
        )
        if redirected is None:
            return None
        if _url_origin(req.full_url) != _url_origin(newurl):
            redirected.remove_header("Authorization")
        return redirected


@dataclass(frozen=True)
class DependencySource:
    """Describe the explicit source used to obtain locked wheels."""

    kind: DependencySourceKind = "aliyun"
    base_url: str | None = DEFAULT_ALIYUN_INDEX_URL
    credential_ref: str | None = None

    def __post_init__(self) -> None:  # pylint: disable=too-many-branches
        """Validate source identity without accepting embedded credentials."""
        allowed = {"aliyun", "pypi", "custom", "offline"}
        if self.kind not in allowed:
            raise ValueError("Unknown dependency source kind")
        if self.kind == "offline":
            if self.base_url is not None:
                raise ValueError("Offline source must not have a URL")
        else:
            if not self.base_url:
                raise ValueError("Network source requires a URL")
            parts = urlsplit(self.base_url)
            if parts.scheme not in {"http", "https"}:
                raise ValueError("Dependency source URL must be HTTP(S)")
            if not parts.hostname or parts.username or parts.password:
                raise ValueError("Dependency source URL must not contain auth")
            if parts.query or parts.fragment:
                raise ValueError(
                    "Dependency source URL must not contain query",
                )
            if any(character.isspace() for character in self.base_url):
                raise ValueError(
                    "Dependency source URL must not contain space",
                )
        if self.kind == "aliyun" and self.base_url != DEFAULT_ALIYUN_INDEX_URL:
            raise ValueError("Aliyun source URL is fixed")
        if self.kind == "pypi" and self.base_url != DEFAULT_PYPI_INDEX_URL:
            raise ValueError("PyPI source URL is fixed")
        if self.credential_ref is not None:
            if self.kind != "custom" or not self.credential_ref:
                raise ValueError(
                    "Credentials require a non-empty custom source reference",
                )

    @classmethod
    def aliyun(cls) -> "DependencySource":
        """Return the global default source."""
        return cls()

    @classmethod
    def pypi(cls) -> "DependencySource":
        """Return the official PyPI source."""
        return cls(kind="pypi", base_url=DEFAULT_PYPI_INDEX_URL)

    @classmethod
    def custom(
        cls,
        base_url: str,
        *,
        credential_ref: str | None = None,
    ) -> "DependencySource":
        """Create a credential-free custom source description."""
        normalized = base_url.rstrip("/") + "/"
        return cls(
            kind="custom",
            base_url=normalized,
            credential_ref=credential_ref,
        )

    @classmethod
    def offline(cls) -> "DependencySource":
        """Create a cache-only source description."""
        return cls(kind="offline", base_url=None)


@dataclass(frozen=True)
class InstalledEnvironment:
    """Describe one published immutable environment."""

    environment_id: str
    environment_spec_id: str
    environment_directory: Path
    interpreter: Path
    manifest: InstallManifest
    reused: bool = False


class _SimpleIndexParser(HTMLParser):
    """Collect links from a PEP 503 simple index page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        """Record anchor href values without interpreting page semantics."""
        if tag.lower() != "a":
            return
        for name, value in attrs:
            if name.lower() == "href" and value:
                self.links.append(value)


class EnvironmentInstaller:
    """Build and atomically publish one exact locked environment."""

    def __init__(
        self,
        environments_root: Path,
        *,
        allowed_platform_tags: Collection[str] = RELEASE_TARGET_PLATFORM_TAGS,
        base_python: Path | None = None,
        default_source: DependencySource | None = None,
        wheel_cache: Path | None = None,
        credential_resolver: CredentialResolver | None = None,
        lock_timeout: float = 300.0,
        subprocess_timeout: float = 300.0,
    ) -> None:
        self.environments_root = Path(environments_root).resolve()
        self.allowed_platform_tags = frozenset(allowed_platform_tags)
        self.base_python = Path(base_python or sys.executable).resolve()
        self.default_source = default_source or DependencySource.aliyun()
        self.wheel_cache = (
            None if wheel_cache is None else Path(wheel_cache).resolve()
        )
        self.credential_resolver = credential_resolver
        self.lock_timeout = lock_timeout
        self.subprocess_timeout = subprocess_timeout
        self._url_opener = build_opener(_OriginRedirectHandler())

    def install(
        self,
        *,
        spec: EnvironmentSpecManifest,
        lock: LockFile,
        source: DependencySource | None = None,
        wheel_cache: Path | None = None,
        base_python: Path | None = None,
    ) -> InstalledEnvironment:
        """Install or reuse one exact immutable environment."""
        self._validate_selection(spec, lock)
        selected_source = source or self.default_source
        selected_cache = (
            self.wheel_cache
            if wheel_cache is None
            else Path(wheel_cache).resolve()
        )
        selected_python = (
            self.base_python
            if base_python is None
            else Path(base_python).resolve()
        )
        if not selected_python.is_file():
            raise EnvironmentInstallError(
                "interpreter_missing",
                "Base Python interpreter is not a file",
            )
        target_python = self._probe_target_python(selected_python, spec)

        spec_directory = self.environments_root / dir_key(
            spec.environment_spec_id,
        )
        lock_path = self.environments_root / (
            f".{spec_directory.name}.install.lock"
        )
        with plugin_install_lock(
            lock_path,
            timeout=self.lock_timeout,
        ) as acquired:
            if not acquired:
                raise EnvironmentInstallError(
                    "install_lock_timeout",
                    "Timed out waiting for the environment install lock",
                )
            if spec_directory.exists():
                self._ensure_spec_manifest(spec_directory, spec)
            existing = self._find_existing(spec_directory, spec, lock)
            if existing is not None:
                return existing
            return self._install_staged(
                spec_directory=spec_directory,
                spec=spec,
                lock=lock,
                source=selected_source,
                wheel_cache=selected_cache,
                base_python=selected_python,
                target_python=target_python,
            )

    def _probe_target_python(
        self,
        base_python: Path,
        spec: EnvironmentSpecManifest,
    ) -> _TargetPython:
        """Read marker and wheel compatibility data from the target Python."""
        try:
            result = subprocess.run(
                [
                    str(base_python),
                    "-I",
                    "-B",
                    "-c",
                    _TARGET_PROBE_SCRIPT,
                    _PACKAGING_ROOT,
                ],
                capture_output=True,
                check=False,
                env=_clean_subprocess_env(),
                text=True,
                timeout=self.subprocess_timeout,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise EnvironmentInstallError(
                "interpreter_probe_failed",
                "Selected base Python probe failed",
            ) from exc
        if result.returncode != 0:
            raise EnvironmentInstallError(
                "interpreter_probe_failed",
                "Selected base Python probe failed",
            )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise EnvironmentInstallError(
                "interpreter_probe_failed",
                "Selected base Python probe returned invalid data",
            ) from exc
        if not isinstance(payload, Mapping):
            raise EnvironmentInstallError(
                "interpreter_probe_failed",
                "Selected base Python probe returned invalid data",
            )
        abi = payload.get("abi")
        raw_platform_tags = payload.get("compatible_platform_tags")
        raw_supported_tags = payload.get("supported_tags")
        marker_environment = payload.get("marker_environment")
        if not isinstance(abi, str):
            raise EnvironmentInstallError(
                "interpreter_probe_failed",
                "Selected base Python probe returned invalid data",
            )
        if not isinstance(raw_platform_tags, list):
            raise EnvironmentInstallError(
                "interpreter_probe_failed",
                "Selected base Python probe returned invalid data",
            )
        if not isinstance(raw_supported_tags, list):
            raise EnvironmentInstallError(
                "interpreter_probe_failed",
                "Selected base Python probe returned invalid data",
            )
        if not isinstance(marker_environment, Mapping):
            raise EnvironmentInstallError(
                "interpreter_probe_failed",
                "Selected base Python probe returned invalid data",
            )
        if any(
            not isinstance(value, str)
            for value in (*raw_platform_tags, *raw_supported_tags)
        ):
            raise EnvironmentInstallError(
                "interpreter_probe_failed",
                "Selected base Python probe returned invalid data",
            )
        if any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in marker_environment.items()
        ):
            raise EnvironmentInstallError(
                "interpreter_probe_failed",
                "Selected base Python probe returned invalid data",
            )
        target = _TargetPython(
            abi=abi,
            platform_tags=frozenset(raw_platform_tags),
            marker_environment=dict(marker_environment),
            supported_tags=frozenset(raw_supported_tags),
        )
        if target.abi != spec.python_abi:
            raise EnvironmentInstallError(
                "unsupported_platform",
                "Selected base Python ABI does not match environment spec",
            )
        if spec.platform_tag not in target.platform_tags:
            raise EnvironmentInstallError(
                "unsupported_platform",
                "Selected base Python platform does not match spec",
            )
        return target

    def _validate_selection(
        self,
        spec: EnvironmentSpecManifest,
        lock: LockFile,
    ) -> None:
        """Reject a lock that cannot satisfy the selected environment spec."""
        if lock.channel_key != spec.channel_key:
            raise EnvironmentInstallError(
                "lock_mismatch",
                "Lock channel does not match environment spec",
            )
        if lock.python_abi != spec.python_abi:
            raise EnvironmentInstallError(
                "lock_mismatch",
                "Lock Python ABI does not match environment spec",
            )
        if lock.platform_tag != spec.platform_tag:
            raise EnvironmentInstallError(
                "lock_mismatch",
                "Lock platform does not match environment spec",
            )
        if lock.condition_set_sha256 != spec.condition_set_sha256:
            raise EnvironmentInstallError(
                "lock_mismatch",
                "Lock conditions do not match environment spec",
            )
        if lock.sha256() != spec.lock_sha256:
            raise EnvironmentInstallError(
                "lock_mismatch",
                "Lock digest does not match environment spec",
            )

    def _ensure_spec_manifest(
        self,
        spec_directory: Path,
        spec: EnvironmentSpecManifest,
    ) -> None:
        """Validate the persisted spec manifest before adding an env."""
        manifest_path = spec_directory / "environment_spec.json"
        if not manifest_path.exists():
            raise EnvironmentInstallError(
                "spec_manifest_invalid",
                "Environment spec manifest is missing",
            )
        try:
            existing = EnvironmentSpecManifest.from_mapping(
                _read_json(manifest_path),
                allowed_platform_tags=self.allowed_platform_tags,
            )
        except (EnvironmentManifestError, ArtifactValidationError) as exc:
            raise EnvironmentInstallError(
                "spec_manifest_invalid",
                "Environment spec manifest is invalid",
            ) from exc
        if existing != spec:
            raise EnvironmentInstallError(
                "spec_manifest_mismatch",
                "Environment spec manifest does not match selection",
            )

    def _find_existing(
        self,
        spec_directory: Path,
        spec: EnvironmentSpecManifest,
        lock: LockFile,
    ) -> InstalledEnvironment | None:
        """Return one already-installed valid environment for this spec."""
        if not spec_directory.is_dir():
            return None
        for environment_directory in sorted(spec_directory.iterdir()):
            if (
                not environment_directory.is_dir()
                or environment_directory.name.startswith(".")
            ):
                continue
            install_path = environment_directory / "install.json"
            try:
                install = InstallManifest.from_mapping(
                    _read_json(install_path),
                    allowed_platform_tags=self.allowed_platform_tags,
                )
            except (
                OSError,
                EnvironmentManifestError,
                ArtifactValidationError,
            ):
                continue
            if install.environment_spec_id != spec.environment_spec_id:
                continue
            try:
                DirectoryIdentity.validate(
                    logical_id=install.environment_id,
                    directory_key=environment_directory.name,
                    manifest_logical_id=install.environment_id,
                )
            except Exception:
                continue
            interpreter = _venv_interpreter(environment_directory / "venv")
            result = validate_installed_environment(
                environment_directory=environment_directory,
                interpreter=interpreter,
                expected_spec=spec,
                expected_environment_id=install.environment_id,
                expected_lock=lock,
                allowed_platform_tags=self.allowed_platform_tags,
            )
            if result.valid:
                return InstalledEnvironment(
                    environment_id=install.environment_id,
                    environment_spec_id=spec.environment_spec_id,
                    environment_directory=environment_directory,
                    interpreter=interpreter,
                    manifest=install,
                    reused=True,
                )
        return None

    def _install_staged(  # pylint: disable=too-many-statements
        self,
        *,
        spec_directory: Path,
        spec: EnvironmentSpecManifest,
        lock: LockFile,
        source: DependencySource,
        wheel_cache: Path | None,
        base_python: Path,
        target_python: _TargetPython,
    ) -> InstalledEnvironment:
        """Build, validate, freeze and publish a new installation."""
        identity = EnvironmentIdentity.create(
            environment_spec_id=spec.environment_spec_id,
        )
        environment_name = dir_key(identity.environment_id)
        final_directory = spec_directory / environment_name
        if final_directory.exists():
            raise EnvironmentInstallError(
                "environment_exists",
                "Generated environment directory already exists",
            )
        publish_whole_spec = not spec_directory.exists()

        staging_spec_directory = (
            self.environments_root
            / _staging_sibling_name(
                spec_directory.name,
            )
        )
        staging_parent = staging_spec_directory
        staging_directory = staging_spec_directory / environment_name
        venv_root = staging_directory / "venv"
        wheel_directory = staging_parent / "wheels"
        try:
            staging_spec_directory.mkdir(parents=True, exist_ok=False)
            _write_canonical_atomic(
                staging_spec_directory / "environment_spec.json",
                spec.to_mapping(),
            )
            staging_directory.mkdir()
            wheel_paths = self._prepare_wheels(
                lock=lock,
                source=source,
                wheel_cache=wheel_cache,
                wheel_directory=wheel_directory,
                target_python=target_python,
            )
            self._create_venv(base_python, venv_root)
            interpreter = _venv_interpreter(venv_root)
            self._install_wheels(interpreter, wheel_paths)
            site_packages = _site_packages(interpreter)
            active_packages = _active_packages(lock, target_python)
            self._remove_bootstrap_distributions(
                site_packages=site_packages,
                expected_names=set(active_packages),
            )
            self._write_direct_url_metadata(
                site_packages=site_packages,
                lock=lock,
            )
            if not _cleanup_path(wheel_directory):
                raise EnvironmentInstallError(
                    "cleanup_failed",
                    "Wheel staging cleanup failed",
                )
            _rewrite_venv_paths(
                venv_root=venv_root,
                old_root=venv_root,
                new_root=final_directory / "venv",
            )
            _refresh_venv_records(venv_root)
            provenance = tuple(
                WheelProvenance(
                    name=package.name,
                    version=package.version,
                    wheel_filename=wheel.filename,
                    wheel_sha256=wheel.sha256,
                )
                for package, wheel, _ in wheel_paths
            )
            _write_canonical_atomic(
                staging_directory / "dependency.lock",
                lock.to_mapping(),
            )
            install = InstallManifest(
                environment_id=identity.environment_id,
                environment_spec_id=spec.environment_spec_id,
                python_abi=spec.python_abi,
                platform_tag=spec.platform_tag,
                lock_sha256=spec.lock_sha256,
                venv_tree_sha256=compute_venv_tree_sha256(venv_root),
                source=InstallSource(
                    kind=source.kind,
                    base_url=source.base_url,
                ),
                packages=provenance,
            )
            _write_canonical_atomic(
                staging_directory / "install.json",
                install.to_mapping(),
            )
            _make_read_only(staging_directory)
            validation = validate_installed_environment(
                environment_directory=staging_directory,
                interpreter=interpreter,
                expected_spec=spec,
                expected_environment_id=identity.environment_id,
                expected_lock=lock,
                allowed_platform_tags=self.allowed_platform_tags,
                allow_staging_directory=True,
            )
            if not validation.valid:
                reason = (
                    validation.reasons[0]
                    if validation.reasons
                    else ("Strict environment validation failed")
                )
                raise EnvironmentInstallError("validation_failed", reason)
            if final_directory.exists():
                raise EnvironmentInstallError(
                    "environment_exists",
                    "Environment directory appeared during publication",
                )
            if not publish_whole_spec:
                os.replace(staging_directory, final_directory)
            else:
                if spec_directory.exists():
                    raise EnvironmentInstallError(
                        "environment_exists",
                        "Environment spec directory appeared during publish",
                    )
                os.replace(staging_spec_directory, spec_directory)
            _fsync_directory(spec_directory)
            return InstalledEnvironment(
                environment_id=identity.environment_id,
                environment_spec_id=spec.environment_spec_id,
                environment_directory=final_directory,
                interpreter=_venv_interpreter(final_directory / "venv"),
                manifest=install,
            )
        except subprocess.TimeoutExpired as exc:
            raise EnvironmentInstallError(
                "installer_timeout",
                "Environment installation timed out",
            ) from exc
        except OSError as exc:
            code = (
                "disk_full"
                if getattr(exc, "errno", None) == 28
                else "io_error"
            )
            raise EnvironmentInstallError(
                code,
                "Environment installation filesystem operation failed",
            ) from exc
        finally:
            _cleanup_path(staging_parent)

    def _prepare_wheels(
        self,
        *,
        lock: LockFile,
        source: DependencySource,
        wheel_cache: Path | None,
        wheel_directory: Path,
        target_python: _TargetPython,
    ) -> tuple[tuple[LockPackage, WheelFile, Path], ...]:
        """Materialize exactly one hash-matching wheel per active package."""
        active = _active_packages(lock, target_python)
        wheel_directory.mkdir(parents=True, exist_ok=True)
        result: list[tuple[LockPackage, WheelFile, Path]] = []
        for package in active.values():
            wheel = _select_wheel(package, target_python)
            destination = wheel_directory / wheel.filename
            self._materialize_wheel(
                package=package,
                wheel=wheel,
                destination=destination,
                source=source,
                wheel_cache=wheel_cache,
            )
            result.append((package, wheel, destination))
        return tuple(result)

    def _materialize_wheel(
        self,
        *,
        package: LockPackage,
        wheel: WheelFile,
        destination: Path,
        source: DependencySource,
        wheel_cache: Path | None,
    ) -> None:
        """Copy or download one wheel and verify its declared digest."""
        filename = wheel.filename
        expected_digest = wheel.sha256
        if wheel_cache is not None:
            cached = _find_cached_wheel(wheel_cache, filename)
            if cached is not None:
                if _sha256(cached) == expected_digest:
                    _copy_file(cached, destination)
                    return
                if source.kind == "offline":
                    raise EnvironmentInstallError(
                        "wheel_hash_mismatch",
                        f"Cached wheel hash mismatches for {package.name}",
                    )
        if source.kind == "offline":
            raise EnvironmentInstallError(
                "wheel_missing",
                f"No cached wheel is available for {package.name}",
            )
        url = wheel.url
        if url is None:
            url = self._find_simple_index_wheel(
                package.name,
                filename,
                source,
            )
        self._download_wheel(
            url=url,
            destination=destination,
            expected_digest=expected_digest,
            source=source,
        )

    def _find_simple_index_wheel(
        self,
        package_name: str,
        filename: str,
        source: DependencySource,
    ) -> str:
        """Resolve one exact filename from the selected simple index only."""
        assert source.base_url is not None
        normalized_name = package_name.replace("_", "-").lower()
        index_url = urljoin(
            source.base_url,
            f"{normalized_name}/",
        )
        request = Request(
            index_url,
            headers=self._request_headers(index_url, source),
        )
        try:
            with self._url_opener.open(
                request,
                timeout=self.subprocess_timeout,
            ) as response:
                page = response.read()
        except (HTTPError, URLError, OSError) as exc:
            raise EnvironmentInstallError(
                "source_unavailable",
                f"Selected dependency source could not provide {package_name}",
            ) from exc
        parser = _SimpleIndexParser()
        try:
            parser.feed(page.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise EnvironmentInstallError(
                "source_invalid",
                "Selected dependency source returned invalid index "
                f"for {package_name}",
            ) from exc
        for href in parser.links:
            candidate = urljoin(index_url, href)
            parts = urlsplit(candidate)
            if parts.username or parts.password:
                continue
            candidate_name = unquote(Path(parts.path).name)
            if candidate_name == filename:
                return candidate
        raise EnvironmentInstallError(
            "wheel_missing",
            "Selected dependency source has no matching wheel "
            f"for {package_name}",
        )

    def _download_wheel(
        self,
        *,
        url: str,
        destination: Path,
        expected_digest: str,
        source: DependencySource,
    ) -> None:
        """Download one wheel without exposing source credentials."""
        request = Request(
            url,
            headers=self._request_headers(url, source),
        )
        temporary: Path | None = None
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary_name = tempfile.mkstemp(
                prefix=f".{destination.name}.",
                suffix=".tmp",
                dir=str(destination.parent),
            )
            temporary = Path(temporary_name)
            with os.fdopen(fd, "wb") as output:
                with self._url_opener.open(
                    request,
                    timeout=self.subprocess_timeout,
                ) as response:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if _sha256(temporary) != expected_digest:
                raise EnvironmentInstallError(
                    "wheel_hash_mismatch",
                    "Downloaded wheel hash does not match the lock",
                )
            os.replace(temporary, destination)
            temporary = None
        except (HTTPError, URLError, OSError) as exc:
            code = (
                "disk_full"
                if getattr(exc, "errno", None) == 28
                else ("source_unavailable")
            )
            raise EnvironmentInstallError(
                code,
                "Selected dependency source could not download the wheel",
            ) from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _request_headers(
        self,
        url: str,
        source: DependencySource,
    ) -> dict[str, str]:
        """Build request headers while keeping secret values in memory only."""
        headers = {"User-Agent": "QwenPaw-Channel-Installer/1"}
        if source.credential_ref is None:
            return headers
        if source.base_url is None:
            return headers
        if _url_origin(url) != _url_origin(source.base_url):
            return headers
        if self.credential_resolver is None:
            raise EnvironmentInstallError(
                "credential_unavailable",
                "Credential reference could not be resolved",
            )
        try:
            resolved = self.credential_resolver(source.credential_ref)
        except Exception as exc:
            raise EnvironmentInstallError(
                "credential_unavailable",
                "Credential reference could not be resolved",
            ) from exc
        if isinstance(resolved, str):
            if not resolved:
                raise EnvironmentInstallError(
                    "credential_unavailable",
                    "Credential reference resolved to an empty value",
                )
            headers["Authorization"] = f"Bearer {resolved}"
            return headers
        if not isinstance(resolved, Mapping):
            raise EnvironmentInstallError(
                "credential_unavailable",
                "Credential reference returned an invalid value",
            )
        authorization = resolved.get("authorization") or resolved.get("token")
        if authorization:
            value = str(authorization)
            headers["Authorization"] = (
                value
                if value.lower().startswith("bearer ")
                else f"Bearer {value}"
            )
            return headers
        username = resolved.get("username")
        password = resolved.get("password")
        if username is not None and password is not None:
            encoded = base64.b64encode(
                f"{username}:{password}".encode("utf-8"),
            ).decode("ascii")
            headers["Authorization"] = f"Basic {encoded}"
            return headers
        raise EnvironmentInstallError(
            "credential_unavailable",
            "Credential reference returned no supported authentication",
        )

    def _create_venv(self, base_python: Path, venv_root: Path) -> None:
        """Create a fresh venv using only the selected base interpreter."""
        try:
            result = subprocess.run(
                [str(base_python), "-I", "-m", "venv", str(venv_root)],
                capture_output=True,
                check=False,
                env=_clean_subprocess_env(),
                text=True,
                timeout=self.subprocess_timeout,
            )
            if result.returncode != 0:
                raise EnvironmentInstallError(
                    "venv_failed",
                    "Selected base Python could not create a venv",
                )
        except (OSError, subprocess.SubprocessError) as exc:
            raise EnvironmentInstallError(
                "venv_failed",
                "Selected base Python could not create a venv",
            ) from exc

    def _install_wheels(
        self,
        interpreter: Path,
        wheels: tuple[tuple[LockPackage, WheelFile, Path], ...],
    ) -> None:
        """Install local wheel paths with dependency resolution disabled."""
        if not wheels:
            return
        command = [
            str(interpreter),
            "-I",
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-input",
            "--no-cache-dir",
            "--no-deps",
            "--no-index",
            "--only-binary=:all:",
        ]
        command.extend(str(wheel_path) for _, _, wheel_path in wheels)
        result = subprocess.run(
            command,
            capture_output=True,
            check=False,
            env=_clean_subprocess_env(),
            text=True,
            timeout=self.subprocess_timeout,
        )
        if result.returncode != 0:
            raise EnvironmentInstallError(
                "pip_failed",
                "pip failed to install the locked wheels",
            )

    def _remove_bootstrap_distributions(
        self,
        *,
        site_packages: Path,
        expected_names: set[str],
    ) -> None:
        """Remove pip/setuptools and every other non-locked distribution."""
        distributions = tuple(
            metadata.distributions(path=[str(site_packages)]),
        )
        expected_paths: set[Path] = set()
        for distribution in distributions:
            name = _distribution_name(distribution)
            if name not in expected_names:
                continue
            for package_path in distribution.files or ():
                located = _safe_site_path(
                    Path(distribution.locate_file(package_path)),
                    site_packages,
                )
                if located is not None:
                    expected_paths.add(located)
        for distribution in distributions:
            name = _distribution_name(distribution)
            if name in expected_names:
                continue
            files = distribution.files or ()
            for package_path in files:
                located = _safe_site_path(
                    Path(distribution.locate_file(package_path)),
                    site_packages,
                )
                if located is None or located in expected_paths:
                    continue
                try:
                    if located.is_file() or located.is_symlink():
                        located.unlink()
                        _remove_empty_parents(located.parent, site_packages)
                except OSError as exc:
                    raise EnvironmentInstallError(
                        "cleanup_failed",
                        "Bootstrap distribution cleanup failed",
                    ) from exc
            _remove_empty_parents(
                Path(getattr(distribution, "_path", site_packages)),
                site_packages,
            )

    def _write_direct_url_metadata(
        self,
        *,
        site_packages: Path,
        lock: LockFile,
    ) -> None:
        """Make direct_url.json agree with a direct URL in the lock."""
        direct_urls = _direct_urls(lock)
        for name, url in direct_urls.items():
            try:
                distribution = next(
                    distribution
                    for distribution in metadata.distributions(
                        path=[str(site_packages)],
                    )
                    if _distribution_name(distribution) == name
                )
            except StopIteration as exc:
                raise EnvironmentInstallError(
                    "pip_failed",
                    f"Locked distribution is missing: {name}",
                ) from exc
            dist_info = Path(getattr(distribution, "_path", ""))
            if not dist_info.is_dir():
                raise EnvironmentInstallError(
                    "pip_failed",
                    f"Distribution metadata is missing: {name}",
                )
            direct_path = dist_info / "direct_url.json"
            direct_path.write_text(
                json.dumps({"url": url}, separators=(",", ":")),
                encoding="utf-8",
            )
            _refresh_record_row(
                record_path=dist_info / "RECORD",
                relative=direct_path.relative_to(site_packages).as_posix(),
                content=direct_path.read_bytes(),
            )


def install_environment(
    *,
    environments_root: Path,
    spec: EnvironmentSpecManifest,
    lock: LockFile,
    source: DependencySource | None = None,
    wheel_cache: Path | None = None,
    base_python: Path | None = None,
    credential_resolver: CredentialResolver | None = None,
    allowed_platform_tags: Collection[str] = RELEASE_TARGET_PLATFORM_TAGS,
) -> InstalledEnvironment:
    """Install one environment through the default installer facade."""
    return EnvironmentInstaller(
        environments_root,
        allowed_platform_tags=allowed_platform_tags,
        base_python=base_python,
        wheel_cache=wheel_cache,
        credential_resolver=credential_resolver,
    ).install(
        spec=spec,
        lock=lock,
        source=source,
        wheel_cache=wheel_cache,
        base_python=base_python,
    )


def _active_packages(
    lock: LockFile,
    target_python: _TargetPython,
) -> dict[str, LockPackage]:
    """Return lock packages applicable to the target marker domain."""
    environment = target_python.marker_environment
    return {
        package.name: package
        for package in lock.packages
        if package.marker is None
        or _marker_matches(package.marker, environment)
    }


def _marker_matches(marker: str, environment: Mapping[str, str]) -> bool:
    """Evaluate one validated marker without exposing parser details."""
    from packaging.markers import Marker

    return Marker(marker).evaluate(environment=environment)


def _select_wheel(
    package: LockPackage,
    target_python: _TargetPython,
) -> WheelFile:
    """Select one wheel compatible with the target interpreter."""
    for wheel in package.wheels:
        try:
            _, _, _, wheel_tags = parse_wheel_filename(wheel.filename)
        except (ValueError, TypeError):
            continue
        if any(str(tag) in target_python.supported_tags for tag in wheel_tags):
            return wheel
    raise EnvironmentInstallError(
        "unsupported_platform",
        f"No compatible locked wheel is available for {package.name}",
    )


def _venv_interpreter(venv_root: Path) -> Path:
    """Return the platform-specific interpreter path inside a venv."""
    if os.name == "nt":
        return venv_root / "Scripts" / "python.exe"
    return venv_root / "bin" / "python"


def _site_packages(interpreter: Path) -> Path:
    """Find the selected venv's purelib directory without ambient imports."""
    result = subprocess.run(
        [
            str(interpreter),
            "-I",
            "-c",
            "import sysconfig; print(sysconfig.get_paths()['purelib'])",
        ],
        capture_output=True,
        check=False,
        env=_clean_subprocess_env(),
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise EnvironmentInstallError(
            "venv_failed",
            "Unable to locate the venv site-packages directory",
        )
    site_packages = Path(result.stdout.strip()).resolve()
    if not site_packages.is_dir():
        raise EnvironmentInstallError(
            "venv_failed",
            "The venv site-packages directory is missing",
        )
    return site_packages


def _distribution_name(distribution: metadata.Distribution) -> str:
    """Return one canonical distribution name or an empty invalid marker."""
    value = distribution.metadata.get("Name")
    if not value:
        return ""
    try:
        return canonicalize_name(value, validate=True)
    except ValueError:
        return ""


def _safe_site_path(path: Path, site_packages: Path) -> Path | None:
    """Resolve a distribution file while keeping it inside site-packages."""
    try:
        lexical = Path(os.path.abspath(path))
        lexical.relative_to(Path(os.path.abspath(site_packages)))
    except ValueError:
        return None
    return lexical


def _remove_empty_parents(path: Path, root: Path) -> None:
    """Remove empty distribution directories without leaving the site root."""
    current = path
    root = root.resolve()
    while current != root and current.exists():
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def _direct_urls(lock: LockFile) -> dict[str, str]:
    """Extract direct URL requirements from a validated lock."""
    from packaging.requirements import Requirement

    result: dict[str, str] = {}
    for requirement_text in lock.direct_requirements:
        requirement = Requirement(requirement_text)
        if requirement.url is not None:
            result[canonicalize_name(requirement.name)] = requirement.url
    return result


def _refresh_record_row(
    *,
    record_path: Path,
    relative: str,
    content: bytes,
) -> None:
    """Refresh one RECORD row after direct_url.json is rewritten."""
    if not record_path.is_file():
        raise EnvironmentInstallError(
            "pip_failed",
            "Installed distribution RECORD is missing",
        )
    digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest())
    encoded = digest.rstrip(b"=").decode("ascii")
    rows: list[list[str]] = []
    with record_path.open(encoding="utf-8", newline="") as record:
        rows = list(csv.reader(record))
    replacement = [relative, f"sha256={encoded}", str(len(content))]
    for index, row in enumerate(rows):
        if row and row[0] == relative:
            rows[index] = replacement
            break
    else:
        rows.append(replacement)
    with record_path.open("w", encoding="utf-8", newline="") as record:
        csv.writer(record, lineterminator="\n").writerows(rows)


def _find_cached_wheel(cache: Path, filename: str) -> Path | None:
    """Find one regular cached wheel by its exact filename."""
    if not cache.is_dir():
        return None
    for candidate in sorted(cache.rglob(filename)):
        if candidate.is_file() and not candidate.is_symlink():
            return candidate
    return None


def _copy_file(source: Path, destination: Path) -> None:
    """Copy one wheel through a temporary sibling and sync its bytes."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=str(destination.parent),
        )
        temporary = Path(temporary_name)
        with os.fdopen(fd, "wb") as output, source.open("rb") as input_file:
            shutil.copyfileobj(input_file, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _sha256(path: Path) -> str:
    """Return a file's SHA-256 digest."""
    digest = hashlib.sha256()
    with path.open("rb") as input_file:
        for chunk in iter(lambda: input_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> object:
    """Read one UTF-8 JSON object for a local manifest."""
    return json.loads(path.read_text(encoding="utf-8"))


def _write_canonical_atomic(path: Path, value: Mapping[str, object]) -> None:
    """Write canonical JSON atomically beside its final destination."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=str(path.parent),
        )
        temporary = Path(temporary_name)
        with os.fdopen(fd, "wb") as output:
            output.write(canonical_json(value))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _clean_subprocess_env() -> dict[str, str]:
    """Remove ambient Python import and pip configuration from subprocesses."""
    environment = dict(os.environ)
    for key in tuple(environment):
        if key == "PYTHONPATH" or key.startswith("PIP_"):
            environment.pop(key, None)
    environment["PYTHONNOUSERSITE"] = "1"
    return environment


def _url_origin(url: str) -> tuple[str, str, int] | None:
    """Return a normalized HTTP origin including its effective port."""
    try:
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            return None
        port = parts.port
    except ValueError:
        return None
    effective_port = port or (443 if parts.scheme == "https" else 80)
    return parts.scheme.lower(), parts.hostname.lower(), effective_port


def _path_inside(path: Path, root: Path) -> bool:
    """Check one resolved path remains inside a resolved root."""
    try:
        path.relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _staging_sibling_name(spec_name: str) -> str:
    """Return one hidden staging sibling name for a spec directory."""
    prefix = f".{spec_name[:5]}.staging-"
    if len(prefix) >= len(spec_name):
        raise EnvironmentInstallError(
            "staging_failed",
            "Environment directory name is too short for staging",
        )
    suffix_length = len(spec_name) - len(prefix)
    return prefix + secrets.token_hex((suffix_length + 1) // 2)[:suffix_length]


def _rewrite_venv_paths(
    *,
    venv_root: Path,
    old_root: Path,
    new_root: Path,
) -> None:
    """Rewrite interpreter paths embedded in generated venv launchers."""
    replacements = (
        (str(old_root), str(new_root)),
        (old_root.as_posix(), new_root.as_posix()),
        (str(old_root).replace("/", "\\"), str(new_root).replace("/", "\\")),
    )
    if any(
        len(old_value) != len(new_value)
        for old_value, new_value in replacements
    ):
        raise EnvironmentInstallError(
            "launcher_rewrite_failed",
            "Staging and canonical venv paths have different lengths",
        )
    scripts_root = venv_root / ("Scripts" if os.name == "nt" else "bin")
    if not scripts_root.is_dir():
        raise EnvironmentInstallError(
            "launcher_rewrite_failed",
            "Generated venv scripts directory is missing",
        )
    for path in scripts_root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise EnvironmentInstallError(
                "launcher_rewrite_failed",
                "Generated venv launcher could not be read",
            ) from exc
        updated = content
        for old_value, new_value in replacements:
            updated = updated.replace(
                old_value.encode("utf-8"),
                new_value.encode("utf-8"),
            )
            updated = updated.replace(
                old_value.encode("utf-16-le"),
                new_value.encode("utf-16-le"),
            )
        if updated == content:
            continue
        try:
            path.write_bytes(updated)
        except OSError as exc:
            raise EnvironmentInstallError(
                "launcher_rewrite_failed",
                "Generated venv launcher could not be rewritten",
            ) from exc


def _refresh_venv_records(venv_root: Path) -> None:
    """Refresh RECORD rows for launchers rewritten before publication."""
    for record_path in venv_root.rglob("RECORD"):
        try:
            with record_path.open(encoding="utf-8", newline="") as record:
                rows = list(csv.reader(record))
            changed = False
            for row in rows:
                if not row or len(row) < 3 or not row[0] or not row[1]:
                    continue
                candidate = (record_path.parent.parent / row[0]).resolve()
                if (
                    not _path_inside(candidate, venv_root)
                    or not candidate.is_file()
                ):
                    continue
                digest = (
                    base64.urlsafe_b64encode(
                        hashlib.sha256(candidate.read_bytes()).digest(),
                    )
                    .rstrip(b"=")
                    .decode("ascii")
                )
                replacement = [
                    row[0],
                    f"sha256={digest}",
                    str(candidate.stat().st_size),
                ]
                if row != replacement:
                    row[:] = replacement
                    changed = True
            if changed:
                with record_path.open(
                    "w",
                    encoding="utf-8",
                    newline="",
                ) as record:
                    csv.writer(record, lineterminator="\n").writerows(rows)
        except (OSError, UnicodeError, csv.Error) as exc:
            raise EnvironmentInstallError(
                "launcher_rewrite_failed",
                "Generated venv RECORD could not be refreshed",
            ) from exc


def _make_read_only(root: Path) -> None:
    """Make an environment tree non-writable on supported operating systems."""
    paths = sorted(
        root.rglob("*"),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for path in paths:
        if path.is_symlink():
            continue
        if os.name == "nt":
            mode = stat.S_IREAD
        elif path.is_dir():
            mode = 0o555
        else:
            current_mode = stat.S_IMODE(path.stat().st_mode)
            mode = 0o555 if current_mode & 0o111 else 0o444
        try:
            os.chmod(path, mode)
        except OSError as exc:
            raise EnvironmentInstallError(
                "readonly_failed",
                "Unable to make the environment immutable",
            ) from exc
    try:
        os.chmod(root, stat.S_IREAD if os.name == "nt" else 0o555)
    except OSError as exc:
        raise EnvironmentInstallError(
            "readonly_failed",
            "Unable to make the environment immutable",
        ) from exc


def _cleanup_path(path: Path) -> bool:
    """Remove staging data with bounded retries and orphan diagnostics."""
    if not path.exists():
        return True
    for child in sorted(path.rglob("*"), reverse=True):
        if child.is_symlink():
            continue
        try:
            os.chmod(child, 0o700 if child.is_dir() else 0o600)
        except OSError:
            pass
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    for attempt in range(_CLEANUP_RETRIES):
        try:
            shutil.rmtree(path)
            return True
        except OSError as exc:
            if attempt + 1 == _CLEANUP_RETRIES:
                _LOGGER.warning(
                    "orphan_staging path=%s error=%s",
                    path,
                    exc,
                )
                return False
            time.sleep(_CLEANUP_RETRY_DELAY_SECONDS)
    return False


def _fsync_directory(path: Path) -> None:
    """Best-effort directory sync where the platform exposes it."""
    if os.name == "nt":
        return
    try:
        descriptor = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)
