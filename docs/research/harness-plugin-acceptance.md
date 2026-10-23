# Optional Harness migration and acceptance

Status: implementation and final acceptance are in progress. Codex and Qoder are
independently optional SDK 1.1 plugins. A passing fake-client test does not establish
live account compatibility or frozen-platform runtime acceptance. No release has
been uploaded by this work.

## Upgrade boundary

This release requires an explicit prepared/operator migration before starting the
updated host. Existing CLI update installs the package and asks for a restart;
desktop update replaces/restarts the application. Neither prepares verified
Harness bundles or their offline dependency environment automatically.

Stop the old host, obtain the verified packages for used backends and prepare
matching dependencies, then use the installed CLI's dry-run/apply operation as
documented in [the plugin guide](../../website/public/docs/plugins.en.md#optional-codex-and-qoder-backends).
The source script and installed CLI share one formal offline installation
implementation. Explicit disable has priority; no third-party Agent means an
empty plan. Missing preparation preserves Agent choices and data and reports the
required installation action. It does not justify silently switching to native.
[Migration tests](../../tests/integration/test_harness_upgrade.py) verify the
empty native plan, missing-package/dependency actions, real release ZIP apply,
idempotence and explicit disable preservation. Apply invokes the formal loader
with its final dependency gate in check-only mode; it never downloads packages
or activates plugin code. Agent and account bytes remain unchanged.
An already installed plugin is retained and audited against its own requirements,
even when a different candidate bundle is supplied. Missing installed dependencies
remain a preparation action; they do not trigger a reinstall or network access.

## Functional evidence

“Protocol” below means the real public SDK/loader or extracted backend with fake
vendor transport, temporary state and no live account. The two vendor columns are
independent; shared host lifecycle tests supplement both rather than establishing
untested vendor behavior.

| Capability | Codex evidence | Qoder evidence |
| --- | --- | --- |
| Plugin declaration and explicit installation | [Contract](../../plugins/harness/codex/tests/test_plugin_contract.py), [formal ZIP install](../../tests/integration/test_harness_install_isolation.py) | [Contract](../../plugins/harness/qoder/tests/test_plugin_contract.py), [formal ZIP install](../../tests/integration/test_harness_install_isolation.py) |
| CLI discovery and explicit executable path | [Discovery tests](../../plugins/harness/codex/tests/test_discovery.py) | [Discovery tests](../../plugins/harness/qoder/tests/test_discovery.py) |
| Account/login/status protocol | [Status/login/models](../../plugins/harness/codex/tests/test_backend_behavior.py), [actual Settings component tests](../../console/src/plugins/vendorHarnessSettings.test.tsx); live OAuth/API-key login not run | [PAT/status/login cleanup](../../plugins/harness/qoder/tests/test_backend_behavior.py), [actual Settings component tests](../../console/src/plugins/vendorHarnessSettings.test.tsx); live browser/PAT login not run |
| Model, effort, text/reasoning/tool stream and attachments | [Backend behavior](../../plugins/harness/codex/tests/test_backend_behavior.py), [failed RPC to public failed terminal](../../plugins/harness/codex/tests/test_plugin_contract.py#L159), [projection](../../plugins/harness/codex/tests/test_projection.py) | [Backend behavior](../../plugins/harness/qoder/tests/test_backend_behavior.py), [event mapping](../../plugins/harness/qoder/tests/test_event_mapper.py) |
| Permission presets, approvals and turn cancellation | [Permission presets](../../plugins/harness/codex/tests/test_backend_behavior.py#L345), [public approval outcomes](../../plugins/harness/codex/tests/test_backend_behavior.py#L526) | [Permission presets](../../plugins/harness/qoder/tests/test_backend_behavior.py#L497), [vendor permission outcomes](../../plugins/harness/qoder/tests/test_backend_behavior.py#L439) |
| Native Skill/MCP discovery and host projection | [Read-only discovery/projection](../../plugins/harness/codex/tests/test_backend_behavior.py) | [Read-only Skills/projection](../../plugins/harness/qoder/tests/test_backend_behavior.py); native MCP discovery is explicitly unsupported |
| Commands, new/clear and materialized history | [Native commands/reset](../../plugins/harness/codex/tests/test_backend_behavior.py#L455), [both host commands with saved history](../../tests/unit/plugins/test_sdk_harness_bridge.py#L20) | [Native commands/history/reset](../../plugins/harness/qoder/tests/test_backend_behavior.py#L226), [both host commands](../../tests/unit/plugins/test_sdk_harness_bridge.py#L20) |
| Original provider-session resume and legacy mapping | [State migration](../../plugins/harness/codex/tests/test_state_migration.py) | [State migration](../../plugins/harness/qoder/tests/test_state_migration.py) |
| User/channel/workspace identity isolation | [State migration](../../plugins/harness/codex/tests/test_state_migration.py), [vendor callback](../../plugins/harness/codex/tests/test_backend_behavior.py#L526) | [State migration](../../plugins/harness/qoder/tests/test_state_migration.py), [vendor callback](../../plugins/harness/qoder/tests/test_backend_behavior.py#L439) |
| Simultaneous same-session host identity/credential custody | [Real BackendExecution dual-identity turns](../../tests/unit/plugins/test_sdk_harness_bridge.py#L297): separate MCP secrets, concurrent approvals and wrong-actor rejection | Same shared host test; the vendor callback above separately verifies public-result conversion |
| Disable/reload/concurrent work and retained failed ownership | [Active-turn force unload](../../plugins/harness/codex/tests/test_plugin_contract.py#L208), [host busy/failed-close lifecycle](../../tests/unit/plugins/test_sdk_harness_lifecycle.py), [HTTP races](../../tests/integration/test_harness_plugin_http.py) | [Active-turn force unload](../../plugins/harness/qoder/tests/test_plugin_contract.py#L107), [host lifecycle](../../tests/unit/plugins/test_sdk_harness_lifecycle.py), [HTTP races](../../tests/integration/test_harness_plugin_http.py) |
| Disable→enable resumes the same native session | [Real plugin lifecycle, fake app-server](../../plugins/harness/codex/tests/test_plugin_contract.py) asserts the next generation calls thread/resume with the original ID | [Real plugin lifecycle, fake SDK client](../../plugins/harness/qoder/tests/test_plugin_contract.py) asserts the next generation receives the original resume ID |
| Persisted disable and native ID across host-process restart | [Two distinct Python children, selected Codex environment](../../tests/integration/test_harness_process_restart.py#L108): install, disable, rediscover, re-enable and resume | [Same real-loader scenario, selected Qoder environment](../../tests/integration/test_harness_process_restart.py#L108); fake vendor transport, not frozen-host execution |
| Missing-owner Agent management and uninstall preservation | [Agent dispatch](../../tests/unit/app/test_backend_dispatch.py) verifies list/read/rename/delete, unavailable execution, and saved Agent/account bytes after formal uninstall | [Shared host contract](../../tests/unit/app/test_backend_dispatch.py); vendor state tests above independently verify Qoder session identity |
| PawPort plugin requirement and archive history | [Requirement gates](../../tests/unit/portability/test_harness_plugin_requirement.py), [migration providers](../../tests/unit/portability/test_migration_providers.py) | [Requirement gates](../../tests/unit/portability/test_harness_plugin_requirement.py), [migration providers](../../tests/unit/portability/test_migration_providers.py) |
| Real macOS frozen runtime and helper dependency install | Build completed; runtime/helper QA still open | Build completed; runtime/helper QA still open |
| Real Windows/Linux platform execution | Not run locally; prepared CI is not execution evidence | Not run locally; prepared CI is not execution evidence |

Final clean release-tree suites passed **Codex 66/66** and **Qoder 36/36**,
including the strict source checker with each pinned SDK in its own environment.
Latest test files were copied byte-identically into the extracted final ZIP trees;
the ZIP payloads and hashes below were unchanged. The shared bridge passed **11**
tests; the two-child restart test passed **once per selected vendor**.
[Exact protocol commands and scope](harness-plugin-protocol-checklist.md)
cover both reset commands, all four approval outcomes, vendor permission presets,
Codex failed terminal, simultaneous host identities and active-turn force unload.
Independent review of this protocol completion passed with no Critical/Important findings.
Safe standalone checks returned Codex CLI **0.144.4** and Qoder CLI **1.0.47** without
logging in. Live account behavior remains unverified.

The final targeted vendor-free core group passed **140 tests**; its two explicit
vendor-apply cases ran in the prepared both-SDK group (**5 passed**). That group
skipped the vendor-free missing-SDK gate, which passed in core. Actual HTTP **7**,
ACP **22**, all three catalog variants, and each vendor's formal ZIP isolation
case also passed. These are source/ZIP protocol checks with fake vendor clients,
not live accounts or frozen-host execution. The final source rereview reports
QUALITY PASS for the three API/catalog/loader fixes.

## Packaging and SDK evidence

- [Distribution tests](../../tests/integration/test_harness_distribution.py) build
  a fresh actual wheel and protect public SDK/frontend/templates, mail MCP and
  all four Hub implementations while excluding the old runtime package.
- [ZIP tests](../../tests/unit/plugins/test_generate_plugin_metadata.py) inspect
  built entries and own dependencies, exclude the other vendor, and reject a
  missing frontend entry. Both release paths build against the real public SDK.
- [Final SDK artifact acceptance](harness-sdk-final-acceptance.json) records a
  fresh external virtualenv using the non-editable final wheel, with no checkout
  Python path or vendor SDKs. Strict mypy, **228 author tests**, exported template
  compliance and actual runtime all pass. Two unknown-marker warnings come from
  running author tests without repository conftest. Frontend exported from that
  installed wheel passes NodeNext, bundler and runtime checks. The earlier
  [B3 receipt](harness-sdk-acceptance.json) remains historical.
- The actual final macOS onedir rebuild completed with canonical CPython
  **3.13.14** and PyInstaller **6.22.3**, exit **0** in **423.857s**. Its
  [inventory](harness-plugin-final-frozen-inventory.json) records **10,437 files**,
  **32,587 PYZ modules** per executable, zero forbidden old/vendor package roots,
  and the six previously missing required dependency metadata directories.
  Migration, mail MCP, four Hub implementations and all **1,746** Console files
  are retained; Console bytes match the final build. This is a payload audit.
  The [actual runtime receipt](harness-plugin-final-frozen-runtime.json) is
  **NOT_ACCEPTED**: isolated `qwenpaw --version` outside the tool sandbox exited
  **-9** with no output, although each Mach-O executable's signature verifies.
  No OS/loader cause is inferred and no security policy was changed. A minimal
  same-Python/PyInstaller hello program with no QwenPaw imports also exited -9
  without output (11.47s); this reproduces the symptom outside QwenPaw, without
  establishing its root cause.
  A separately labeled fresh-wheel test emulated only
  frozen routing and installed both real ZIPs through the canonical helper and
  offline target; each target contained only its own SDK/CLI distributions.
  This passed but does not establish frozen-host execution. Formal plugin
  installation from the actual frozen host remains unexecuted. Windows/Linux
  execution is unavailable locally.

The refreshed final receipt identifies a **1,546-file wheel** (11,605,169 bytes,
SHA-256 `1d2745dc2af383b606d9fa44d393bf88d01ba64d84c2977272c5e300bd077169`)
and **1,827-file sdist** (10,538,987 bytes,
SHA-256 `f1b8c73d9ff184ee84d52682a61cfed21c429c46d04b782f68b3fda25acd0b3e`).
Both contain zero old runtime or vendor payloads; exact local paths and source
manifest identity are in the receipt. The **228** external author tests passed
again in **171s**, with strict types, template runtime and the three exported
frontend checks. Development `node_modules` symlinks remain subject to the strict SDK
checker; archive checks use exact runtime contents, not a relaxed rule.

Final release ZIPs each contain **11 runtime entries**, including their built
frontend, without tests, node_modules or bytecode. They were formally installed
in separate vendor environments; only the matching Harness was available and
the opposite SDK remained absent. Local root:
`/private/tmp/qwenpaw-harness-final-plugins-20261024/harness/<plugin-id>/`.

| ZIP | Bytes | SHA-256 |
| --- | ---: | --- |
| `qwenpaw-harness-codex-1.0.0.zip` | 28,182 | `5f3445b9a86e096bff4c61668cf12c7978909e80041b27e9f12179b10ff9fad4` |
| `qwenpaw-harness-qoder-1.0.0.zip` | 23,448 | `3c6149d62d005f19f6263a45abfd749e49aad33f912b5fde20e6a9ceb736190c` |

The [final source/artifact closure](harness-plugin-final-source-closure.json)
confirms **1,544 core source snapshot files**, **1,746 Console files**, all **22 ZIP
payload entries** and the measurement driver match current runtime bytes. Later
test-only additions and staging change whole-worktree fingerprints; they do not
change those measured/built payloads or invalidate the recorded runtime comparison.
This byte closure does not establish account or platform runtime acceptance.

## Single-worker ownership

- [Host lifecycle/bridge](../../tests/unit/plugins/test_sdk_harness_lifecycle.py)
  covers stream/save custody, workspace close, settings rebuild and failed close.
- Codex [state](../../plugins/harness/codex/tests/test_state_migration.py) and
  [process tests](../../plugins/harness/codex/tests/test_backend_behavior.py) cover
  held filesystem operations, turn approvals, subprocess cleanup and pipe drain.
- Qoder [state tests](../../plugins/harness/qoder/tests/test_state_migration.py)
  cover repeated cancellation through disconnect/delete and consistent following
  turns; [behavior tests](../../plugins/harness/qoder/tests/test_backend_behavior.py)
  cover permission/login/client cleanup.
- Async clients/locks stay on their loop. Bounded filesystem/discovery workers
  finish before cancellation releases mapping/resource ownership. No whole
  lifecycle or Driver is moved to a worker.

## Native absence and footprint evidence

The old runtime package and temporary aliases are physically removed.
[Fresh-process absence tests](../../tests/integration/test_harness_absent.py)
guard actual app imports, vendor probing and subprocess creation, then run a real
native Workspace/Runtime/Agent turn and verify saved terminal history. They pass
both without vendor SDKs and with both preinstalled, with a disabled manifest
present. Generic saved history uses the [core session bridge](../../tests/unit/runtime/backends/test_session_bridge.py).

The [final same-environment comparison](harness-plugin-final-footprint.json)
completed **75 fresh processes: 15 scene/state groups × 5**, without failed
milestones. It uses the same pinned [environment](harness-plugin-final-environment.json)
for original archived source and reviewed current source, with both vendor SDKs
preinstalled. The driver verifies actual imported source origins and records
source fingerprints, driver and staged-manifest hashes alongside every raw sample.
It exercises real ASGI startup_ready, Agent listing and Workspace/native
Runtime/Agent chat with only model transport fixed. All **25 chat samples**
verify saved user and assistant history.

Values below are **median / maximum** over five runs. `ready` is core
startup_ready, not import completion; actual plugin activation is verified
separately before the live RSS/module snapshot and its timing is retained in the
JSON. RSS is the live process snapshot in MiB, not post-shutdown or process peak.

| State | Scene | Import ms | Core ready ms | Live RSS MiB |
| --- | --- | ---: | ---: | ---: |
| Original baseline | Startup | 2,598.2 / 3,020.8 | 5,682.5 / 6,006.7 | 431.17 / 432.44 |
| Original baseline | Agent list | 2,556.7 / 2,585.2 | 5,446.4 / 5,538.1 | 433.41 / 434.39 |
| Original baseline | Native chat | 2,564.1 / 2,772.3 | 5,409.5 / 5,757.4 | 444.11 / 444.17 |
| No plugins | Startup | 2,597.5 / 2,608.3 | 5,512.7 / 5,627.7 | 427.45 / 427.56 |
| No plugins | Agent list | 2,586.8 / 2,715.2 | 5,467.8 / 5,588.9 | 429.86 / 430.53 |
| No plugins | Native chat | 2,579.4 / 2,642.5 | 5,481.2 / 5,646.1 | 439.45 / 440.80 |
| Both disabled | Startup | 2,596.2 / 2,603.8 | 5,513.3 / 5,542.8 | 428.53 / 428.72 |
| Both disabled | Agent list | 2,570.8 / 3,022.0 | 5,521.5 / 5,958.9 | 429.17 / 434.89 |
| Both disabled | Native chat | 2,584.3 / 2,609.6 | 5,452.3 / 5,487.7 | 439.42 / 439.73 |
| Codex only | Startup | 2,569.0 / 2,689.8 | 5,515.8 / 5,701.0 | 433.30 / 436.78 |
| Codex only | Agent list | 2,611.6 / 3,012.6 | 5,497.7 / 5,932.2 | 434.69 / 441.69 |
| Codex only | Native chat | 2,602.1 / 2,637.0 | 5,575.5 / 5,706.5 | 439.09 / 439.30 |
| Qoder only | Startup | 2,608.5 / 2,651.5 | 5,504.5 / 5,534.8 | 433.17 / 434.03 |
| Qoder only | Agent list | 2,640.1 / 3,010.4 | 5,531.2 / 5,899.5 | 434.56 / 439.27 |
| Qoder only | Native chat | 2,588.4 / 2,614.3 | 5,474.3 / 5,489.8 | 439.09 / 439.34 |

No-plugin RSS medians were locally **3.55–4.66 MiB lower** than baseline across
these scenes. Startup core readiness was **5,512.7 vs 5,682.5 ms**, while list/chat
readiness and operation durations do not show a uniform advantage. These results
support reduced optional module loading, not a general speedup claim.

Each baseline sample loaded **16 old runtime modules**. Every current state had
zero old runtime, vendor SDK or adapter modules and zero vendor probes/processes;
no-plugin/disabled states executed no vendor entry, and each selected state
executed only its one registration entry. Guarded process creation is denied in
both revisions, so zero successful vendor processes is not evidence of an
unguarded vendor startup. Nonvendor blocked launches/network attempts remain in
the raw diagnostics rather than being concealed.

Each process has temporary HOME/config. State ordering rotates, with no concurrent
project tests/builds, but there is **no OS page-cache flush or machine-load
isolation**; these are local process observations, not guaranteed OS-cold timings.
No account or paid model is used. A separate
[complete default-core, vendor-free run](harness-plugin-final-vendor-free-native.json)
also reaches `native-chat-persisted` with verified history and zero vendor
probes/processes; it is an absence acceptance check, not mixed into this comparison.

The [actual browser comparison](harness-plugin-frontend-comparison.json) uses five
fresh runs per version in the same pinned environment with both SDKs installed:
open a native Agent's edit settings and expand its backend selector without
saving a third-party selection. Every baseline run loaded 17 old/vendor modules,
attempted one vendor probe and issued one vendor status request; all three counts
were zero in every final run. Observed document and static asset response **body**
bytes (identity encoded) had median/max **11,547,780 → 11,529,598**, a reduction of
**18,182 bytes (about 17.8 KiB)**. All five after runs were recollected following
the final catalog/loader fixes and supersede earlier after samples. This excludes
polling/API responses and HTTP headers and is not
a wire-transfer, latency or RAM measurement. A fresh final Console build's 1,746
files matched the [measured asset hashes](harness-plugin-final-frontend-files.json)
byte-for-byte; an audit of all **555 JavaScript files** found none of the recorded
vendor authentication UI markers. See the
[before](harness-plugin-acceptance-assets/frontend-before.jpg) and
[after](harness-plugin-acceptance-assets/frontend-after.jpg) captures.

Earlier [baseline raw records](harness-plugin-baseline-raw/) and
[baseline summary](harness-plugin-footprint-baseline.json) are diagnostic history.
They must not be combined with a different final environment to claim a saving.
The final receipt retains raw samples, medians/maxima, environment, source origins
and fingerprints plus driver/plugin-manifest hashes. Interrupted earlier batches
are diagnostic history, not part of its completed 75 samples. Frozen-platform
runtime and live-account acceptance remain open despite completed source,
packaging, browser and local footprint evidence.

## Pending account and platform handoff

These checks remain **unchecked**. Record OS/architecture/version, Python/runtime
version, artifact SHA-256, command or user action, result and sanitized error for
each run. Do not include tokens, account secrets or private chat contents.

- [ ] **macOS frozen host:** use the exact final bundle at
  `/private/tmp/qwenpaw-harness-final-frozen-20261024/pyinstaller/qwenpaw-backend`
  identified by the [inventory](harness-plugin-final-frozen-inventory.json) and
  [runtime receipt](harness-plugin-final-frozen-runtime.json). From an isolated
  working directory, run the bundle's `qwenpaw --version`, then
  `qwenpaw app --host 127.0.0.1 --port <unused-port>` and verify readiness. Current safe startup
  is NOT_ACCEPTED (-9/no output); no security bypass or root-cause inference is
  part of this handoff. After successful startup, formally install each prepared
  ZIP separately through the real frozen host/helper and verify matching-only SDK,
  executable permissions and adjacent CLI resources.
- [ ] **Windows frozen host:** build and run the minimum artifact on Windows;
  verify safe version/app startup, then each formal plugin installation in an
  isolated matching-SDK environment, CLI resources and disable/uninstall/restart
  behavior. A macOS build or prepared CI job does not satisfy this check.
- [ ] **Linux wheel:** install the final wheel into a clean Linux environment,
  run native startup/chat, then formally install and run each plugin separately
  with only its matching SDK. Preserve platform/version/hash and command results.
- [ ] **Live accounts:** verify Codex OAuth and API-key routes and Qoder browser
  login and PAT routes. For each vendor, run a real text turn, attachment and tool
  approval, cancellation, native-session resume across restart, disable and
  uninstall cleanup. The local protocol evidence above composes the host behavior;
  it does not replace these account/engine checks. Use sanitized outcomes only.
