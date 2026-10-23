# -*- coding: utf-8 -*-
"""Import measurements never claim actual service readiness."""

# These tests intentionally inspect isolated measurement helpers.
# pylint: disable=protected-access

import json
import os
import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "measure_harness_footprint.py"


def measure(tmp_path, scenario, *, variant="absent", plugin_dir=None):
    output = tmp_path / f"{scenario}-{variant}.json"
    command = [
        sys.executable,
        str(SCRIPT),
        "--scenario",
        scenario,
        "--variant",
        variant,
        "--output",
        str(output),
    ]
    if plugin_dir is not None:
        command.extend(("--plugin-dir", str(plugin_dir)))
    result = subprocess.run(
        command,
        cwd=ROOT,
        env={**os.environ, "QWENPAW_WORKING_DIR": str(tmp_path / "work")},
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(output.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "scenario",
    ["native-startup", "native-agent-list", "native-chat", "frontend-native"],
)
def test_probe_records_modules_without_vendor_execution(tmp_path, scenario):
    report = measure(tmp_path, scenario)
    expected_commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        text=True,
    ).strip()
    assert report["commit"] == expected_commit
    assert isinstance(report["source_state"]["dirty"], bool)
    assert report["source_state"]["fingerprint"]
    assert report["scenario"] == scenario
    assert report["variant"] == "absent"
    assert report["python"] and report["platform"]
    assert report["modules"] == sorted(set(report["modules"]))
    assert "qwenpaw" in report["modules"]
    assert report["vendor_process_count"] == 0
    assert isinstance(report["vendor_probe_count"], int)
    assert report["vendor_probe_count"] >= 0
    for field in ("process_rss_bytes", "children_rss_bytes"):
        if report[field] is None:
            assert report["rss_source"].startswith("rss-unavailable:")
        else:
            assert isinstance(report[field], int) and report[field] >= 0
    assert (
        isinstance(report["import_ms"], (int, float))
        and report["import_ms"] >= 0
    )
    if scenario == "frontend-native":
        assert report["ready_ms"] is None and report["unavailable_reason"]
        assert report["milestone"] == "app-imported"
        assert "network_bytes" not in report and "bundle_bytes" not in report
    else:
        expected = {
            "native-startup": "core-agents-ready",
            "native-agent-list": "native-agent-list-complete",
            "native-chat": "native-chat-persisted",
        }[scenario]
        assert report["milestone"] == expected
        assert report["ready_ms"] >= 0
        assert report["plugin_ready_ms"] >= report["ready_ms"]
        assert report["plugin_states"] == {}
        assert report["vendor_probe_count"] == 0
        assert report["unavailable_reason"] is None
        if scenario != "native-startup":
            assert report["agent_count"] >= 1
            assert report["agent_list_ms"] >= 0
        if scenario == "native-chat":
            assert report["native_chat_ms"] >= 0
            assert report["history_verified"] is True
            assert report["history_roles"] == ["assistant", "user"]


@pytest.mark.parametrize("variant", ["disabled", "codex", "qoder"])
def test_variant_waits_for_actual_catalog_state(tmp_path, variant):
    plugin_dir = None
    if variant == "disabled":
        packed = tmp_path / "packed"
        process = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/pack/generate_plugin_metadata.py"),
                "--plugins-root",
                str(ROOT / "plugins"),
                "--dist",
                str(packed),
                "--only",
                "qwenpaw-harness-codex",
                "--only",
                "qwenpaw-harness-qoder",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert process.returncode == 0, process.stderr
        plugin_dir = packed / "harness"
    if variant != "disabled":
        if os.environ.get("QWENPAW_A6_VARIANTS") != "1":
            pytest.skip("Run in prepared both-vendor env with A6 variants")
        assert importlib.util.find_spec("codex_cli_bin") is not None
        assert importlib.util.find_spec("qoder_agent_sdk") is not None

    report = measure(
        tmp_path,
        "native-startup",
        variant=variant,
        plugin_dir=plugin_dir,
    )
    expected = (
        {"codex": "disabled", "qoder": "disabled"}
        if variant == "disabled"
        else {variant: "available"}
    )
    assert report["milestone"] == "core-agents-ready"
    assert report["plugin_states"] == {"qwenpaw": "available", **expected}
    assert set(report["plugin_manifests"]) == {
        f"qwenpaw-harness-{name}" for name in expected
    }
    assert report["plugin_ready_ms"] >= report["ready_ms"]
    assert report["vendor_probe_count"] == 0
    assert report["vendor_process_count"] == 0
    assert not any(
        name.startswith(("codex_cli_bin", "openai_codex", "qoder_agent_sdk"))
        for name in report["modules"]
    )


def test_unknown_scenario_fails_without_writing_report(tmp_path):
    output = tmp_path / "invalid.json"
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--scenario",
            "not-a-scenario",
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode != 0 and not output.exists()
    assert "invalid choice" in result.stderr


def test_source_archive_requires_declared_baseline_commit(tmp_path):
    archive = tmp_path / "snapshot"
    archive.mkdir()
    output = tmp_path / "archive.json"
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--scenario",
            "frontend-native",
            "--source-root",
            str(archive),
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode != 0 and not output.exists()
    assert "Source archives require --baseline-commit" in result.stderr


@pytest.fixture(name="probe")
def probe_module():
    spec = importlib.util.spec_from_file_location(
        "footprint_probe_contract",
        SCRIPT,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable marker proof")
def test_vendor_probe_guard_blocks_real_launch_and_restores_on_exit(
    tmp_path,
    probe,
):
    import shutil

    marker = tmp_path / "vendor-started"
    executable = tmp_path / "codex"
    executable.write_text(
        f"#!{sys.executable}\nfrom pathlib import Path\n"
        f"Path({str(marker)!r}).touch()\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    original_which = shutil.which
    with probe.VendorProbeGuard() as guard:
        assert shutil.which(str(executable)) == str(executable)
        with pytest.raises(RuntimeError):
            subprocess.run([str(executable)], check=True)
        assert not marker.exists()
        assert guard.vendor_process_count == 0
        assert guard.vendor_probe_count >= 2
        assert guard.blocked_process_attempts
    assert shutil.which is original_which
    subprocess.run([str(executable)], check=True)
    assert (
        marker.exists()
    ), "Guard exit must restore ordinary process execution"


@pytest.mark.parametrize("event", ["socket.sendto", "socket.sendmsg"])
def test_probe_guard_denies_outbound_datagram_audit_events(probe, event):
    # Emit the real CPython boundary event without performing any networking.
    with probe.VendorProbeGuard() as guard:
        with pytest.raises(RuntimeError, match="network"):
            sys.audit(event, None, ("example.invalid", 9))
        assert guard.blocked_network_attempts == 1


@pytest.mark.parametrize("runner", [os.system, subprocess.run])
def test_blocked_shell_report_never_contains_command_or_secret(probe, runner):
    command = "echo P0_CONTRACT_SECRET_SENTINEL"
    with probe.VendorProbeGuard() as guard:
        with pytest.raises(RuntimeError):
            runner(command)
        encoded = json.dumps(guard.blocked_process_attempts)
        assert guard.blocked_process_attempts
        assert "P0_CONTRACT_SECRET_SENTINEL" not in encoded
        assert command not in encoded


def test_source_state_distinguishes_clean_dirty_and_changed_bytes(
    tmp_path,
    probe,
):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    clean = probe._source_state(tmp_path)
    source = tmp_path / "src" / "example.py"
    source.parent.mkdir()
    source.write_text("VALUE = 1\n", encoding="utf-8")
    dirty = probe._source_state(tmp_path)
    source.write_text("VALUE = 2\n", encoding="utf-8")
    changed = probe._source_state(tmp_path)
    assert clean["dirty"] is False and dirty["dirty"] is True
    assert changed["dirty"] is True
    assert len({row["fingerprint"] for row in (clean, dirty, changed)}) == 3


def test_isolated_env_replaces_windows_profile_directories(
    tmp_path,
    probe,
    monkeypatch,
):
    for key in ("USERPROFILE", "APPDATA", "LOCALAPPDATA"):
        monkeypatch.setenv(key, str(tmp_path / "real-user" / key))
    isolated = tmp_path / "isolated"
    env = probe._isolated_env(isolated)
    for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA"):
        assert Path(env[key]).is_relative_to(isolated)


def test_rss_without_platform_measurement_returns_unavailable(
    probe,
    monkeypatch,
):
    # Simulate missing optional/platform facilities, not their return values.
    monkeypatch.setitem(sys.modules, "psutil", None)
    monkeypatch.setitem(sys.modules, "resource", None)
    process, children, source = probe._rss()
    assert process is None and children is None
    assert source.startswith("rss-unavailable:")
