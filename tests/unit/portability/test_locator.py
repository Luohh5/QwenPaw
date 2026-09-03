# -*- coding: utf-8 -*-
"""Cross-user and cross-platform source-location regression tests."""

from pathlib import Path

import pytest

from qwenpaw.portability.providers.locator import resolve_source_location


def test_codex_location_uses_each_users_home(tmp_path: Path) -> None:
    home = tmp_path / "another-user"
    codex_home = home / ".codex"
    codex_home.mkdir(parents=True)

    location = resolve_source_location(
        "codex",
        user_home=home,
        environ={},
    )

    assert location.data_home == str(codex_home.resolve())
    assert location.data_home_source == "platform_default"
    assert location.data_home_exists is True


def test_environment_and_explicit_roots_have_stable_priority(
    tmp_path: Path,
) -> None:
    configured = tmp_path / "configured-codex"
    explicit = tmp_path / "explicit-codex"
    configured.mkdir()
    explicit.mkdir()

    from_environment = resolve_source_location(
        "codex",
        user_home=tmp_path,
        environ={"CODEX_HOME": str(configured)},
    )
    from_argument = resolve_source_location(
        "codex",
        source_home=explicit,
        user_home=tmp_path,
        environ={"CODEX_HOME": str(configured)},
    )

    assert from_environment.data_home == str(configured.resolve())
    assert from_environment.data_home_source == "environment:CODEX_HOME"
    assert from_argument.data_home == str(explicit.resolve())
    assert from_argument.data_home_source == "explicit"


@pytest.mark.parametrize(
    ("platform_name", "environment", "relative"),
    [
        (
            "darwin",
            {},
            Path("Library/Application Support/Qoder/User"),
        ),
        (
            "linux",
            {},
            Path(".config/Qoder/User"),
        ),
        (
            "linux",
            {"XDG_CONFIG_HOME": "/opt/user-config"},
            Path("/opt/user-config/Qoder/User"),
        ),
        (
            "win32",
            {"APPDATA": "/windows/AppData/Roaming"},
            Path("/windows/AppData/Roaming/Qoder/User"),
        ),
    ],
)
def test_qoder_editor_location_is_platform_specific(
    tmp_path: Path,
    platform_name: str,
    environment: dict[str, str],
    relative: Path,
) -> None:
    home = tmp_path / "user"
    location = resolve_source_location(
        "qoder",
        user_home=home,
        platform_name=platform_name,
        environ=environment,
    )
    expected = relative if relative.is_absolute() else home / relative

    assert location.data_home == str((home / ".qoder").resolve())
    assert location.user_data_home == str(expected.resolve())


def test_qoder_dedicated_environment_variables_are_honored(
    tmp_path: Path,
) -> None:
    qoder_home = tmp_path / "qoder-home"
    user_data = tmp_path / "qoder-user-data"

    location = resolve_source_location(
        "qoder",
        user_home=tmp_path,
        environ={
            "QODER_HOME": str(qoder_home),
            "QODER_USER_DATA_HOME": str(user_data),
        },
    )

    assert location.data_home == str(qoder_home.resolve())
    assert location.user_data_home == str(user_data.resolve())
    assert location.data_home_source == "environment:QODER_HOME"


def test_relative_explicit_source_is_rejected() -> None:
    with pytest.raises(ValueError, match="must be absolute"):
        resolve_source_location(
            "codex",
            source_home=Path("relative/.codex"),
            environ={},
        )
