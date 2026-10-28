# MCP optional plugin and SDK 1.1 release validation

**2026-10-21 performance follow-up:** the expanded
[MCP performance investigation](2026-10-21-mcp-performance-diagnosis.md)
identified a configured-source sequential-startup regression and additional SDK
schema-validation cost. The functional checks below remain historical evidence;
they do not establish unchanged performance for configured MCP services.

Status: **measurement and validation evidence complete within the scope below**.
The broad source run and its focused repairs are recorded separately; there was
no invented single all-green broad run. All code-review findings are resolved
and the plan checklist is complete. Work remains on the local `codex/mcp-plugin`
branch, without push or merge. See the [design decisions](2026-10-20-mcp-plugin-decisions.md).

The Task 8 artifact hashes and thirty numeric samples below are historical
pre-final-review evidence. A later consolidated fix corrects failed source
replacement, embedded-text delivery and visible recovery diagnostics. Its
affected verification passed 184 Python and 70 Console tests, followed by
42 replacement/lifecycle tests after a final async sampling correction.
The remaining live-retry gap was reproduced independently for enabled and
disabled sources, then passed both focused cases and 61 affected tests;
the [final amendment](2026-10-20-mcp-plugin-rereview-i3.md) records the exact
commands, ownership checks and original failed review.
The final reviewed source `773f1fc5b` now has rebuilt wheel/sdist acceptance,
349 external SDK author tests, complete frontend SDK checks and a short
current-source unused-runtime counter check, recorded separately below. The
original measurements and source hashes remain historical and must not be
represented as performance measurements of the revised source.

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

## Final reviewed-source release: `773f1fc5b`

The final source identity is `773f1fc5bb257c1416c832d861ac8c7032fcb38c`.
The [initial review](2026-10-20-mcp-plugin-review.md),
[scoped rereview](2026-10-20-mcp-plugin-rereview.md) and
[residual retry-ordering rereview](2026-10-20-mcp-plugin-rereview-i3.md)
record the findings and their resolution. The chronological
[decisions](2026-10-20-mcp-plugin-decisions.md) distinguish the affected source
regressions from earlier broad runs. The initial review covers `d8d7aa2e5`,
the first scoped rereview covers `029011ac9` and retains its failed retry
finding; the final narrow rereview covers `773f1fc5b` and closes that finding.

| Current artifact | SHA256 |
| --- | --- |
| `qwenpaw-2.2.3b1-py3-none-any.whl` | `de09674b767dc321f82a2dc4af9ceabe14922a920fd26d4825d3c6e6b4ec223b` |
| `qwenpaw-2.2.3b1.tar.gz` | `8758bad2a5e7165e2fe4f75e5c5efd0b6cd9e30cee513de44ae24d52bf4881b0` |

These artifacts were built from a fresh Git archive of the final commit, with
release-equivalent Console and website-doc staging inside that private snapshot.
The earlier Task 8 wheel contained no full Console output; this release closes
that packaging gap. Both archives contain all **1,745 Console files, 84 website
Markdown documents and 13 optional MCP resources**, with bytes verified against
the staged snapshot. The wheel has 3,402 members and sdist 3,693 members.
The installed package has sole QwenPaw metadata and all `qwenpaw/` member bytes
match the wheel. Independent acceptance also checks the final retry coordinator
bytes in both archives against the reviewed source.

Console inputs are identical between `029011ac9` and `773f1fc5b`
(`git diff --exit-code 029011ac9 773f1fc5b -- console`, exit 0). The full
production build was executed once in the former private snapshot using the
existing declared `node_modules`, then reused. TypeScript and Vite passed;
19,703 modules were transformed, Monaco CSS verified, 495 assets precompressed,
and the initial-bundle check passed: 9.85 MiB raw / 2.55 MiB Brotli across 11
initial assets. Vite retained mixed static/dynamic import and large-chunk
warnings. This is build and packaged-byte evidence, not an actual browser test.

| Final release gate | Actual result |
| --- | --- |
| Full Console production build | Exit 0; Vite build 34.21 s; compression and initial-bundle checks passed |
| Wheel and sdist build, actual external wheel install | Both exit 0; no source install or network dependency download |
| Existing distribution acceptance | 7 passed, no skips, 17.89 s |
| Complete installed-wheel SDK script | All seven stages exit 0; 349 tests, 3 warnings, 367.84 s |
| Packaged frontend SDK | NodeNext, bundler and runtime each exit 0 |
| Current unused-runtime counters | Three current-only cells, N=1/10/50, zero measured MCP work and clean stops |

The complete SDK gate copied 25 public author test modules, including the new
`test_sdk_driver_replacement.py`, into an external `author_tests` package. All
seven stages passed: installed import/version/byte binding, strict author
mypy, exported author tests, template export, frontend export, template
compliance and actual template load/call/reload/cross-workspace runtime. The
349-case suite retains three upstream lark datetime, aibot websockets and
websockets legacy deprecation warnings. Template compliance is SDK 1.1 with
its declared SDK 1.0 template compatible; permission review remains not run,
and its dynamic/uncalled/native behavior limitation remains visible. No broad
source or Console tests were duplicated for this artifact refresh.

### Final release commands and environment

The following is the recorded command sequence. `REPO`, `RELEASE`, `BUILD_PY`
and `PY` are task-specific path variables; the two Python environments use
Python 3.13.10. `BUILD_PY` is the cached temporary build environment, while `PY`
is a newly created venv with no system site packages. Its dependency-only mirror
excludes all foreign QwenPaw package/metadata, editable entries and inherited
`.pth`/`.egg-link` files; the single generated `.pth` points to that filtered
mirror. Only the newly built wheel supplies QwenPaw. No global environment,
real-user configuration or live source Console/docs output was changed.

```sh
REPO=/Users/luohh/Documents/Qwenpaw/QwenPaw-main_harness
RELEASE=/private/tmp/qwenpaw-final-release
BUILD_PY=/private/tmp/qwenpaw-mcp-checkenv/bin/python
PY="$RELEASE/venv/bin/python"
clean() {
  env -i PATH=/Library/Frameworks/Python.framework/Versions/3.13/bin:/usr/bin:/bin:/opt/homebrew/bin \
    QWENPAW_WORKING_DIR="$RELEASE/bootstrap-work" \
    QWENPAW_SECRET_DIR="$RELEASE/bootstrap-secrets" \
    XDG_CONFIG_HOME="$RELEASE/bootstrap-config" \
    XDG_CACHE_HOME="$RELEASE/bootstrap-cache" \
    PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring PYTHONNOUSERSITE=1 \
    PIP_NO_INDEX=1 PIP_CACHE_DIR="$RELEASE/pip-cache" UV_NO_INDEX=1 \
    npm_config_cache="$RELEASE/npm-cache" npm_config_userconfig="$RELEASE/npmrc" "$@"
}
cd "$REPO"
clean git archive --format=tar --output="$RELEASE/source-029011ac9.tar" 029011ac9
mkdir "$RELEASE/source-029011ac9"
tar -xf "$RELEASE/source-029011ac9.tar" -C "$RELEASE/source-029011ac9"
ln -s "$REPO/console/node_modules" "$RELEASE/source-029011ac9/console/node_modules"
clean npm --prefix "$RELEASE/source-029011ac9/console" run build
clean git diff --exit-code 029011ac9 773f1fc5b -- console
clean git archive --format=tar --output="$RELEASE/source-773f1fc5b.tar" 773f1fc5b
mkdir "$RELEASE/source-773f1fc5b"
tar -xf "$RELEASE/source-773f1fc5b.tar" -C "$RELEASE/source-773f1fc5b"
```

Staging uses `shutil.copytree(..., dirs_exist_ok=True)` from the completed
Console `dist` to the final snapshot's `src/qwenpaw/console`; each
`website/public/docs/*.md` is copied with `shutil.copy2` into its
`src/qwenpaw/docs`. This is the data staging in
[the standard release script](../../../scripts/wheel_build.sh); its npm-install,
global-tool-install and cleanup wrapper was not run. All destinations belong
to the new isolated snapshot.

```sh
cd "$RELEASE/source-773f1fc5b"
clean "$BUILD_PY" -m build --no-isolation --outdir "$RELEASE/dist"
WHEEL="$RELEASE/dist/qwenpaw-2.2.3b1-py3-none-any.whl"
SDIST="$RELEASE/dist/qwenpaw-2.2.3b1.tar.gz"
clean "$PY" -I -m pip install --no-index --no-deps "$WHEEL"
clean "$PY" -I -m zipfile -e "$WHEEL" "$RELEASE/unpacked"
cd "$REPO"
clean "$PY" scripts/check_plugin_sdk_artifacts.py \
  --wheel "$WHEEL" --sdist "$SDIST" --python "$PY" \
  --source-root "$REPO" --output "$RELEASE/sdk-check"
clean "$PY" scripts/check_plugin_sdk_frontend.py \
  --package "$RELEASE/unpacked/qwenpaw/plugins/frontend" \
  --dependencies "$REPO/console/node_modules" --output "$RELEASE/frontend-check"
```

All outputs were new directories. The package build creates the wheel from its
new sdist. The final source archive SHA256 is
`222f7fd37c10ff58412f1ea4018381cc2ab467ecccdc5f8e2239018d051a1eef`.
The install succeeded with a pip warning that the default macOS user-cache
location was inaccessible and caching disabled; subsequent commands explicitly
set the private `PIP_CACHE_DIR` shown above. No cache permission was widened.
The earlier isolation incident remains documented below.

Raw records are in `/private/tmp/qwenpaw-final-release`: `console-build.log`,
`console-input-parity.log`, `build.log`, `install.log`, `final-source.json`,
`final-artifacts.json`, `sdk-check.log`, `sdk-check/report.json` and its seven
stage logs, plus `frontend-check/results.json`. Independent distribution and
archive-identity records are `/private/tmp/final-fix-773-distribution.log` and
`/private/tmp/final-fix-773-artifact-identity.log`. The artifact manifest has
absolute `wheel`, `sdist`, `python`, `frontend_sdk` fields, the pinned source
commit and the hashes above; it can be reconstructed without scratch metadata.

The exact independent distribution invocation was:

```sh
.superpowers/sdd/2026-10-20-mcp-plugin/run-tests -c pyproject.toml \
  tests/integration/test_mcp_plugin_distribution.py \
  --mcp-plugin-artifacts /private/tmp/qwenpaw-final-release/final-artifacts.json
```

The scratch-independent `run_source` equivalent below preserves this runner's
environment; pass the same test path and new manifest to it. The seven cases
cover resource allowlist equality, actual installed-wheel wrong-byte rejection,
wheel and frozen-layout unused/legacy cold children, and logical POSIX/Windows
resource/interpreter paths. The local subprocess-inventory checks used the
previously authorized narrow sandbox escalation. This is macOS execution and
deterministic Windows-style path coverage, not native Windows execution.

Final raw-output identities (SHA256):

| Record | SHA256 |
| --- | --- |
| Console build (`console-build.log`) | `61c234a4de3941c18802550e540862c1211df7b72b31824c7fbced908ae5ac3f` |
| Package build (`build.log`) | `f13922fe389cf6e4f68e4b297c05ff868f9b368d07833350283a44d7213b5df4` |
| Actual wheel install (`install.log`) | `4bbe9198b7e45d53c162fdfb300daeb2a95ade6b00a505621d689d5106cf0b6e` |
| Full SDK report (`sdk-check/report.json`) | `1e53adfe5a9f1052568de3ad50c20af73daeb30e1d56f06962548938ca88c7c5` |
| SDK author test output (`sdk-check/public-author-tests.log`) | `51dc704698457ff0ba390f63d9032399373ba87d21ded536d2dcc710af358960` |
| Frontend SDK results (`frontend-check/results.json`) | `d67565b7965b1c6a6738fa2138ba7345c31726bd271bee68f44da117fa781728` |

### Current unused-runtime scope

The [current counter evidence](2026-10-20-mcp-plugin-current-counters.json)
retains all three raw rows, the complete handoff, exact commands/environment,
source and driver hashes and original raw-file hashes. Only machine repository,
private output and third-party-directory prefixes are replaced with documented
placeholders. The original handoff SHA256 is
`1d4d5c9324a463c5196b3672470bd221047a7dc2b5057d24f9b9c956592d277b`.

At N=1, 10 and 50, current source has zero real managers, snapshots, card-file
enumerations, dedicated tasks and project MCP imports, both after startup plus
warmup and during the idle window; every stop leaves zero owned tasks. Each
cell uses a fresh private bytecode-cache prefix with bytecode writes disabled,
Python 3.13.10 and AgentScope 2.0.10. These are one replicate per count with
requested 3.2-second observation and 0.1-second warmup, using actual Workspace
Driver service descriptors. The retained incidental timings and RSS values
are not performance estimates. There is no new CPU/RSS/startup comparison for
`773f1fc5b`. The earlier six-cell `029011ac9` smoke and the original thirty
numeric samples keep their historical source identities.

Credential CAS evidence uses the same-process shared lock; independent
multiprocess writers were not probed. The SDK deadline is optional and remains
`None` in current production. Neither boundary was expanded during this change.
Frozen acceptance remains an evaluated resource layout plus cold subprocesses,
not a compiled native executable. OS/Python, permission-review and physical
power-loss limits below still apply.

## Historical Task 8 distribution identity and resource scope

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

## Historical Task 8 executed gates

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

## Historical reproduction commands and retained result index

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

## Historical measured workspace-runtime overhead

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
| Public SDK imports only; test host uses production adapter/governance | Final external SDK 349-case run and strict author fixture; historical 327-case run retained; static compliance below |
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
