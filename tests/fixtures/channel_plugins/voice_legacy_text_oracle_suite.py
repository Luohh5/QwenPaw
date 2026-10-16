"""Characterize the frozen legacy extractor without importing legacy channel SDKs."""

import ast
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from qwenpaw.schemas import ContentType

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize(
    "message_type", ["message", "reasoning", "tool_call", "tool_result"]
)
def test_frozen_legacy_completed_extractor_preserves_all_types_and_empty_fragments(
    message_type,
):
    path = (
        ROOT
        / "tests/fixtures/channel_plugins/legacy_voice_conversation_relay.py"
    )
    tree = ast.parse(path.read_text())
    (function,) = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_extract_text_from_event"
    ]
    function.decorator_list = []
    namespace = {"Any": Any, "ContentType": ContentType}
    exec(
        compile(ast.Module(body=[function], type_ignores=[]), str(path), "exec"),
        namespace,
    )
    extract = namespace[function.name]
    event = SimpleNamespace(
        type=message_type,
        content=[
            SimpleNamespace(type=ContentType.TEXT, text="  alpha  "),
            SimpleNamespace(type=ContentType.TEXT, text="   "),
            SimpleNamespace(type=ContentType.REFUSAL, refusal="  no  "),
            SimpleNamespace(type="image", text="not spoken"),
        ],
    )
    assert extract(event) == "alpha  no"
    assert extract(SimpleNamespace(content=[])) == ""
    assert extract(SimpleNamespace(content=None)) == ""


def test_legacy_inputs_resolve_in_copy_without_sdd(tmp_path):
    """All historical inputs exist in a minimal repository-shaped fixture copy."""
    import hashlib
    import json
    import shutil

    fixture_root = ROOT / "tests/fixtures/channel_plugins"
    copied = tmp_path / "checkout/tests/fixtures/channel_plugins"
    copied.mkdir(parents=True)
    shutil.copytree(
        fixture_root / "legacy_azure_bot", copied / "legacy_azure_bot"
    )
    shutil.copy2(fixture_root / "legacy_voice_conversation_relay.py", copied)
    shutil.copy2(
        fixture_root / "legacy_azure_registration_fields.json", copied
    )
    provenance = json.loads(
        (fixture_root / "legacy_channel_baselines.json").read_text()
    )
    for name, row in provenance["azure"].items():
        assert (
            hashlib.sha256(
                (copied / "legacy_azure_bot" / name).read_bytes()
            ).hexdigest()
            == row["sha256"]
        )
    assert (
        hashlib.sha256(
            (copied / "legacy_voice_conversation_relay.py").read_bytes()
        ).hexdigest()
        == provenance["voice_fixture_sha256"]
    )
    registration = provenance["azure_registration_fields"]
    assert (
        hashlib.sha256(
            (tmp_path / "checkout" / registration["fixture"]).read_bytes()
        ).hexdigest()
        == registration["fixture_sha256"]
    )
    inputs = set()
    for name in (
        "official_g1_azure_form_suite.py",
        "azure_bot_legacy_auth_suite.py",
        "azure_bot_legacy_refs_suite.py",
        "voice_legacy_text_oracle_suite.py",
        "voice_unknown_identity_oracle_suite.py",
        "voice_raw_completed_host_suite.py",
    ):
        source = fixture_root / name
        shutil.copy2(source, copied / name)
        tree = ast.parse(source.read_text())
        tree.body = [
            node
            for node in tree.body
            if not (
                isinstance(node, ast.FunctionDef)
                and node.name
                == "test_legacy_inputs_resolve_in_copy_without_sdd"
            )
        ]
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert not node.value.startswith("." + "superpowers/")
                if node.value.startswith(
                    "tests/fixtures/channel_plugins/legacy_"
                ):
                    inputs.add(node.value)
                    assert (tmp_path / "checkout" / node.value).exists()
    assert inputs == {
        "tests/fixtures/channel_plugins/legacy_azure_registration_fields.json",
        "tests/fixtures/channel_plugins/legacy_azure_bot",
        "tests/fixtures/channel_plugins/legacy_voice_conversation_relay.py",
    }
    assert not (tmp_path / "checkout" / ".superpowers").exists()
