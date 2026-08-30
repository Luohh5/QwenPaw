# -*- coding: utf-8 -*-
"""Install immutable, lock-pinned Channel dependency environments."""

from __future__ import annotations

import base64
import csv
from dataclasses import dataclass
from html.parser import HTMLParser
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Collection, Mapping
from typing import Callable, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urljoin, urlsplit
from urllib.request import Request, urlopen

from packaging.markers import default_environment
from packaging.tags import sys_tags
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

        spec_directory = self.environments_root / dir_key(
            spec.environment_spec_id,
        )
        lock_path = spec_directory / ".install.lock"
        with plugin_install_lock(
            lock_path,
            timeout=self.lock_timeout,
        ) as acquired:
            if not acquired:
                raise EnvironmentInstallError(
                    "install_lock_timeout",
                    "Timed out waiting for the environment install lock",
                )
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
            )

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
        """Publish the immutable spec manifest before any environment child."""
        spec_directory.mkdir(parents=True, exist_ok=True)
        manifest_path = spec_directory / "environment_spec.json"
        if manifest_path.exists():
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
            return
        _write_canonical_atomic(manifest_path, spec.to_mapping())

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

        staging_parent = self.environments_root / (
            f".{spec_directory.name}.staging-{secrets.token_hex(12)}"
        )
        staging_spec_directory = staging_parent / spec_directory.name
        staging_directory = staging_spec_directory / environment_name
        venv_root = staging_directory / "venv"
        wheel_directory = staging_parent / "wheels"
        published = False
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
            )
            self._create_venv(base_python, venv_root)
            interpreter = _venv_interpreter(venv_root)
            self._install_wheels(interpreter, wheel_paths)
            site_packages = _site_packages(interpreter)
            active_packages = _active_packages(lock)
            self._remove_bootstrap_distributions(
                site_packages=site_packages,
                expected_names=set(active_packages),
            )
            self._write_direct_url_metadata(
                site_packages=site_packages,
                lock=lock,
            )
            _cleanup_path(wheel_directory)
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
            validation = validate_installed_environment(
                environment_directory=staging_directory,
                interpreter=interpreter,
                expected_spec=spec,
                expected_environment_id=identity.environment_id,
                expected_lock=lock,
                allowed_platform_tags=self.allowed_platform_tags,
            )
            if not validation.valid:
                reason = (
                    validation.reasons[0]
                    if validation.reasons
                    else ("Strict environment validation failed")
                )
                raise EnvironmentInstallError("validation_failed", reason)
            spec_directory.mkdir(parents=True, exist_ok=True)
            if final_directory.exists():
                raise EnvironmentInstallError(
                    "environment_exists",
                    "Environment directory appeared during publication",
                )
            os.replace(staging_directory, final_directory)
            _fsync_directory(spec_directory)
            _make_read_only(final_directory)
            published = True
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
            if not published:
                _cleanup_path(staging_parent)
                if final_directory.exists():
                    _cleanup_path(final_directory)
            else:
                _cleanup_path(staging_parent)

    def _prepare_wheels(
        self,
        *,
        lock: LockFile,
        source: DependencySource,
        wheel_cache: Path | None,
        wheel_directory: Path,
    ) -> tuple[tuple[LockPackage, WheelFile, Path], ...]:
        """Materialize exactly one hash-matching wheel per active package."""
        active = _active_packages(lock)
        wheel_directory.mkdir(parents=True, exist_ok=True)
        result: list[tuple[LockPackage, WheelFile, Path]] = []
        for package in active.values():
            wheel = _select_wheel(package)
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
            with urlopen(request, timeout=self.subprocess_timeout) as response:
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
                with urlopen(
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
        if urlsplit(url).hostname != urlsplit(source.base_url).hostname:
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


def _active_packages(lock: LockFile) -> dict[str, LockPackage]:
    """Return lock packages applicable to this interpreter's marker domain."""
    environment = default_environment()
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


def _select_wheel(package: LockPackage):
    """Select one wheel compatible with the current interpreter."""
    supported = frozenset(sys_tags())
    for wheel in package.wheels:
        try:
            _, _, _, wheel_tags = parse_wheel_filename(wheel.filename)
        except (ValueError, TypeError):
            continue
        if supported.intersection(wheel_tags):
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


def _cleanup_path(path: Path) -> None:
    """Best-effort removal of failed or completed staging data."""
    if not path.exists():
        return
    for child in sorted(path.rglob("*"), reverse=True):
        if child.is_symlink():
            continue
        try:
            os.chmod(child, 0o700 if child.is_dir() else 0o600)
        except OSError:
            pass
    try:
        shutil.rmtree(path)
    except OSError:
        pass


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
