# -*- coding: utf-8 -*-
"""Portable author contract: public harness types never expose host objects."""

import dataclasses
import inspect

import pytest

DTO_FIELDS = {
    "HarnessCapabilities": {
        "authentication",
        "model_selection",
        "reasoning_effort",
        "reasoning_stream",
        "tool_stream",
        "session_resume",
        "workspace_ui",
        "native_skills_ui",
        "native_tools_ui",
        "native_mcp_ui",
        "loop_modes",
        "attachments",
        "context_usage",
        "skills_commands",
        "qwenpaw_skills_projection",
        "qwenpaw_mcp_projection",
        "provider_skills_discovery",
        "provider_mcp_discovery",
        "mcp_tool_allowlist",
        "commands",
        "approval_presets",
        "external_history",
    },
    "HarnessSpec": {
        "id",
        "label",
        "create",
        "capabilities",
        "config_fields",
        "description",
        "icon",
        "doc_url",
        "instance_settings_keys",
    },
    "HarnessContext": {
        "plugin_id",
        "generation",
        "workspace_id",
        "workspace_dir",
        "state_dir",
        "storage",
        "resources",
        "logger",
    },
    "HarnessCallContext": {
        "request_id",
        "session_id",
        "user_id",
        "channel",
        "cwd",
        "settings",
        "runtime_capabilities",
        "resources",
        "deadline",
        "cancelled",
    },
    "HarnessApprovalSummary": {
        "tool_name",
        "title",
        "description",
        "parameters",
        "severity",
    },
    "HarnessRunRequest": {"prompt", "attachments"},
    "HarnessEvent": {"kind", "text", "item_id", "tool_name", "data"},
    "HarnessHistoryItem": {"kind", "text", "item_id", "tool_name", "data"},
    "HarnessAttachment": {"kind", "path", "name"},
    "HarnessCommand": {"name", "description", "accepts_arguments"},
    "HarnessApprovalPreset": {"id", "name", "description", "settings"},
    "HarnessModel": {
        "id",
        "name",
        "description",
        "is_default",
        "reasoning_efforts",
        "default_reasoning_effort",
    },
    "HarnessStatus": {
        "installed",
        "authenticated",
        "account",
        "runtime_path",
        "runtime_source",
        "error",
    },
    "HarnessLogin": set(),
    "HarnessDiscoveredSkill": {
        "name",
        "description",
        "provider_id",
        "source",
        "enabled",
        "read_only",
        "scope",
    },
    "HarnessDiscoveredMCPServer": {
        "name",
        "provider_id",
        "transport",
        "enabled",
        "auth_status",
        "read_only",
        "scope",
    },
    "HarnessRuntimeCapabilities": {"skills", "mcp_servers"},
    "HarnessSkillDefinition": {"name", "description", "directory", "revision"},
    "HarnessMCPServerDefinition": {
        "name",
        "display_name",
        "transport",
        "command",
        "args",
        "cwd",
        "url",
        "env",
        "headers",
        "tools",
        "tool_policies",
        "default_policy",
        "credential_revision",
        "runtime_revision",
    },
}


def test_public_harness_dtos_are_frozen_and_versioned():
    from qwenpaw.plugins import SDK_CAPABILITIES, SDK_VERSION
    from qwenpaw.plugins import harnesses
    from qwenpaw.plugins.compliance import API_EXPORTS, PUBLIC_API

    assert SDK_VERSION == "1.1.0" and "harnesses" in SDK_CAPABILITIES
    exports = {
        row.symbol: row
        for row in API_EXPORTS
        if row.module == harnesses.__name__
    }
    for name, required in DTO_FIELDS.items():
        cls = getattr(harnesses, name)
        assert (
            dataclasses.is_dataclass(cls) and cls.__dataclass_params__.frozen
        )
        assert required <= {field.name for field in dataclasses.fields(cls)}
        assert name in PUBLIC_API[harnesses.__name__]
        assert exports[name].since == "1.1.0"
    assert all(
        row.since == "1.0.0"
        for row in API_EXPORTS
        if row.module == "qwenpaw.plugins.tools"
    )
    # History must be SDK-owned, rather than an alias into a vendor module.
    assert harnesses.HarnessHistoryItem.__module__ == harnesses.__name__
    request = harnesses.HarnessRunRequest("hello")
    assert isinstance(request.attachments, tuple)
    assert not request.attachments
    with pytest.raises(dataclasses.FrozenInstanceError):
        request.prompt = "mutated"
    assert harnesses.HarnessCapabilities().external_history is False


def test_backend_and_archive_protocol_signatures():
    from qwenpaw.plugins.harnesses import (
        HarnessBackend,
        HarnessArchiveBackend,
        HarnessCallContext,
    )

    signatures = {
        "status": ("self", "context"),
        "start_login": ("self", "context", "device_code"),
        "logout": ("self", "context"),
        "models": ("self", "context"),
        "discover_skills": ("self", "context"),
        "discover_mcp": ("self", "context"),
        "run_turn": ("self", "context", "request"),
        "run_command": ("self", "context", "command", "arguments"),
        "history": ("self", "context"),
        "reset_session": ("self", "context"),
        "close": ("self",),
    }
    # Inspecting the typing marker is the protocol-shape assertion.
    # pylint: disable-next=protected-access
    assert HarnessBackend._is_protocol and HarnessArchiveBackend._is_protocol
    for name, parameters in signatures.items():
        method = getattr(HarnessBackend, name)
        assert tuple(inspect.signature(method).parameters) == parameters
        assert inspect.iscoroutinefunction(method) is (name != "run_turn")
    login = inspect.signature(HarnessBackend.start_login).parameters[
        "device_code"
    ]
    assert (
        login.kind is inspect.Parameter.KEYWORD_ONLY and login.default is False
    )
    for name, parameters in {
        "list_external_threads": ("self", "context", "limit", "archived"),
        "read_external_thread": ("self", "context", "thread_id"),
        "external_skill_records": ("self", "context"),
        "external_mcp_records": ("self", "context"),
    }.items():
        method = getattr(HarnessArchiveBackend, name)
        assert tuple(inspect.signature(method).parameters) == parameters
        assert inspect.iscoroutinefunction(method)
    approval = inspect.signature(HarnessCallContext.request_approval)
    assert tuple(approval.parameters) == ("self", "summary", "timeout")
    assert (
        approval.parameters["timeout"].kind is inspect.Parameter.KEYWORD_ONLY
    )


def test_event_and_history_kinds_preserve_wire_values():
    from qwenpaw.plugins.harnesses import HarnessEventKind, HarnessHistoryKind

    assert {kind.value for kind in HarnessEventKind} == {
        "text_delta",
        "reasoning_delta",
        "tool_started",
        "tool_progress",
        "tool_completed",
        "completed",
        "cancelled",
        "error",
    }
    assert {kind.value for kind in HarnessHistoryKind} == {
        "user",
        "message",
        "reasoning",
        "tool_call",
        "tool_output",
    }


def test_runtime_credentials_are_masked_until_explicit_reveal():
    import json
    from pydantic import SecretStr
    from qwenpaw.plugins.harnesses import (
        HarnessMCPServerDefinition,
        HarnessRuntimeCapabilities,
    )

    server = HarnessMCPServerDefinition(
        name="private-mcp",
        display_name="Private MCP",
        transport="stdio",
        env={"TOKEN": SecretStr("contract-secret-token")},
        headers={"Authorization": SecretStr("contract-secret-header")},
        credential_revision="credential-r1",
    )
    capabilities = HarnessRuntimeCapabilities(mcp_servers=(server,))
    snapshot = json.dumps(dataclasses.asdict(capabilities), default=str)
    for secret in ("contract-secret-token", "contract-secret-header"):
        assert secret not in repr(capabilities) and secret not in snapshot
        assert secret not in capabilities.fingerprint
    assert server.revealed_env() == {"TOKEN": "contract-secret-token"}
    assert server.revealed_headers() == {
        "Authorization": "contract-secret-header",
    }
    assert isinstance(capabilities.skills, tuple)
    assert not capabilities.skills


def test_runtime_secret_mappings_cannot_be_mutated_into_plaintext():
    import json
    from pydantic import SecretStr
    from qwenpaw.plugins.harnesses import HarnessMCPServerDefinition

    inputs = {
        "env": {"TOKEN": SecretStr("source-env-secret")},
        "headers": {"Authorization": SecretStr("source-header-secret")},
    }
    server = HarnessMCPServerDefinition(
        name="protected",
        display_name="Protected",
        transport="stdio",
        env=inputs["env"],
        headers=inputs["headers"],
    )
    for field, key in (("env", "TOKEN"), ("headers", "Authorization")):
        inputs[field][key] = SecretStr("changed-input-secret")
        assert (
            "changed-input-secret"
            not in getattr(server, field)[key].get_secret_value()
        )
        values = getattr(server, field)
        try:
            values[key] = "mutated-mapping-secret"
        except (TypeError, AttributeError):
            pass
        else:
            assert isinstance(values[key], SecretStr)
        serialized = json.dumps(dataclasses.asdict(server), default=str)
        for secret in (
            "source-env-secret",
            "source-header-secret",
            "mutated-mapping-secret",
            "changed-input-secret",
        ):
            assert secret not in repr(server) and secret not in serialized
