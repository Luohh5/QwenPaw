# Final local protocol checklist — 2026-10-24

Production source and release artifacts remained frozen. All new assertions use the real public SDK loader/bridge or the vendor adapter with fake vendor clients; no account, vendor executable, or network call was made. The 75-run footprint comparison is unaffected by test-only changes.

The child-process restart driver sets `PIP_NO_INDEX=1` and `UV_OFFLINE=1`; both selected-vendor reruns passed after that final test-only guard.

| Checklist gap | Evidence |
| --- | --- |
| State and native ID across host restart | `test_host_restart_retains_disabled_state_and_resumes_native_id` starts two **different Python processes** per selected-vendor interpreter, installs through `PluginTestHost`, persists disabled config and native ID, boots through installed-directory discovery, re-enables, and resumes that ID. Codex and Qoder each passed separately. Existing same-process re-enable tests remain. |
| Codex failed terminal | `test_failed_codex_turn_reaches_public_failed_stream` injects a failed `turn/completed` vendor RPC through the installed adapter and asserts the public `ChatEvent` ends failed with the vendor error text. |
| Permission presets and approvals | `test_codex_permission_presets_reach_vendor_turn` covers unrestricted/ask/read-only sandbox and approval-policy projection; `test_qoder_permission_presets_reach_vendor_client` covers bypass/default/plan projection. Each vendor's live callback test covers approved, rejected, cancelled, and timeout outcomes with its actual vendor response type. `test_approval_uses_host_identity_and_original_decisions` additionally exercises the host TIMEOUT→`timeout` result. These are protocol fakes; they do not claim real account behavior. |
| `/new` and `/clear` | The existing `test_turn_envelopes_attachments_commands_clear_and_history` is parameterized over both commands, retaining its reset and persisted-history assertions through the shared host bridge. The preexisting vendor command/reset tests cover their own native protocols. |
| Simultaneous identity and credential custody | `test_concurrent_same_session_approval_and_credentials_stay_scoped` holds two real `BackendExecution` turns with the same session ID in different workspaces/users/channels. It checks distinct projected MCP secrets, simultaneous pending approvals, wrong-actor rejection, and separate approval results. Both vendor callback tests above exercise conversion from the public call's result to their respective vendor protocol. This composition covers the shared host identity/credential path without duplicating an artificial cross-vendor harness. |
| Active-turn force unload | `test_force_unload_interrupts_live_codex_turn` and `test_force_unload_interrupts_live_qoder_turn` hold a real installed-plugin turn, invoke public `host.unload(..., interrupt_tasks=True)`, and assert quiescence, vendor client stop/interrupt/disconnect, and unavailable catalog state. The existing generic busy/failed-close lifecycle tests retain their distinct ownership checks. |

Exact commands were run from the repository root, all after final formatting:

```sh
PYTHONPATH=src:packages/qwenpawmail-mcp/src QWENPAW_WORKING_DIR=/private/tmp/harness-protocol-core QWENPAW_RUNTIME_ID=protocol-core PYTHONDONTWRITEBYTECODE=1 .venv-test/bin/python -m pytest tests/unit/plugins/test_sdk_harness_bridge.py -q -p no:cacheprovider --tb=short
# 11 passed

PYTHONPATH=src:packages/qwenpawmail-mcp/src QWENPAW_WORKING_DIR=/private/tmp/harness-protocol-codex QWENPAW_RUNTIME_ID=protocol-codex PYTHONDONTWRITEBYTECODE=1 /private/tmp/qwenpaw-harness-vendor-tests-20261023/bin/python -m pytest plugins/harness/codex/tests/test_plugin_contract.py plugins/harness/codex/tests/test_backend_behavior.py -q -k 'not test_explicit_install_registers_only_codex' -p no:cacheprovider --tb=short
# 21 passed, 1 deselected (raw developer node_modules symlinks; strict checker previously passed on clean release ZIP)

PYTHONPATH=src:packages/qwenpawmail-mcp/src QWENPAW_WORKING_DIR=/private/tmp/harness-protocol-qoder QWENPAW_RUNTIME_ID=protocol-qoder PYTHONDONTWRITEBYTECODE=1 /private/tmp/qwenpaw-harness-qoder-tests-20261023/bin/python -m pytest plugins/harness/qoder/tests/test_plugin_contract.py plugins/harness/qoder/tests/test_backend_behavior.py -q -k 'not test_qoder_registers_without_codex_sdk_or_vendor_probe' -o asyncio_mode=auto -p no:cacheprovider --tb=short
# 15 passed, 1 deselected (same developer-tree checker condition)

QWENPAW_HARNESS_VENDOR=codex PYTHONPATH=src:packages/qwenpawmail-mcp/src QWENPAW_WORKING_DIR=/private/tmp/harness-protocol-codex QWENPAW_RUNTIME_ID=protocol-codex PYTHONDONTWRITEBYTECODE=1 /private/tmp/qwenpaw-harness-vendor-tests-20261023/bin/python -m pytest tests/integration/test_harness_process_restart.py -q -p no:cacheprovider --tb=short
# 1 passed; two child processes

QWENPAW_HARNESS_VENDOR=qoder PYTHONPATH=src:packages/qwenpawmail-mcp/src QWENPAW_WORKING_DIR=/private/tmp/harness-protocol-qoder QWENPAW_RUNTIME_ID=protocol-qoder PYTHONDONTWRITEBYTECODE=1 /private/tmp/qwenpaw-harness-qoder-tests-20261023/bin/python -m pytest tests/integration/test_harness_process_restart.py -q -o asyncio_mode=auto -p no:cacheprovider --tb=short
# 1 passed; two child processes

PYLINTHOME=/private/tmp/harness-protocol-pylint pre-commit run --files plugins/harness/codex/tests/test_plugin_contract.py plugins/harness/codex/tests/test_backend_behavior.py plugins/harness/qoder/tests/test_plugin_contract.py plugins/harness/qoder/tests/test_backend_behavior.py tests/unit/plugins/test_sdk_harness_bridge.py tests/integration/test_harness_process_restart.py
# all applicable hooks passed
```

The first restart test run failed on a test assumption: disabled installed plugins remain visible in the catalog with an unavailable status. The assertion now checks unavailable status while retaining the persisted `enabled=False` requirement. The first concurrent-identity run failed on a test-only Python await/generator expression, then on a misplaced assertion while inserting the case; both test errors were corrected. No production RED was observed. The final commands above all passed. An unselected default run of the restart test skips by design; only the two selected-vendor commands count as acceptance.

The four modified plugin test files were then copied byte-for-byte into the previously extracted clean release-ZIP test trees, leaving the ZIPs untouched. Full plugin suites, including the strict source checker, passed **Codex 66/66** and **Qoder 36/36**. `cmp` verified every copied test file; both ZIP SHA-256 hashes remain `5f3445b9a86e096bff4c61668cf12c7978909e80041b27e9f12179b10ff9fad4` and `3c6149d62d005f19f6263a45abfd749e49aad33f912b5fde20e6a9ceb736190c` respectively.

```sh
# cwd: /private/tmp/qwenpaw-harness-final-stage-20261024
PYTHONPATH=/private/tmp/qwenpaw-harness-final-stage-20261024:/Users/luohh/Documents/Qwenpaw/QwenPaw-main_plugin/src:/Users/luohh/Documents/Qwenpaw/QwenPaw-main_plugin/packages/qwenpawmail-mcp/src QWENPAW_WORKING_DIR=/private/tmp/harness-protocol-codex QWENPAW_RUNTIME_ID=protocol-codex PYTHONDONTWRITEBYTECODE=1 /private/tmp/qwenpaw-harness-vendor-tests-20261023/bin/python -m pytest plugins/harness/codex/tests -q -c /Users/luohh/Documents/Qwenpaw/QwenPaw-main_plugin/pyproject.toml -p no:cacheprovider --tb=short
# 66 passed
PYTHONPATH=/private/tmp/qwenpaw-harness-final-stage-20261024:/Users/luohh/Documents/Qwenpaw/QwenPaw-main_plugin/src:/Users/luohh/Documents/Qwenpaw/QwenPaw-main_plugin/packages/qwenpawmail-mcp/src QWENPAW_WORKING_DIR=/private/tmp/harness-protocol-qoder QWENPAW_RUNTIME_ID=protocol-qoder PYTHONDONTWRITEBYTECODE=1 /private/tmp/qwenpaw-harness-qoder-tests-20261023/bin/python -m pytest plugins/harness/qoder/tests -q -c /Users/luohh/Documents/Qwenpaw/QwenPaw-main_plugin/pyproject.toml -o asyncio_mode=auto -p no:cacheprovider --tb=short
# 36 passed
```

Actual OAuth/PAT/API-key login, frozen macOS runtime launch, and Windows/Linux runtime behavior still require their separate platform/account acceptance and are not marked complete by these tests.
