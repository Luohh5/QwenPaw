# -*- coding: utf-8 -*-
"""Tests for CH-1-002 environment selection and strict validation."""

from __future__ import annotations

import base64
import csv
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import venv
import zipfile

import pytest
from packaging.tags import sys_tags

from qwenpaw.app.channels.env_manager import (
    compute_venv_tree_sha256,
    EnvironmentManifestError,
    EnvironmentSpecManifest,
    InstallManifest,
    InstallSource,
    WheelProvenance,
    select_environment_spec,
    validate_installed_environment,
)
from qwenpaw.channel_protocol import (
    ArtifactValidationError,
    ChannelDescriptor,
    EnvironmentIdentity,
    InstallationIdentity,
    LockFile,
    LockManifest,
    LockPackage,
    RELEASE_TARGET_PLATFORM_TAGS,
    condition_set_sha256,
    current_python_abi,
    dir_key,
    write_canonical_json,
)

CURRENT_PLATFORM_TAGS = frozenset(tag.platform for tag in sys_tags())
PLATFORM_TAG = next(
    tag
    for tag in sorted(RELEASE_TARGET_PLATFORM_TAGS)
    if tag in CURRENT_PLATFORM_TAGS
)
INCOMPATIBLE_PLATFORM_TAG = next(
    tag
    for tag in sorted(RELEASE_TARGET_PLATFORM_TAGS)
    if tag not in CURRENT_PLATFORM_TAGS
)
WHEEL_FILENAME = "demo_package-1.0-py3-none-any.whl"
WHEEL_DIGEST = "a" * 64
DIRECT_URL = "https://example.invalid/demo-package.whl"


def _descriptor(platform_tag: str = PLATFORM_TAG) -> ChannelDescriptor:
    """Build a one-target descriptor with one finite condition value."""
    return ChannelDescriptor.from_mapping(
        {
            "schema_version": 1,
            "channel_key": "fixture",
            "source_kind": "builtin",
            "process_mode": "runner_process",
            "dispatch_mode": "manager_queue",
            "ingress_owner": "none",
            "label": {"en": "Fixture", "zh": "Fixture"},
            "description": {"en": "", "zh": ""},
            "icon": "",
            "doc_url": "",
            "plugin_metadata": None,
            "entrypoint": {
                "scope": "runner",
                "module": "fixture_runner",
                "qualname": "FixtureDriver",
            },
            "config_fields": [
                {
                    "name": "region",
                    "label": "Region",
                    "help": "",
                    "placeholder": "",
                    "type": "select",
                    "required": True,
                    "nullable": False,
                    "default": "asia",
                    "allowed_values": ["asia"],
                    "secret": False,
                    "condition": True,
                },
            ],
            "core_requirements": [],
            "isolated_requirements": [
                f"demo-package @ {DIRECT_URL}",
            ],
            "condition_fields": ["region"],
            "supported_python_abis": [current_python_abi()],
            "supported_platform_tags": [platform_tag],
            "capabilities": [],
            "bot_identity_fields": [],
            "environment_passthrough_allowlist": [],
        },
        allowed_platform_tags=RELEASE_TARGET_PLATFORM_TAGS,
    )


def _package() -> LockPackage:
    """Build one exact direct package with a selected wheel candidate."""
    return LockPackage.from_mapping(
        {
            "name": "demo-package",
            "version": "1.0",
            "marker": None,
            "direct": True,
            "wheels": [
                {
                    "filename": WHEEL_FILENAME,
                    "url": DIRECT_URL,
                    "sha256": WHEEL_DIGEST,
                },
            ],
        },
    )


def _release(
    tmp_path: Path,
    *,
    platform_tag: str = PLATFORM_TAG,
) -> tuple[ChannelDescriptor, LockFile, LockManifest, Path]:
    """Write one exact release lock and return its validated models."""
    descriptor = _descriptor(platform_tag)
    conditions = descriptor.condition_set({"region": "asia"})
    lock = LockFile.from_mapping(
        {
            "schema_version": 1,
            "channel_key": descriptor.channel_key,
            "python_abi": current_python_abi(),
            "platform_tag": platform_tag,
            "condition_set_sha256": condition_set_sha256(conditions),
            "direct_requirements": [f"demo-package @ {DIRECT_URL}"],
            "packages": [_package().to_mapping()],
        },
        allowed_platform_tags=RELEASE_TARGET_PLATFORM_TAGS,
    )
    manifest = LockManifest.from_lock_files(descriptor, [lock])
    code_root = tmp_path / "code"
    entry = manifest.locks[0]
    write_canonical_json(code_root / entry.lock_path, lock.to_mapping())
    return descriptor, lock, manifest, code_root


def _selection(
    tmp_path: Path,
    *,
    platform_tag: str = PLATFORM_TAG,
):
    descriptor, lock, manifest, code_root = _release(
        tmp_path,
        platform_tag=platform_tag,
    )
    result = select_environment_spec(
        descriptor=descriptor,
        effective_config={"region": "asia"},
        manifest=manifest,
        code_root=code_root,
        python_abi=current_python_abi(),
        platform_tag=platform_tag,
        allowed_platform_tags=RELEASE_TARGET_PLATFORM_TAGS,
    )
    assert result.selected
    assert result.environment_spec is not None
    assert result.lock == lock
    return result.environment_spec, lock


def test_selects_one_exact_environment_spec(tmp_path: Path) -> None:
    """The target tuple and effective config select one stable spec."""
    descriptor, lock, manifest, code_root = _release(tmp_path)

    first = select_environment_spec(
        descriptor=descriptor,
        effective_config={"region": "asia", "unrelated": "first"},
        manifest=manifest,
        code_root=code_root,
        python_abi=current_python_abi(),
        platform_tag=PLATFORM_TAG,
        allowed_platform_tags=RELEASE_TARGET_PLATFORM_TAGS,
    )
    second = select_environment_spec(
        descriptor=descriptor,
        effective_config={"region": "asia", "unrelated": "second"},
        manifest=manifest,
        code_root=code_root,
        python_abi=current_python_abi(),
        platform_tag=PLATFORM_TAG,
        allowed_platform_tags=RELEASE_TARGET_PLATFORM_TAGS,
    )

    assert first.selected
    assert first.error_code is None
    assert first.lock == lock
    assert first.environment_spec == second.environment_spec


@pytest.mark.parametrize(
    ("config", "python_abi", "platform_tag", "expected"),
    [
        ({}, current_python_abi(), PLATFORM_TAG, "config_invalid"),
        (
            {"region": "invalid"},
            current_python_abi(),
            PLATFORM_TAG,
            "config_invalid",
        ),
        (
            {"region": "asia"},
            "cp399-cp399",
            PLATFORM_TAG,
            "unsupported_platform",
        ),
        (
            {"region": "asia"},
            current_python_abi(),
            "win_amd64",
            "unsupported_platform",
        ),
    ],
)
def test_selection_returns_stable_failure_codes(
    tmp_path: Path,
    config: dict[str, object],
    python_abi: str,
    platform_tag: str,
    expected: str,
) -> None:
    """Invalid config and unsupported targets remain distinguishable."""
    descriptor, _, manifest, code_root = _release(tmp_path)

    result = select_environment_spec(
        descriptor=descriptor,
        effective_config=config,
        manifest=manifest,
        code_root=code_root,
        python_abi=python_abi,
        platform_tag=platform_tag,
        allowed_platform_tags=RELEASE_TARGET_PLATFORM_TAGS,
    )

    assert not result.selected
    assert result.error_code == expected


def test_selection_rejects_a_changed_lock(tmp_path: Path) -> None:
    """The selected lock bytes must match the release manifest digest."""
    descriptor, lock, manifest, code_root = _release(tmp_path)
    entry = manifest.locks[0]
    changed = lock.to_mapping()
    changed["packages"] = []
    write_canonical_json(code_root / entry.lock_path, changed)

    with pytest.raises(ArtifactValidationError):
        select_environment_spec(
            descriptor=descriptor,
            effective_config={"region": "asia"},
            manifest=manifest,
            code_root=code_root,
            python_abi=current_python_abi(),
            platform_tag=PLATFORM_TAG,
            allowed_platform_tags=RELEASE_TARGET_PLATFORM_TAGS,
        )


def test_environment_manifests_are_closed(tmp_path: Path) -> None:
    """Unknown and missing persisted fields cannot pass as valid state."""
    spec, _ = _selection(tmp_path)
    value = spec.to_mapping()
    value["unknown"] = True
    with pytest.raises(EnvironmentManifestError):
        EnvironmentSpecManifest.from_mapping(
            value,
            allowed_platform_tags=RELEASE_TARGET_PLATFORM_TAGS,
        )

    environment_id = EnvironmentIdentity.create(
        environment_spec_id=spec.environment_spec_id,
        installation=InstallationIdentity.parse(f"install1_{'1' * 32}"),
    ).environment_id
    install = _install_manifest(
        spec,
        environment_id,
        venv_tree_sha256="0" * 64,
    ).to_mapping()
    del install["lock_sha256"]
    with pytest.raises(EnvironmentManifestError):
        InstallManifest.from_mapping(
            install,
            allowed_platform_tags=RELEASE_TARGET_PLATFORM_TAGS,
        )

    install = _install_manifest(
        spec,
        environment_id,
        venv_tree_sha256="0" * 64,
    ).to_mapping()
    install["source"]["base_url"] = "https://user:secret@example.invalid/"
    with pytest.raises(EnvironmentManifestError):
        InstallManifest.from_mapping(
            install,
            allowed_platform_tags=RELEASE_TARGET_PLATFORM_TAGS,
        )


def _hash(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256(path.read_bytes()).digest()
    encoded = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return f"sha256={encoded}", path.stat().st_size


def _hash_bytes(value: bytes) -> tuple[str, int]:
    digest = hashlib.sha256(value).digest()
    encoded = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return f"sha256={encoded}", len(value)


def _write_test_wheel(tmp_path: Path) -> Path:
    """Build one local wheel that pip can install without network access."""
    wheel_path = tmp_path / WHEEL_FILENAME
    dist_info = "demo_package-1.0.dist-info"
    contents = {
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
    for relative, value in contents.items():
        digest, size = _hash_bytes(value)
        rows.append((relative, digest, str(size)))
    record_path = f"{dist_info}/RECORD"
    rows.append((record_path, "", ""))
    contents[record_path] = "".join(
        f"{relative},{digest},{size}\n" for relative, digest, size in rows
    ).encode()
    with zipfile.ZipFile(
        wheel_path,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
    ) as archive:
        for relative, value in contents.items():
            archive.writestr(relative, value)
    return wheel_path


def _rewrite_record_hash(record_path: Path, changed_path: Path) -> None:
    """Update one pip RECORD row after normalizing test-only provenance."""
    site_packages = changed_path.parents[1]
    relative = changed_path.relative_to(site_packages).as_posix()
    digest, size = _hash(changed_path)
    with record_path.open(encoding="utf-8", newline="") as record:
        rows = list(csv.reader(record))
    for row in rows:
        if row[0] == relative:
            row[1:] = [digest, str(size)]
            break
    else:
        raise AssertionError(f"RECORD does not contain {relative}")
    with record_path.open("w", encoding="utf-8", newline="") as record:
        writer = csv.writer(record, lineterminator="\n")
        writer.writerows(rows)


def _interpreter(venv_root: Path) -> Path:
    if os.name == "nt":
        return venv_root / "Scripts" / "python.exe"
    return venv_root / "bin" / "python"


def _site_packages(interpreter: Path) -> Path:
    result = subprocess.run(
        [
            str(interpreter),
            "-I",
            "-c",
            "import sysconfig; print(sysconfig.get_paths()['purelib'])",
        ],
        capture_output=True,
        check=True,
        text=True,
        timeout=10,
    )
    return Path(result.stdout.strip())


def _write_distribution(site_packages: Path) -> dict[str, Path]:
    package_root = site_packages / "demo_package"
    dist_info = site_packages / "demo_package-1.0.dist-info"
    package_root.mkdir(parents=True)
    dist_info.mkdir()
    files = {
        "package": package_root / "__init__.py",
        "metadata": dist_info / "METADATA",
        "wheel": dist_info / "WHEEL",
        "direct_url": dist_info / "direct_url.json",
        "record": dist_info / "RECORD",
    }
    files["package"].write_text("VALUE = 1\n", encoding="utf-8")
    files["metadata"].write_text(
        "Metadata-Version: 2.1\nName: demo-package\nVersion: 1.0\n",
        encoding="utf-8",
    )
    files["wheel"].write_text(
        "Wheel-Version: 1.0\nTag: py3-none-any\n",
        encoding="utf-8",
    )
    files["direct_url"].write_text(
        json.dumps({"url": DIRECT_URL}),
        encoding="utf-8",
    )
    rows: list[tuple[str, str, str]] = []
    for key in ("package", "metadata", "wheel", "direct_url"):
        path = files[key]
        relative = path.relative_to(site_packages).as_posix()
        digest, size = _hash(path)
        rows.append((relative, digest, str(size)))
    record_relative = files["record"].relative_to(site_packages).as_posix()
    rows.append((record_relative, "", ""))
    files["record"].write_text(
        "".join(f"{path},{digest},{size}\n" for path, digest, size in rows),
        encoding="utf-8",
    )
    return files


def _install_manifest(
    spec: EnvironmentSpecManifest,
    environment_id: str,
    *,
    venv_tree_sha256: str,
) -> InstallManifest:
    return InstallManifest(
        environment_id=environment_id,
        environment_spec_id=spec.environment_spec_id,
        python_abi=spec.python_abi,
        platform_tag=spec.platform_tag,
        lock_sha256=spec.lock_sha256,
        venv_tree_sha256=venv_tree_sha256,
        source=InstallSource(
            kind="test",
            base_url="https://example.invalid/simple/",
        ),
        packages=(
            WheelProvenance(
                name="demo-package",
                version="1.0",
                wheel_filename=WHEEL_FILENAME,
                wheel_sha256=WHEEL_DIGEST,
            ),
        ),
    )


def _installed_environment(
    tmp_path: Path,
    *,
    platform_tag: str = PLATFORM_TAG,
) -> tuple[
    Path,
    Path,
    EnvironmentSpecManifest,
    str,
    LockFile,
    dict[str, Path],
]:
    spec, lock = _selection(tmp_path, platform_tag=platform_tag)
    environment_id = EnvironmentIdentity.create(
        environment_spec_id=spec.environment_spec_id,
        installation=InstallationIdentity.parse(f"install1_{'2' * 32}"),
    ).environment_id
    spec_directory = tmp_path / dir_key(spec.environment_spec_id)
    environment_directory = spec_directory / dir_key(environment_id)
    venv_root = environment_directory / "venv"
    venv.EnvBuilder(
        with_pip=False,
        symlinks=os.name != "nt",
    ).create(venv_root)
    interpreter = _interpreter(venv_root)
    site_packages = _site_packages(interpreter)
    files = _write_distribution(site_packages)
    write_canonical_json(
        spec_directory / "environment_spec.json",
        spec.to_mapping(),
    )
    write_canonical_json(
        environment_directory / "dependency.lock",
        lock.to_mapping(),
    )
    write_canonical_json(
        environment_directory / "install.json",
        _install_manifest(
            spec,
            environment_id,
            venv_tree_sha256=compute_venv_tree_sha256(venv_root),
        ).to_mapping(),
    )
    return (
        environment_directory,
        interpreter,
        spec,
        environment_id,
        lock,
        files,
    )


def _validate(
    environment: tuple[
        Path,
        Path,
        EnvironmentSpecManifest,
        str,
        LockFile,
        dict[str, Path],
    ],
):
    directory, interpreter, spec, environment_id, lock, _ = environment
    return validate_installed_environment(
        environment_directory=directory,
        interpreter=interpreter,
        expected_spec=spec,
        expected_environment_id=environment_id,
        expected_lock=lock,
        allowed_platform_tags=RELEASE_TARGET_PLATFORM_TAGS,
    )


def _refresh_venv_tree_digest(environment_directory: Path) -> None:
    """Refresh the trusted snapshot after an intentional test installation."""
    manifest_path = environment_directory / "install.json"
    value = json.loads(manifest_path.read_text(encoding="utf-8"))
    value["venv_tree_sha256"] = compute_venv_tree_sha256(
        environment_directory / "venv",
    )
    write_canonical_json(manifest_path, value)


def test_strict_validation_accepts_an_exact_isolated_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid venv ignores ambient import state and matches every manifest."""
    ambient = tmp_path / "ambient"
    ambient.mkdir()
    monkeypatch.setenv("PYTHONPATH", str(ambient))
    environment = _installed_environment(tmp_path)
    if os.name != "nt":
        assert environment[1].is_symlink()

    result = _validate(environment)

    assert result.status == "installed"
    assert not result.reasons


@pytest.mark.parametrize(
    "mutation",
    [
        "wheel_provenance",
        "extra_distribution",
        "distribution_version",
        "direct_url",
        "record_hash",
        "record_entry",
        "system_site",
    ],
)
def test_dependency_or_isolation_mismatch_requires_repair(
    tmp_path: Path,
    mutation: str,
) -> None:
    """No dependency or isolation mismatch can pass strict validation."""
    environment = _installed_environment(tmp_path)
    directory, _, _, _, _, files = environment
    if mutation == "wheel_provenance":
        value = json.loads(
            (directory / "install.json").read_text(encoding="utf-8"),
        )
        value["packages"][0]["wheel_sha256"] = "b" * 64
        write_canonical_json(directory / "install.json", value)
    elif mutation == "extra_distribution":
        site_packages = files["package"].parents[1]
        extra = site_packages / "extra_package-1.0.dist-info"
        extra.mkdir()
        (extra / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: extra-package\nVersion: 1.0\n",
            encoding="utf-8",
        )
    elif mutation == "distribution_version":
        files["metadata"].write_text(
            "Metadata-Version: 2.1\nName: demo-package\nVersion: 2.0\n",
            encoding="utf-8",
        )
    elif mutation == "direct_url":
        files["direct_url"].write_text(
            json.dumps({"url": "https://example.invalid/other.whl"}),
            encoding="utf-8",
        )
    elif mutation == "record_hash":
        files["package"].write_text("VALUE = 2\n", encoding="utf-8")
    elif mutation == "record_entry":
        rows = files["record"].read_text(encoding="utf-8").splitlines()
        record_text = "\n".join(rows[:-1])
        files["record"].write_text(
            f"{record_text}\n",
            encoding="utf-8",
        )
    else:
        config = directory / "venv" / "pyvenv.cfg"
        config.write_text(
            config.read_text(encoding="utf-8").replace(
                "include-system-site-packages = false",
                "include-system-site-packages = true",
            ),
            encoding="utf-8",
        )
    if mutation != "wheel_provenance":
        _refresh_venv_tree_digest(directory)

    result = _validate(environment)

    assert result.status == "repair_required"
    assert result.reasons


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("python_abi", "cp399-cp399"),
        ("platform_tag", "win_amd64"),
        ("condition_set_sha256", "f" * 64),
    ],
)
def test_persisted_spec_dimension_mismatch_requires_repair(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    """Every persisted Environment Spec identity dimension is authoritative."""
    environment = _installed_environment(tmp_path)
    directory, _, _, _, _, _ = environment
    spec_path = directory.parent / "environment_spec.json"
    spec_value = json.loads(spec_path.read_text(encoding="utf-8"))
    spec_value[field] = value
    write_canonical_json(spec_path, spec_value)

    result = _validate(environment)

    assert result.status == "repair_required"
    assert result.reasons


def test_manifest_identity_mismatch_requires_repair(tmp_path: Path) -> None:
    """A persisted installation cannot be reused for another spec identity."""
    environment = _installed_environment(tmp_path)
    directory, _, _, _, _, _ = environment
    value = json.loads(
        (directory / "install.json").read_text(encoding="utf-8"),
    )
    value["environment_spec_id"] = f"ches1_{'f' * 64}"
    write_canonical_json(directory / "install.json", value)

    result = _validate(environment)

    assert result.status == "repair_required"
    assert result.reasons


def test_main_interpreter_cannot_replace_environment_python(
    tmp_path: Path,
) -> None:
    """The Core interpreter is never accepted as an environment fallback."""
    environment = _installed_environment(tmp_path)
    directory, _, spec, environment_id, lock, _ = environment

    result = validate_installed_environment(
        environment_directory=directory,
        interpreter=Path(sys.executable),
        expected_spec=spec,
        expected_environment_id=environment_id,
        expected_lock=lock,
        allowed_platform_tags=RELEASE_TARGET_PLATFORM_TAGS,
    )

    assert result.status == "repair_required"
    assert any("inside" in reason for reason in result.reasons)


def test_actual_interpreter_rejects_an_incompatible_platform_tag(
    tmp_path: Path,
) -> None:
    """A registry tag must also be compatible with the target interpreter."""
    environment = _installed_environment(
        tmp_path,
        platform_tag=INCOMPATIBLE_PLATFORM_TAG,
    )

    result = _validate(environment)

    assert result.status == "repair_required"
    assert any("platform" in reason for reason in result.reasons)


def test_unrecorded_importable_file_requires_repair(tmp_path: Path) -> None:
    """The install snapshot cannot bless a file absent from every RECORD."""
    environment = _installed_environment(tmp_path)
    directory, _, _, _, _, files = environment
    (files["package"].parent / "injected.py").write_text(
        "INJECTED = True\n",
        encoding="utf-8",
    )
    _refresh_venv_tree_digest(directory)

    result = _validate(environment)

    assert result.status == "repair_required"
    assert any("locked RECORDs" in reason for reason in result.reasons)


@pytest.mark.parametrize(
    "startup_file",
    ["unrecorded.pth", "sitecustomize.py"],
)
def test_unrecorded_site_code_in_install_snapshot_is_not_executed(
    tmp_path: Path,
    startup_file: str,
) -> None:
    """An install snapshot cannot authorize unowned site startup code."""
    environment = _installed_environment(tmp_path)
    directory, _, _, _, _, files = environment
    site_packages = files["package"].parents[1]
    sentinel = tmp_path / "unrecorded-site-code-executed"
    code = (
        f"import pathlib; pathlib.Path({str(sentinel)!r}).write_text"
        f"('executed', encoding='utf-8')\n"
    )
    (site_packages / startup_file).write_text(code, encoding="utf-8")
    _refresh_venv_tree_digest(directory)

    result = _validate(environment)

    assert result.status == "repair_required"
    assert any("locked RECORDs" in reason for reason in result.reasons)
    assert not sentinel.exists()


@pytest.mark.parametrize(
    "startup_file",
    ["side_effect.pth", "sitecustomize.py"],
)
def test_unverified_site_code_is_not_executed(
    tmp_path: Path,
    startup_file: str,
) -> None:
    """Integrity failure is returned before Python initializes site code."""
    environment = _installed_environment(tmp_path)
    directory, _, _, _, _, files = environment
    site_packages = files["package"].parents[1]
    sentinel = tmp_path / "site-code-executed"
    code = (
        f"import pathlib; pathlib.Path({str(sentinel)!r}).write_text"
        f"('executed', encoding='utf-8')\n"
    )
    startup_path = site_packages / startup_file
    startup_path.write_text(code, encoding="utf-8")
    digest, size = _hash(startup_path)
    relative = startup_path.relative_to(site_packages).as_posix()
    with files["record"].open("a", encoding="utf-8") as record:
        record.write(f"{relative},{digest},{size}\n")
    _refresh_venv_tree_digest(directory)
    files["package"].write_text("VALUE = 2\n", encoding="utf-8")
    _refresh_venv_tree_digest(directory)

    result = _validate(environment)

    assert result.status == "repair_required"
    assert any("RECORD hash mismatches" in reason for reason in result.reasons)
    assert not sentinel.exists()


@pytest.mark.parametrize("metadata_file", ["metadata", "record"])
def test_invalid_distribution_encoding_requires_repair(
    tmp_path: Path,
    metadata_file: str,
) -> None:
    """Invalid distribution text is normalized to a repair result."""
    environment = _installed_environment(tmp_path)
    directory, _, _, _, _, files = environment
    files[metadata_file].write_bytes(b"\xff\xfe")
    _refresh_venv_tree_digest(directory)

    result = _validate(environment)

    assert result.status == "repair_required"
    assert result.reasons


def test_malformed_record_requires_repair(tmp_path: Path) -> None:
    """A structurally invalid RECORD cannot escape as a parser exception."""
    environment = _installed_environment(tmp_path)
    directory, _, _, _, _, files = environment
    files["record"].write_text("one-column-only\n", encoding="utf-8")
    _refresh_venv_tree_digest(directory)

    result = _validate(environment)

    assert result.status == "repair_required"
    assert any("RECORD" in reason for reason in result.reasons)


def test_pep_376_unhashed_bytecode_is_protected_by_tree_snapshot(
    tmp_path: Path,
) -> None:
    """Pip-generated pyc may omit hashes but remains snapshot-protected."""
    environment = _installed_environment(tmp_path)
    directory, _, _, _, _, files = environment
    shutil.rmtree(files["package"].parent)
    shutil.rmtree(files["metadata"].parent)
    site_packages = files["package"].parents[1]
    wheel_path = _write_test_wheel(tmp_path)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-cache-dir",
            "--no-deps",
            "--no-index",
            "--target",
            str(site_packages),
            str(wheel_path),
        ],
        capture_output=True,
        check=True,
        text=True,
        timeout=30,
    )
    files["direct_url"].write_text(
        json.dumps({"url": DIRECT_URL}),
        encoding="utf-8",
    )
    _rewrite_record_hash(files["record"], files["direct_url"])
    bytecodes = tuple(site_packages.rglob("*.pyc"))
    assert bytecodes
    assert any(
        f"{path.relative_to(site_packages).as_posix()},,"
        in files["record"].read_text(encoding="utf-8")
        for path in bytecodes
    )
    _refresh_venv_tree_digest(directory)

    valid = _validate(environment)

    assert valid.status == "installed"

    bytecodes[0].write_bytes(b"changed")
    changed = _validate(environment)
    assert changed.status == "repair_required"
    assert any("tree digest" in reason for reason in changed.reasons)


@pytest.mark.skipif(os.name == "nt", reason="POSIX parent symlink alias")
def test_parent_path_alias_keeps_the_venv_interpreter_inside(
    tmp_path: Path,
) -> None:
    """Resolving parent aliases must preserve the final interpreter symlink."""
    actual_root = tmp_path / "actual"
    environment = _installed_environment(actual_root)
    directory, interpreter, spec, environment_id, lock, files = environment
    alias_root = tmp_path / "alias"
    alias_root.symlink_to(actual_root, target_is_directory=True)
    aliased_environment = (
        alias_root / directory.relative_to(actual_root),
        alias_root / interpreter.relative_to(actual_root),
        spec,
        environment_id,
        lock,
        files,
    )

    result = _validate(aliased_environment)

    assert result.status == "installed"
