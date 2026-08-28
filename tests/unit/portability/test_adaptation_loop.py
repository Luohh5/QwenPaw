# -*- coding: utf-8 -*-
# pylint: disable=protected-access
import asyncio
import inspect
import json
from pathlib import Path
import sys
from textwrap import indent
from types import SimpleNamespace

import pytest

from qwenpaw.app.agent_context import scoped_session_id
from qwenpaw.modes.mission import MissionMode
from qwenpaw.plugins.api import PluginApi
from qwenpaw.portability.adaptation_loop import (
    get_active_adaptation_context,
    run_adaptation_loop,
)
from qwenpaw.portability.adaptation_staging import (
    component_map,
    stage_local_assets,
)
from qwenpaw.portability.compatibility import (
    AssetType,
    AssetZone,
    load_manifest,
)
from qwenpaw.portability.compatibility_testing import (
    CompatibilityTester,
    discover_components,
)
from qwenpaw.portability.models import (
    ProviderInventory,
    SourceMCPServer,
    SourcePlugin,
    SourceSkill,
)


def _skill(tmp_path: Path, body: str) -> SourceSkill:
    root = tmp_path / "source-skill"
    root.mkdir()
    (root / "SKILL.md").write_text(body, encoding="utf-8")
    return SourceSkill(
        source_id="demo",
        name="demo",
        description="demo",
        directory=root,
    )


class _Workspace:
    def __init__(self, root: Path, action) -> None:
        self.workspace_dir = root / "workspace"
        self.workspace_dir.mkdir()
        self.agent_id = "agent"
        self.plugins = SimpleNamespace(
            modes=[MissionMode()],
            tool_registry=SimpleNamespace(names=lambda: ["read_file"]),
        )
        self.cron_manager = None
        self._action = action
        self.request = None
        self.requests = []
        self.active_queries = 0
        self.max_active_queries = 0

    async def stream_query(self, request):
        self.request = request
        self.requests.append(request)
        self.active_queries += 1
        self.max_active_queries = max(
            self.max_active_queries,
            self.active_queries,
        )
        try:
            await asyncio.sleep(0)
            with scoped_session_id(request.session_id):
                await self._action(get_active_adaptation_context())
        finally:
            self.active_queries -= 1
        if self.request is None:
            yield None


def test_native_plugin_test_rejects_manifest_without_entry(
    tmp_path: Path,
) -> None:
    (tmp_path / "plugin.json").write_text(
        '{"id":"empty","version":"1.0.0"}',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="no backend or frontend"):
        CompatibilityTester._test_native_plugin(tmp_path)


def _write_native_plugin(root: Path, backend: str) -> None:
    (root / "plugin.json").write_text(
        '{"id":"native","version":"1.0.0","entry":{"backend":"plugin.py"}}',
        encoding="utf-8",
    )
    (root / "plugin.py").write_text(backend, encoding="utf-8")


def _native_backend(
    registration: str,
    *,
    definitions: str = "",
    register_args: str = "self, api",
) -> str:
    prefix = f"{definitions.rstrip()}\n\n" if definitions else ""
    return (
        f"{prefix}class NativePlugin:\n"
        f"    def register({register_args}):\n"
        f"{indent(registration.strip(), '        ')}\n\n"
        "plugin = NativePlugin()\n"
    )


@pytest.mark.parametrize(
    ("backend", "message"),
    [
        (
            _native_backend(
                "api.register_slash_command(\n"
                "    name='demo', async_handler=command)",
                definitions="async def command(ctx, args):\n    return None",
            ),
            "unexpected keyword.*async_handler",
        ),
        (
            _native_backend(
                "api.register_slash_command('demo', command)",
                definitions="async def command(args):\n    return None",
            ),
            "register_slash_command.handler",
        ),
        (
            _native_backend(
                "api.register_slash_command('demo', command)",
                definitions="def command(ctx, args):\n    return None",
            ),
            "register_slash_command.handler",
        ),
        (
            _native_backend("api.register_skill_provider('skills', True)"),
            "register_skill_provider",
        ),
        (
            _native_backend("pass", register_args="self, api, required"),
            r"callable as register\(api\)",
        ),
        (
            _native_backend(
                "api.register_startup_hook('start', startup)",
                definitions="def startup(required):\n    pass",
            ),
            "register_startup_hook.callback",
        ),
        (
            _native_backend(
                "api.register_middleware(middleware)",
                definitions="async def middleware(ctx, config):\n    pass",
            ),
            "register_middleware.middleware_factory",
        ),
        (
            _native_backend(
                "api.register_control_command(Handler())",
                definitions=(
                    "class Handler:\n"
                    "    command_name = '/demo'\n"
                    "    def handle(self, context):\n"
                    "        pass"
                ),
            ),
            "register_control_command.handler",
        ),
        (
            _native_backend(
                "configure(api)",
                definitions=(
                    "def configure(api):\n"
                    "    api.register_skill_provider('skills')"
                ),
            ),
            "PluginApi value escapes",
        ),
    ],
)
def test_native_plugin_rejects_invalid_registration_contract(
    tmp_path: Path,
    backend: str,
    message: str,
) -> None:
    _write_native_plugin(tmp_path, backend)

    with pytest.raises(ValueError, match=message):
        CompatibilityTester._test_native_plugin(tmp_path)


def test_native_plugin_test_accepts_valid_registration_contract(
    tmp_path: Path,
) -> None:
    _write_native_plugin(
        tmp_path,
        "async def command(ctx, args):\n"
        "    return None\n\n"
        "class NativePlugin:\n"
        "    def register(self, api):\n"
        "        api.register_skill_provider(\n"
        "            'skills', enabled_by_default=True, channels=['all'])\n"
        "        api.register_slash_command(\n"
        "            name='demo', handler=command, help_text='Demo')\n\n"
        "plugin = NativePlugin()\n",
    )

    result = CompatibilityTester._test_native_plugin(tmp_path)

    assert result.passed
    assert "plugin_api_calls_validated=2" in result.evidence


def test_codex_content_adapter_generates_a_native_plugin_contract(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / ".codex-plugin/plugin.json"
    skill = tmp_path / "skills/building-native-ui/SKILL.md"
    manifest.parent.mkdir()
    skill.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps(
            {
                "name": "expo",
                "version": "1.0.2",
                "description": "Expo workflows",
                "skills": "./skills/",
                "interface": {"displayName": "Expo"},
            },
        ),
        encoding="utf-8",
    )
    skill.write_text(
        "---\nname: building-native-ui\ndescription: Build Expo UI\n---\n",
        encoding="utf-8",
    )
    plugin = SourcePlugin(
        source_id="expo@openai-curated-remote",
        name="Expo",
        marketplace="openai-curated-remote",
        install_source=str(tmp_path),
        metadata={"adapter": "codex_content_bundle_v1"},
    )

    try:
        result = CompatibilityTester._test_plugin(plugin)
    except Exception as exc:  # pragma: no cover - assertion reports the cause
        pytest.fail(f"Codex content adapter did not build a wrapper: {exc}")

    assert result.passed
    assert "adapter=codex_content_bundle_v1" in result.evidence
    assert "plugin_api_calls_validated=1" in result.evidence


def test_codex_plugin_mcp_resolves_command_in_staged_container(
    tmp_path: Path,
) -> None:
    command = tmp_path / "scripts/launch"
    command.parent.mkdir()
    command.write_text("#!/bin/sh\n", encoding="utf-8")
    command.chmod(0o700)
    plugin_id = "tools@openai-bundled"
    server = SourceMCPServer(
        source_id="codex:plugin-mcp:tools:server",
        name="server",
        command="./scripts/launch",
        metadata={
            "source_plugin": plugin_id,
            "source_plugin_relative_cwd": ".",
        },
    )
    inventory = ProviderInventory(
        provider_id="codex",
        provider_name="Codex",
        detected=True,
        plugins=[
            SourcePlugin(
                source_id=plugin_id,
                name="Tools",
                marketplace="openai-bundled",
                install_source=str(tmp_path),
            ),
        ],
        mcp_servers=[server],
    )
    tester = CompatibilityTester(
        SimpleNamespace(workspace_dir=tmp_path),
        inventory,
        SimpleNamespace(),
    )

    result = tester._test_mcp(server)

    assert result.passed
    assert any("plugin_runtime=" in item for item in result.evidence)


def test_inspect_returns_exact_live_plugin_api_signatures(tmp_path: Path):
    workspace = _Workspace(tmp_path, lambda _context: None)
    tester = CompatibilityTester(
        workspace,
        ProviderInventory(
            provider_id="qoder",
            provider_name="Qoder",
            detected=True,
        ),
        SimpleNamespace(),
    )

    contract = tester.environment()["plugin_contract"]
    expected_methods = {
        name
        for name, method in inspect.getmembers(PluginApi, inspect.isfunction)
        if not name.startswith("_") and name != "set_registry"
    }
    api_methods = {item.split("(", 1)[0] for item in contract["api"]}
    assert api_methods == expected_methods
    slash = next(
        item
        for item in contract["api"]
        if item.startswith("register_slash_command")
    )
    skills = next(
        item
        for item in contract["api"]
        if item.startswith("register_skill_provider")
    )
    assert "handler" in slash
    assert "async_handler" not in slash
    assert "*, enabled_by_default" in skills
    assert contract["callbacks"]["register_uninstall_hook.callback"] == (
        "sync or async callable (*, plugin_id, delete_files)"
    )


def test_large_plugin_checklist_uses_behavioral_entries_not_doc_corpus(
    tmp_path: Path,
) -> None:
    files = {
        ".qoder-plugin/plugin.json": "{}",
        "skills/demo/SKILL.md": "Demo",
        "skills/demo/scripts/run.py": "print('run')",
        "skills/demo/references/doc.md": "Long reference corpus",
        "agents/reviewer.md": "Reviewer",
        "docs/examples/agents/example.md": "Not an installed agent",
        ".DS_Store": "binary metadata placeholder",
        "__MACOSX/._plugin.json": "archive metadata",
    }
    for relative, content in files.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    components = discover_components(
        AssetType.PLUGIN,
        SimpleNamespace(install_source=str(tmp_path)),
    )
    paths = {path for item in components for path in item.paths}
    assert "skills/demo/SKILL.md" in paths
    assert "skills/demo/scripts/run.py" in paths
    assert "agents/reviewer.md" in paths
    assert "skills/demo/references/doc.md" not in paths
    assert "docs/examples/agents/example.md" not in paths
    assert ".DS_Store" not in paths
    assert "__MACOSX/._plugin.json" not in paths


def test_codex_plugin_is_staged_and_read_as_component_container(
    tmp_path: Path,
) -> None:
    source = tmp_path / "cache/cloudflare/0.1.2"
    files = {
        ".codex-plugin/plugin.json": json.dumps(
            {
                "name": "cloudflare",
                "version": "0.1.2",
                "skills": "./skills/",
                "mcpServers": "./.mcp.json",
            },
        ),
        "skills/cloudflare/SKILL.md": "Cloudflare guidance",
        ".mcp.json": '{"mcpServers":{}}',
    }
    for relative, content in files.items():
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    inventory = ProviderInventory(
        provider_id="codex",
        provider_name="Codex",
        detected=True,
        plugins=[
            SourcePlugin(
                source_id="cloudflare@openai-curated-remote",
                name="Cloudflare",
                marketplace="openai-curated-remote",
                metadata={
                    "adapter": "codex_content_bundle_v1",
                    "install_path": str(source),
                },
            ),
        ],
    )

    warnings = stage_local_assets(inventory, tmp_path / "staging")
    components = component_map(inventory)[
        "plugins:cloudflare@openai-curated-remote"
    ]

    assert not warnings
    assert Path(inventory.plugins[0].install_source) != source
    assert {item.kind for item in components} >= {"manifest", "skill", "mcp"}
    assert any(
        ".codex-plugin/plugin.json" in item.paths for item in components
    )


@pytest.mark.asyncio
async def test_mission_classifies_portable_asset_for_enabled_migration(
    tmp_path: Path,
) -> None:
    progress_messages = []

    async def progress(message: str) -> None:
        progress_messages.append(message)

    async def action(context):
        tested = await context.test_asset("skills:demo")
        assert tested["passed"], tested
        await context.classify_asset("skills:demo", "migrate", "native")

    workspace = _Workspace(tmp_path, action)
    inventory = ProviderInventory(
        provider_id="codex",
        provider_name="Codex",
        detected=True,
        skills=[
            _skill(
                tmp_path,
                "---\nname: demo\ndescription: demo\n---\nUse QwenPaw.\n",
            ),
        ],
    )
    result = await run_adaptation_loop(
        workspace,
        inventory,
        "migration-1",
        progress,
    )
    manifest = load_manifest(result.manifest_path)
    assert result.status == "completed"
    assert manifest.assets[0].zone is AssetZone.MIGRATE
    assert result.summary_path.is_file()
    mission_prd = json.loads(
        (result.summary_path.parent / "mission" / "prd.json").read_text(
            encoding="utf-8",
        ),
    )
    assert mission_prd["userStories"][0]["passes"] is True
    assert any("正在测试 Skill「demo」" in item for item in progress_messages)
    assert any("测试通过，可以迁移" in item for item in progress_messages)
    assert [
        item.request_context["portability_phase"]
        for item in workspace.requests
    ] == ["mission_repair"]


@pytest.mark.asyncio
async def test_mission_repairs_then_retests_skill(tmp_path: Path) -> None:
    async def action(context):
        await context.write_file(
            "skills:demo",
            "SKILL.md",
            "---\nname: demo\ndescription: demo\n---\nRun QwenPaw tools.\n",
        )
        with pytest.raises(RuntimeError, match="latest change"):
            context.store.classify(
                "skills:demo",
                AssetZone.MIGRATE,
                "not retested",
            )
        await context.test_asset("skills:demo")
        await context.classify_asset("skills:demo", "migrate", "retested")

    workspace = _Workspace(tmp_path, action)
    inventory = ProviderInventory(
        provider_id="codex",
        provider_name="Codex",
        detected=True,
        skills=[
            _skill(
                tmp_path,
                "---\nname: demo\ndescription: demo\n---\nRun codex exec.\n",
            ),
        ],
    )
    result = await run_adaptation_loop(workspace, inventory, "migration-2")
    assert result.asset_zones["skills:demo"] == "migrate"
    staged = inventory.skills[0].directory / "SKILL.md"
    assert "QwenPaw tools" in staged.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_static_failure_keeps_remote_plugin_in_repair(
    tmp_path: Path,
) -> None:
    async def action(context):
        tested = await context.test_asset("plugins:remote")
        assert not tested["passed"]

    workspace = _Workspace(tmp_path, action)
    inventory = ProviderInventory(
        provider_id="qoder",
        provider_name="Qoder",
        detected=True,
        plugins=[
            SourcePlugin(
                source_id="remote",
                name="remote",
                marketplace="remote",
                install_source="https://example.test/plugin.zip",
            ),
        ],
    )
    result = await run_adaptation_loop(workspace, inventory, "migration-3")
    assert result.status == "stopped_limit"
    assert result.asset_zones["plugins:remote"] == "repair"
    assert "每项最多 4 次尝试" in result.summary_path.read_text(
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_qoder_marketplace_skill_plugin_reaches_migrate_zone(
    tmp_path: Path,
) -> None:
    source = tmp_path / "cangjie-skills"
    skill = source / "skills/cangjie/SKILL.md"
    manifest = source / ".qoder-plugin/plugin.json"
    skill.parent.mkdir(parents=True)
    manifest.parent.mkdir()
    skill.write_text(
        "---\nname: cangjie\ndescription: Cangjie docs\n---\n\n"
        "Answer Cangjie language questions.\n",
        encoding="utf-8",
    )
    manifest.write_text(
        '{"name":"cangjie-skills","version":"1.0.0","skills":"./skills/"}',
        encoding="utf-8",
    )

    async def action(context):
        tested = await context.test_asset("plugins:cangjie")
        assert tested["passed"], tested
        await context.classify_asset(
            "plugins:cangjie",
            "migrate",
            "native checks passed",
        )

    inventory = ProviderInventory(
        provider_id="qoder",
        provider_name="Qoder",
        detected=True,
        plugins=[
            SourcePlugin(
                source_id="cangjie",
                name="cangjie-skills",
                marketplace="community",
                version="1.0.0",
                install_source=str(source),
                metadata={
                    "adapter": "qoder_skill_only_v1",
                    "canonical_plugin_source": str(source.resolve()),
                    "skills_relative_path": "skills",
                },
            ),
        ],
    )

    result = await run_adaptation_loop(
        _Workspace(tmp_path, action),
        inventory,
        "migration-cangjie",
    )

    assert result.asset_zones["plugins:cangjie"] == "migrate"


@pytest.mark.asyncio
async def test_missing_mission_mode_fails_safe_into_repair(
    tmp_path: Path,
) -> None:
    async def action(context):
        await context.test_asset("skills:demo")

    workspace = _Workspace(tmp_path, action)
    workspace.plugins.modes = []
    inventory = ProviderInventory(
        provider_id="codex",
        provider_name="Codex",
        detected=True,
        skills=[
            _skill(
                tmp_path,
                "---\nname: demo\ndescription: demo\n---\n",
            ),
        ],
    )
    result = await run_adaptation_loop(workspace, inventory, "migration-4")
    assert result.status == "stopped_limit"
    assert result.asset_zones["skills:demo"] == "repair"
    summary = result.summary_path.read_text(encoding="utf-8")
    assert "停止原因：无法完成 QwenPaw Mission" in summary


@pytest.mark.asyncio
async def test_rejected_secret_repair_does_not_mutate_source(
    tmp_path: Path,
) -> None:
    server = SourceMCPServer(
        source_id="safe-mcp",
        name="safe-mcp",
        command=sys.executable,
    )

    async def action(context):
        with pytest.raises(ValueError, match="contains a secret"):
            await context.update_asset(
                "mcp:safe-mcp",
                "args",
                '["--api-key", "sk-do-not-persist"]',
            )
        assert server.args == []
        tested = await context.test_asset("mcp:safe-mcp")
        assert tested["passed"], tested
        await context.classify_asset("mcp:safe-mcp", "migrate", "native")

    workspace = _Workspace(tmp_path, action)
    inventory = ProviderInventory(
        provider_id="codex",
        provider_name="Codex",
        detected=True,
        mcp_servers=[server],
    )

    result = await run_adaptation_loop(workspace, inventory, "migration-5")

    assert result.asset_zones["mcp:safe-mcp"] == "migrate", result.warnings
    persisted = result.manifest_path.read_text(encoding="utf-8")
    assert "sk-do-not-persist" not in persisted


@pytest.mark.asyncio
async def test_mixed_plugin_is_one_asset_with_component_review_and_repair(
    tmp_path: Path,
) -> None:
    source = tmp_path / "mixed-plugin"
    files = {
        ".qoder-plugin/plugin.json": '{"name":"mixed","version":"1"}',
        "skills/report/SKILL.md": "Report skill",
        "commands/report.md": "Run Qoder command",
        "agents/reviewer.md": "Qoder review agent",
        "hooks/hooks.json": '{"onStart":"./start.sh"}',
        "hooks/start.sh": "qoder --start",
        "rules/review.md": "Review rules",
        "mcp.json": '{"mcpServers":{}}',
    }
    for relative, content in files.items():
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    async def action(context):
        await context.write_file(
            "plugins:mixed",
            "plugin.json",
            json.dumps(
                {
                    "id": "mixed",
                    "version": "1.0.0",
                    "entry": {"backend": "plugin.py"},
                },
            ),
        )
        await context.write_file(
            "plugins:mixed",
            "plugin.py",
            "class MixedPlugin:\n"
            "    def register(self, api):\n"
            "        api.register_skill_provider(skills_dir=__import__(\n"
            "            'pathlib').Path(__file__).parent / 'skills')\n\n"
            "plugin = MixedPlugin()\n",
        )
        tested = await context.test_asset("plugins:mixed")
        assert tested["passed"], tested
        await context.classify_asset(
            "plugins:mixed",
            "migrate",
            "native plugin test passed",
        )

    inventory = ProviderInventory(
        provider_id="qoder",
        provider_name="Qoder",
        detected=True,
        plugins=[
            SourcePlugin(
                source_id="mixed",
                name="mixed",
                marketplace="local",
                install_source=str(source),
            ),
        ],
    )
    result = await run_adaptation_loop(
        _Workspace(tmp_path, action),
        inventory,
        "migration-mixed",
    )
    assert result.asset_zones["plugins:mixed"] == "migrate"
    staged = Path(inventory.plugins[0].install_source)
    assert (staged / "hooks/start.sh").is_file()


@pytest.mark.asyncio
async def test_mission_repairs_assets_in_parallel_with_isolated_scope(
    tmp_path: Path,
) -> None:
    first = _skill(tmp_path, "---\nname: demo\n---\nInvalid.\n")
    first.source_id = "first"
    first.name = "first"
    second_root = tmp_path / "second-skill"
    second_root.mkdir()
    (second_root / "SKILL.md").write_text(
        "---\nname: second\ndescription: valid\n---\nValid.\n",
        encoding="utf-8",
    )
    second = SourceSkill(
        source_id="second",
        name="second",
        directory=second_root,
    )

    async def action(context):
        key = context.active_asset_key
        other = "skills:second" if key == "skills:first" else "skills:first"
        with pytest.raises(PermissionError, match="assigned asset"):
            await context.test_asset(other)
        tested = await context.test_asset(key)
        if tested["passed"]:
            await context.classify_asset(key, "migrate", "passed")
            return
        await context.write_file(
            key,
            "SKILL.md",
            "---\nname: first\ndescription: repaired\n---\nValid.\n",
        )
        assert (await context.test_asset(key))["passed"]
        await context.classify_asset(key, "migrate", "passed")

    workspace = _Workspace(tmp_path, action)
    result = await run_adaptation_loop(
        workspace,
        ProviderInventory(
            provider_id="codex",
            provider_name="Codex",
            detected=True,
            skills=[first, second],
        ),
        "migration-parallel",
    )
    assert result.status == "completed"
    assert workspace.max_active_queries == 2
    phases = [
        item.request_context["portability_phase"]
        for item in workspace.requests
    ]
    assert phases.count("mission_repair") == 2
