# MCP optional plugin and SDK 1.1 release validation

Status: **measurement and validation evidence complete within the scope below**.
The broad source run and its focused repairs are recorded separately; there was
no invented single all-green broad run. Final review and plan checkboxes remain
with the coordinating reviewer. See the [design decisions](2026-10-20-mcp-plugin-decisions.md).

The Task 8 artifact hashes and thirty numeric samples below are historical
pre-final-review evidence. A later consolidated fix corrects failed source
replacement, embedded-text delivery and visible recovery diagnostics. Its
affected verification passed 184 Python and 70 Console tests, followed by
42 replacement/lifecycle tests after a final async sampling correction.
Publication of the rebuilt artifacts and current-source unused-runtime checks
is pending. The original measurements and their source hashes are preserved;
they must not be represented as measurements of the revised source.

## Scope and environment

The deliverables are the official `qwenpaw-mcp` plugin (protocol `mcp`) and the
public SDK 1.1 `DriverProtocolSpec` extension. Agent Mail and ACP pluginization
are outside this change. SDK 1.0 compatibility, source identity, credentials,
policy, lifecycle cleanup and disabled-plugin intent remain required contracts.

Validation uses Python 3.13 with `agentscope==2.0.10`, current repository code,
temporary configuration/workspace/credential directories and null keyring.
The external wheel environment has QwenPaw `2.2.3b1`, FastAPI `0.139.0`, pytest
`9.1.1` and uv `0.8.15`. The target project's supported Python range is
`>=3.11,<3.14`; these runs do not establish a complete OS/Python version matrix.

The baseline for the measured performance comparison is source commit
`56be46ddf`; the task implementation starts from reviewed Tasks 1–7 at
`3cb75eecf`. Runtime and test authors are separate. No external account/model
service is used by the local transport acceptance tests.

## Distribution identity and resource scope

| Artifact | SHA256 |
| --- | --- |
| `qwenpaw-2.2.3b1-py3-none-any.whl` | `eb596514a9ba8b9329b70e563be7fbdc0f2b800b168ca60ac6b63d5f82f1376e` |
| `qwenpaw-2.2.3b1.tar.gz` | `67440a6ad4c8d8d5ded67c42a8bd457c479dc0df074328609bac7bd24a45e234` |

The actual build uses `python -m build --no-isolation`; its wheel is built from
the generated sdist. Initial build-tool discovery found no `wheel` distribution,
so cached pure-Python wheel 0.48.0 was copied into a temporary build environment.
No dependencies were downloaded and no global environment was modified.

The wheel was actually installed using `pip install --no-index --no-deps` in a
new external venv. Its dependency mirror reuses third-party packages only:
foreign QwenPaw package directories/metadata, editable finders, `.egg-link`, and
pre-existing `.pth` files are excluded. One generated `.pth` points at that
filtered mirror, never at a foreign checkout. The installed import path and sole
QwenPaw distribution are checked, and every packaged QwenPaw file is compared
against the supplied wheel bytes. A same-version corrupted private venv is
rejected before the author test stages.

The wheel, sdist and frozen resource mapping use the same 13 optional MCP inputs from
`plugins/protocol/mcp`: Python modules, manifest, requirements, and the prebuilt
`frontend/dist/index.js`. The PyInstaller spec derives its mapping from the
existing pyproject package-data declarations. These are inert offline install
resources, outside plugin discovery; no node_modules or author build caches are
included.

The distribution test completed **3 tests in 17.04 seconds**. It checks the exact
frozen mapping, rejects installed wrong bytes, and cold-starts installed-wheel
and frozen-resource-layout subprocesses for both unused and legacy configured
users. **The frozen check reconstructs resources from the evaluated spec and
runs Python subprocesses; it is not a native PyInstaller executable build.**
PyInstaller is unavailable in the current interpreter. Native bundling, signing,
and each target platform's startup still require their release pipeline.

## Executed gates

| Gate | Observed result |
| --- | --- |
| Mandatory complete external SDK artifact check | Exit 0; all seven stages passed |
| Corrected external SDK author suite | 327 passed, 3 warnings; 364.12 s |
| First external SDK author suite | 322 passed, 5 failed, 5 warnings; 344.17 s |
| Fixed transaction cases in source environment | 5 passed; 20.12 s |
| Packaged frontend SDK, NodeNext | Exit 0 |
| Packaged frontend SDK, ESNext/bundler | Exit 0 |
| Packaged frontend SDK runtime capability/scope/stream checks | Exit 0 |
| Documentation fake protocol | Compliance `ok:true`, actual host discovery/invoke/close passed |
| Console plugin and MCP tests | 27 files, 220 tests passed; 11.78 s |
| Console `format:check` | TypeScript build check and Prettier passed |
| Changed Python infrastructure hooks | AST, docstring, encoding, private-key, whitespace, trailing-comma, Black, flake8, mypy and Pylint passed; Pylint 10.00/10 |
| Frozen spec parse and scoped whitespace checks | Passed |
| Initial broad Python source regression | 1974 passed, 6 failed, 7 Windows skips; 375.80 s |
| Corrected affected cold/distribution source tests | 15 passed; 77.88 s (covers five initial failures) |
| Distribution path-portability review correction | 7 passed; 16.65 s, actual artifact manifest and no skips |
| Existing UV dependency-plan case with corrected environment | 1 passed; 0.40 s (sixth initial failure) |
| 1/10/50-workspace idle benchmark | 30/30 cache-matched samples; controller and strict validator exit 0 |
| Official MCP static SDK compliance | `ok:true`, SDK 1.1 compatible, no private-import errors; eight unmanaged-resource warnings retained |

The corrected full artifact script passed wheel identity/byte checks, strict
public-author typing, all 327 author tests, template/frontend exports, template
compliance, and actual template calls/reload/workspace persistence. It used a
new external output directory and the same built wheel.

The first external SDK run exposed a genuine test portability defect: five
transitive dependency transaction cases constructed a checkout-relative `src`
path, so their nested interpreter could not import QwenPaw after wheel install.
The test author replaced that assumption with the currently imported package's
exact origin and matching sole metadata in a narrow temporary import root.
The third-party mirror now follows active absolute dependency roots while
excluding foreign QwenPaw and editable paths. Existing no-mutation, version,
byte-preservation and install-plan assertions are retained. No test is excluded.

Two avoidable unknown `p1` marker warnings in the exported suite were corrected
by declaring the copied tests' marker in the isolated pytest configuration.
Third-party datetime, websockets and JSDOM pseudo-element warnings are retained
in logs rather than suppressed. The initial failed run is diagnostic evidence,
not an all-green acceptance result.

The six broad-source failures were resolved by focused checks without repeating
the 1974 passing tests. Five cold-start cases used a denial function in place
of `subprocess.Popen`, which broke an upstream generic type annotation; the
fixture now subclasses Popen while retaining its zero-child-process assertion.
The sixth case needed the existing uv 0.8.15 executable on the runner PATH and
authorized access to macOS SystemConfiguration outside the filesystem sandbox;
it passed against local dependency artifacts. No production behavior or safety
assertion was weakened. The seven skips are platform-specific Windows cases.

Task 8 review found Windows-specific assumptions in the new distribution tests:
native resource separators were compared with a POSIX prefix, and a copied
venv's interpreter path assumed `bin`. Deterministic PureWindowsPath/PurePosixPath
regressions first produced two failures and two passes. Logical resource keys
now use `as_posix()` consistently; the cloned interpreter retains its supplied
venv-relative layout. All seven affected distribution tests then passed with
the actual artifact manifest, preserving the exact 13 resources, installed-byte
rejection and cold-start checks. This is Windows-style path coverage on macOS,
not a Windows native execution claim. The focused command is the distribution
command below; raw results are `task8-fix1-path-red.log` and
`task8-fix1-distribution-green.log`, and scoped hooks all passed.

## Reproduction commands and retained result index

The following reproduces the recorded isolated source runner without depending
on deleted planning scratch. Run from the repository root with the same local
Python/dependency environment. Paths below identify the actual release artifacts;
substitute existing local paths when reproducing elsewhere. The dependency
mirror for the external wheel must first be prepared using the filtering contract
in [the transaction fixture][transactions]. It excludes foreign QwenPaw/editable
paths and retains the actual installed wheel and matching sole metadata.

```sh
task_python=/Library/Frameworks/Python.framework/Versions/3.13/bin/python3
task_dependencies=/Users/luohh/Library/Python/3.13/lib/python/site-packages
task_uv_bin=/private/tmp/qwenpaw-mcp-checkenv/bin
task_release=/private/tmp/qwenpaw-task8-release
cat > "$task_release/artifacts.json" <<'JSON'
{
  "wheel": "/private/tmp/qwenpaw-task8-release/dist/qwenpaw-2.2.3b1-py3-none-any.whl",
  "sdist": "/private/tmp/qwenpaw-task8-release/dist/qwenpaw-2.2.3b1.tar.gz",
  "python": "/private/tmp/qwenpaw-task8-release/venv/bin/python",
  "frontend_sdk": "/private/tmp/qwenpaw-task8-release/unpacked/qwenpaw/plugins/frontend"
}
JSON
run_source() {
  task_run=$(mktemp -d /private/tmp/qwenpaw-mcp-test.XXXXXX)
  env -i PATH="$(dirname "$task_python"):/usr/bin:/bin:/usr/sbin:/sbin:$task_uv_bin" \
    QWENPAW_WORKING_DIR="$task_run/workspaces" \
    QWENPAW_SECRET_DIR="$task_run/credentials" \
    XDG_CONFIG_HOME="$task_run/config" XDG_CACHE_HOME="$task_run/cache" \
    PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring \
    QWENPAW_TELEMETRY_DISABLED=1 PYTHONDONTWRITEBYTECODE=1 \
    PYTHONNOUSERSITE=1 PYTHONPATH="$PWD/src:$task_dependencies" \
    "$task_python" -B -m pytest -q -p no:cacheprovider \
    --basetemp="$task_run/pytest" "$@"
}
run_source \
  tests/unit/plugins tests/unit/drivers \
  tests/unit/app/test_mcp_plugin_classification.py \
  tests/unit/app/test_lazy_driver_runtime.py \
  tests/unit/app/test_mcp_config_watcher_activation.py \
  tests/unit/app/routers/test_mcp_oauth_flows.py \
  tests/unit/app/routers/test_mcp_oauth_helpers.py \
  tests/unit/app/routers/test_mcp_oauth_discovery.py \
  tests/integration/test_mcp_plugin_upgrade.py \
  tests/integration/test_mcp_plugin_api.py \
  tests/integration/test_mcp_plugin_optional_runtime.py \
  tests/integration/test_mcp_plugin_distribution.py \
  tests/integration/test_driver_mcp_stdio_flow.py \
  tests/integration/test_driver_mcp_http_flow.py \
  tests/integration/test_driver_mcp_oauth_flow.py \
  tests/integration/test_driver_mcp_env_ref_flow.py \
  tests/integration/test_driver_mcp_approval_level_policy.py \
  --mcp-plugin-artifacts "$task_release/artifacts.json"
# Recorded broad run: 1974 passed, 6 failed, 7 skipped, 31 warnings; 375.80s.
# Its original PATH lacked task_uv_bin; the corrected runner includes it.
run_source tests/integration/test_mcp_plugin_optional_runtime.py \
  tests/integration/test_mcp_plugin_distribution.py \
  --mcp-plugin-artifacts "$task_release/artifacts.json"
# Corrected affected cases: 15 passed; 77.88s.
run_source tests/unit/plugins/test_sdk_host_bridge.py::test_sdk_dependency_uv_reinstall_plan_is_rejected_before_mutation
# Corrected local UV case, with authorized macOS access: 1 passed; 0.40s.
run_source tests/unit/plugins/test_sdk_install_transaction.py -k transitive
# Corrected source transaction cases: 5 passed, 9 deselected; 20.12s.
```

The recorded runner lived in planning scratch; the shell function above preserves
its isolation and complete pytest arguments. Local socket/process inventory and
UV's macOS SystemConfiguration access required narrowly authorized runs outside
the sandbox. None of those checks removed assertions or used external accounts.
The seven Windows-only named-pipe skips are in
[the platform-specific tests](../../../tests/unit/plugins/computer_use/test_windows_pipe.py)
(lines 245, 274, 302, 313, 327, 340 and 354 at validation time), with reason
`Windows named pipes only`. A retained assertion-bytecode filename mentioned an
older checkout; the actual imported module origin was independently checked as
the current source tree. No foreign checkout import is used as acceptance.

The exact required artifact/frontend invocation arguments were:

```sh
/private/tmp/qwenpaw-mcp-checkenv/bin/python -m build --no-isolation \
  --outdir /private/tmp/qwenpaw-task8-release/dist
# Actual wheel install into the new external venv:
/private/tmp/qwenpaw-task8-release/venv/bin/python -I -m pip install \
  --no-index --no-deps /private/tmp/qwenpaw-task8-release/dist/qwenpaw-2.2.3b1-py3-none-any.whl
# In the clean environment described below:
/private/tmp/qwenpaw-task8-release/venv/bin/python scripts/check_plugin_sdk_artifacts.py \
  --wheel /private/tmp/qwenpaw-task8-release/dist/qwenpaw-2.2.3b1-py3-none-any.whl \
  --sdist /private/tmp/qwenpaw-task8-release/dist/qwenpaw-2.2.3b1.tar.gz \
  --python /private/tmp/qwenpaw-task8-release/venv/bin/python \
  --source-root "$PWD" --output /private/tmp/qwenpaw-task8-release/sdk-check-final
/private/tmp/qwenpaw-task8-release/venv/bin/python scripts/check_plugin_sdk_frontend.py \
  --package /private/tmp/qwenpaw-task8-release/unpacked/qwenpaw/plugins/frontend \
  --dependencies "$PWD/console/node_modules" \
  --output /private/tmp/qwenpaw-task8-release/frontend-check
npm --prefix console run test:run -- src/plugins src/pages/Agent/MCP
npm --prefix console run format:check
```

Both SDK checks used `env -i` with
`PATH=/Library/Frameworks/Python.framework/Versions/3.13/bin:/usr/bin:/bin:/opt/homebrew/bin`,
`PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring`,
`QWENPAW_WORKING_DIR=/private/tmp/qwenpaw-task8-release/work` and
`QWENPAW_SECRET_DIR=/private/tmp/qwenpaw-task8-release/secrets`.
The artifact script creates fresh per-check XDG/config/secret/work directories.
Outputs must be new directories; use different output names on a rerun.
The frontend package is extracted from the same wheel, with existing declared
node_modules; no source-install substitute was used.

Raw local logs remain under `/private/tmp`: `task8-final-python.log`,
`task8-final-affected-cold.log`, `task8-final-resolver-local.log`,
`task8-sdk-portable-source.log`, `task8-final-console.log`,
`task8-final-format.log`; release logs are under
`qwenpaw-task8-release/{build.log,sdk-check-final.log,frontend-check.log}`.
`sdk-check-final/report.json` records all seven return codes, artifact hashes,
24 copied public author test modules and environment versions; its
`public-author-tests.log` records the 327 passes and three warnings.
These temporary logs are diagnostic originals; the results, commands and linked
test matrix here remain self-contained after planning scratch is removed.

## Measured workspace-runtime overhead

[The durable benchmark JSON](2026-10-20-mcp-plugin-benchmark.json) contains all
30 complete raw sample objects, full precision summaries, original JSON SHA256s,
supporting manifest/median objects, baseline/final source hashes and benchmark
driver hash. Only the absolute source and temporary-cache path prefixes are
normalized; per-sample unique cache directory names, executable/version identity,
RSS arrays, counters and all other values remain unchanged. Publication checked
all original file hashes and equality of every non-path field. The strict
independent validator and sampling controller both exited 0.

Each configuration has five independent 60-second idle samples after a
three-second warmup, for 1/10/50 unused workspaces and baseline/final sources.
The final run uses fresh private `PYTHONPYCACHEPREFIX` directories plus `-B`
and `PYTHONDONTWRITEBYTECODE=1` for both trees; every recorded prefix is distinct
and nonexistent. It therefore does not read old bytecode from either tree.
All children use the same Python 3.13.10, executable and AgentScope 2.0.10.

This is actual Workspace startup/shutdown with production driver_manager and
baseline driver_config_watcher descriptors. Model, channel and unrelated network
services are excluded. Startup includes cold source imports, fixture/config
setup and descriptor startup; interpreter launch and the earlier psutil import
are outside its timer. CPU is child user+system CPU time during the sampled idle
interval. RSS is sampled once a second; the table takes the median per child,
then the median across five children. Other columns are also medians of five
samples. Exact actual sample durations (all at least 60 seconds) are in JSON.

| Workspaces | Source | CPU seconds / approximately 60 s | RSS MiB | Startup seconds | Idle file scans / snapshots | Manager constructions | Related tasks after warmup |
| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | baseline | 0.022520576 | 248.656250 | 3.005088 | 30 / 30 | 1 | 1 |
| 1 | final | 0.011177760 | 248.734375 | 3.034867 | 0 / 0 | 0 | 0 |
| 10 | baseline | 0.072085024 | 248.921875 | 3.044088 | 300 / 300 | 10 | 10 |
| 10 | final | 0.011088640 | 248.687500 | 3.041551 | 0 / 0 | 0 | 0 |
| 50 | baseline | 0.277328128 | 249.593750 | 3.100720 | 1500 / 1500 | 50 | 50 |
| 50 | final | 0.011157312 | 249.125000 | 3.085753 | 0 / 0 | 0 | 0 |

In every final sample, real Manager constructions, idle snapshots/file scans,
related tasks and project MCP handler/transport imports were zero. All 30
shutdowns left zero owned tasks. Baseline project MCP imports numbered three;
upstream `mcp.*` imports numbered 83 in **both** sources and are listed separately.
`counts_after_start` means **after descriptor startup plus the three-second
warmup**, not immediate startup. `idle_counts` is the sampled interval's delta.
`file_scans` counts direct `list_card_paths` enumeration calls, not OS syscalls.

The deterministic finding is removal of the unused MCP manager/watcher work.
CPU time was lower within this measured descriptor scope. RSS and startup
changes were small and did not improve uniformly: the one-workspace final RSS
and startup medians were slightly higher. These developer-host samples do not
establish material whole-application memory/startup improvements, confidence
intervals or statistical significance. Other author/test processes shared the
machine; alternating pair order reduces order bias but does not isolate host load.

Two earlier datasets are wholly excluded. The first under
`qwenpaw-task8-benchmark` missed direct snapshot enumeration in its counter.
The second under `qwenpaw-task8-benchmark-final` had 950 existing final-source
pyc files versus zero baseline pyc files; `-B` alone does not prevent reading
cache. Its 14 samples were discarded for **all** metrics. No medians or subsets
from either run are mixed into the final cache-matched results.

The recorded benchmark command was:

```sh
/Library/Frameworks/Python.framework/Versions/3.13/bin/python3 \
  scripts/benchmark_mcp_optional_runtime.py \
  --baseline /private/tmp/qwenpaw-mcp-baseline --final "$PWD" \
  --dependencies /Users/luohh/Library/Python/3.13/lib/python/site-packages \
  --manifest .superpowers/sdd/2026-10-20-mcp-plugin/task-8-baseline-manifest.json \
  --output /private/tmp/qwenpaw-task8-benchmark-cache-matched --seconds 60 --warmup 3
```

For reproduction after scratch cleanup, extract
`supporting_artifacts["baseline-manifest.json"]` from the durable JSON to a local
manifest file and pass its path to `--manifest`; obtain the recorded baseline
commit in an isolated source directory including its own qwenpawmail package.
The retained benchmark driver has SHA256
`f9346556ca66cd83ec514fa5accfc672f95ac6a9943386439fefd1c62d05b6ec`.
Defaults are `--counts 1 10 50 --replicates 5`. Original controller/validator logs
are `task8-benchmark-cache-matched-run.log` and `task8-benchmark-validated.log`.

## Isolation incident

One early package metadata inspection omitted the temporary environment roots.
QwenPaw bootstrap attempted the real-user restore lock; the filesystem sandbox
rejected the lock open. A source audit confirms that this failure occurred
before the lock context yielded, before persisted environment loading or
restore cleanup. No real-user configuration changed. The recorded warning was `Operation not permitted` when opening
`~/.qwenpaw/.qwenpaw_restore.lock`; the command was not repeated, and
no user files were inspected for the audit. Subsequent runtime checks use clean
environments, explicit temporary roots and null keyring.

## Acceptance matrix and executable evidence

The linked tests below are durable repository evidence, covered by the broad
source command and its focused repairs, or the recorded Console/external SDK
runs. Cold fixtures retain `fixture-before.json`, actual source/credential files,
post-upgrade state and `observations.json` under their temporary roots. Controlled
credential values are compared without printing secrets. The legacy real RPC
uses the supported request approval override solely to exercise migration;
separate policy cases establish allow/deny/ask behavior.

| Required behavior | Executable evidence |
| --- | --- |
| Legacy `mcp.clients` converted once; original tools callable | [Cold runtime] real stdio legacy conversion; [Upgrade] actual SDK bridge |
| Existing DriverCard/watermark retained; deleted sources not revived | [Cold runtime] second-process deleted-source restart; [Migration version] |
| Mixed stdio/SSE/HTTP and same names across workspaces | [Cold runtime] real-peer PID/source identity and mixed transports |
| command/args/cwd/env/url/headers/timeout/display/enabled preserved | [Legacy upgrade], [Legacy env refs], [Upgrade], [Cold runtime] byte/field comparisons |
| Credentials/OAuth/policy/whitelist preserved; `tools=[]` denies all | [Runtime credentials], [Public MCP driver], [Driver lifecycle], [Cold runtime] |
| Enabled discovery/invocation/reconnection works; disabled never connects | [Cold runtime] actual stdio/SSE/HTTP peer termination/restart and disabled siblings |
| Custom all-disabled sources remain manageable without re-entry | [Upgrade], [Lazy runtime], [Cold runtime] authenticated management/OAuth with zero managers |
| Two-second scan retained; manually enabled sources applied next round | [Watcher activation] interval, manual-edit and next-scan tests |
| Explicit plugin disable/uninstall survives restart and preserves data | [Cold runtime] late-install lifecycle and [Upgrade] intent/interruption cases |
| Interrupted upgrade has no half-install/duplicate registration/leak | [Upgrade] copy/settings/marker/cancellation/concurrency/receipt cases; deterministic interruption, not literal power loss |
| Disable/re-enable/update/failure recovery retains usable configuration | [Cold runtime] real lifecycle; [Transactions] failed-update rollback and [Upgrade] |
| No-MCP, untouched disabled AnySearch and default migrated origins stay unused | [Classifier], [Upgrade], [Cold runtime] none/v0/v1/v2, each two cold starts |
| No installation directory, discovery/load entry, installer or DependencyGate | [Cold runtime] denial guards and observations; [Distribution] wheel/frozen-layout unused starts |
| No pip/uv, OAuth/MCP connection or project MCP transport load when unused | [Cold runtime] denied installer/socket/Popen paths and project-import observations |
| Zero real managers, MCP processes/connections/tasks/snapshots when unused | [Cold runtime] actual constructor/IO/task/process checks; [benchmark data] actual service descriptors |
| Second restart does not interpret generated defaults as usage | [Cold runtime] independent child restarts; [Upgrade] repeated-unused cases |
| Later explicit install works in same Workspace without rebuilding core | [Cold runtime] same-facade late-install real RPC and full plugin lifecycle |
| Original `/mcp` entry, route and initial page remain; visiting starts nothing | [MCP UI lifecycle], [Plugin API], [Cold runtime] AuthMiddleware/no-constructor management |
| Uninstalled/disabled/failed states support install/enable/retry in place | [MCP UI lifecycle], [MCP UI hook], [Plugin API] |
| Disable/uninstall restores initial UI; absent bundle not loaded | [MCP UI lifecycle], [Console plugins] discovery/host bridge |
| Other backend discovery remains available | [Console plugins] existing discovery/registry compatibility cases |
| SDK 1.0 plugins load; unsupported drivers rejected before entry import | [Driver contract], [Cold runtime] explicit SDK1.0 marker rejection |
| Non-MCP protocol works through public DriverProtocolSpec alone | [Driver lifecycle] fake protocol; independently executed bilingual documentation example |
| Workspace/source/session identity and credentials cannot cross scope | [Driver lifecycle], [Runtime credentials], [Cold runtime] OAuth mismatch and authenticated workspace selection |
| Existing allow/deny/ask policy remains enforced | [Driver lifecycle], [Public MCP driver], [Approval policy] |
| Interrupted initialization, canceled RPC, replacement, close timeout retain custody | [Driver lifecycle], [Public MCP driver], [Transport stateful], [Transport ownership], [Lazy runtime] |
| Public SDK imports only; test host uses production adapter/governance | External SDK 327-case run and strict author fixture; static compliance below |
| No scan for missing/disabled MCP; temporary source disable still monitored | [Watcher activation], [Lazy runtime], [Cold runtime], [benchmark data] |
| Offline resource parity, old-user install, no unconditional desktop seed | [Distribution] exact spec mapping and actual wheel/frozen-layout cold starts; archive hashes above |

[Cold runtime]: ../../../tests/integration/test_mcp_plugin_optional_runtime.py
[Upgrade]: ../../../tests/integration/test_mcp_plugin_upgrade.py
[Migration version]: ../../../tests/unit/drivers/adapters/test_mcp_migration_version.py
[Legacy upgrade]: ../../../tests/unit/drivers/adapters/test_mcp_legacy_upgrade.py
[Legacy env refs]: ../../../tests/unit/drivers/adapters/test_mcp_legacy_env_ref.py
[Runtime credentials]: ../../../tests/unit/plugins/mcp/test_runtime_credentials.py
[Public MCP driver]: ../../../tests/unit/plugins/mcp/test_public_driver.py
[Driver lifecycle]: ../../../tests/unit/plugins/test_sdk_driver_lifecycle.py
[Lazy runtime]: ../../../tests/unit/app/test_lazy_driver_runtime.py
[Watcher activation]: ../../../tests/unit/app/test_mcp_config_watcher_activation.py
[Transactions]: ../../../tests/unit/plugins/test_sdk_install_transaction.py
[Classifier]: ../../../tests/unit/app/test_mcp_plugin_classification.py
[Distribution]: ../../../tests/integration/test_mcp_plugin_distribution.py
[MCP UI lifecycle]: ../../../console/src/pages/Agent/MCP/pluginLifecycle.test.tsx
[MCP UI hook]: ../../../console/src/pages/Agent/MCP/useMCPPlugin.test.ts
[Plugin API]: ../../../tests/integration/test_mcp_plugin_api.py
[Console plugins]: ../../../console/src/plugins
[Driver contract]: ../../../tests/unit/plugins/test_sdk_driver_contract.py
[Approval policy]: ../../../tests/integration/test_driver_mcp_approval_level_policy.py
[Transport stateful]: ../../../tests/unit/plugins/mcp/test_transport_stateful.py
[Transport ownership]: ../../../tests/unit/plugins/mcp/test_transport_ownership.py
[benchmark data]: 2026-10-20-mcp-plugin-benchmark.json

## Static compliance warnings and resource custody

The official MCP static check `python -m qwenpaw.plugins.check plugins/protocol/mcp`
was run with current-source PYTHONPATH and isolated temporary configuration.
Its recorded result is `ok:true`, SDK/declared version `1.1.0`, compatible,
`runtime_checked:false`, no private-import errors, and **eight**
`unmanaged-resource` warnings for `asyncio.create_task` in
[the stateful client](../../../plugins/protocol/mcp/mcp_stateful_client.py).
The recorded warning locations are lines 269, 408, 688, 692, 693, 788 and 850
(two findings at 850). The static record's plugin hash is
`36b0cb51b8d83698842d8f7cb087d8c3c1b0a3b0bdccb39b2d9a5522ea31f3bb`,
checked at `2026-10-20T11:32:05.674806+00:00` with rules `1.0.0`.
Dynamic `getattr` paths and unmanaged-resource ownership remain explicitly
uncovered by static analysis; this is not a warning-free static pass or a full
permission audit (`permission_review:not-run`). No checker exemption was added.

The ownership argument is concrete and separately tested. Lifecycle tasks stay
referenced until their bounded join succeeds or the retained lifecycle-reaper
ledger completes them. Request watchers are canceled and awaited in the caller's
`finally`; drain tasks are shielded/joined during same-task stack cleanup. An RPC
that resists cancellation stays in the active or retained `_rpc_cleanup_tasks`
set, with result consumption and removal only after completion. Lifecycle finish
or idempotent close cannot erase that outstanding ownership. The real-host
regression in [Public MCP driver] proves prompt caller cancellation, failed
strict close and nonquiescent unload while a task still runs, then successful
cleanup only after the task is released and awaited. [Transport stateful] and
[Transport ownership] cover reapers and failed close retry. This guarantees a
bounded cleanup attempt plus retained ownership, not forced termination of a
Python coroutine that suppresses cancellation. The warnings remain available
for final review triage.

The broad source run's 31 warnings include FastAPI/Starlette TestClient notices
and deliberate legacy shutdown/uninstall-hook deprecations in compatibility
tests. The final wheel suite retains three upstream lark datetime and
websockets/aibot deprecations; Console retains JSDOM pseudo-element warnings.
No blanket warning suppression or speculative dependency upgrade was used.
Physical power loss, native desktop binaries/signing, Windows named pipes and
every supported Python minor version were not exercised on this macOS host.
