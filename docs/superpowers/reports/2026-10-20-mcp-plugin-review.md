# Final whole-branch review — MCP optional plugin and SDK 1.1

Reviewed base `56be46ddf7672aa042e06b5c25f8cf2194e9fb93` through exact HEAD `d8d7aa2e5172ed33a172e964e903bb9692b23328`. Used the supplied once-generated diff and current source, in production integration passes. No source, repository test, dependency, index, HEAD or branch changes; no additional reviewers or subagents. The only writes were this requested report and the focused probe in `/private/tmp/task_mcp_final_review_probe.py`.

## Spec compliance, code quality and architecture

**Spec compliance: needs fixes.** The optional official plugin, public SDK 1.1, governed generic bridge, lazy workspace service, conditional existing two-second watcher, offline provisioning and retained `/mcp` page are implemented. Agent Mail and ACP remain compatibility consumers. The cross-task boundaries still violate the required usable-old-instance behavior after failed source preparation (I1), existing tool-result fidelity at the actual agent consumer (I2), and actionable recovery diagnostics (I3).

**Code quality: generally sound, with three concrete integration defects.** Ownership is explicit, public data is deeply snapshotted, settings intent is separated from installed files, and tests cover many adversarial lifecycle sequences. The main remaining weakness is where a local guarantee is mistaken for an end-to-end guarantee: retaining the old handler object does not keep it callable; retaining text in a DTO does not deliver it to the agent; returning recovery diagnostics does not show them to the user.

**Architecture: appropriate for the requested scope.** The plugin depends on public SDK imports, the host owns authorization/identity/lifecycle, and protocol implementations remain outside default discovery. The lightweight service owns monitoring separately from active connections, matching the explicit polling requirement. Changes reuse installation/lifecycle facilities rather than creating another installer. MCP-named whitelist helpers in the host operate on generic card/tool data and did not establish a second protocol restriction. Some new helpers remain loosely typed and the bridge is large, but neither fact independently justifies an additional blocking rewrite.

## Strengths

- `src/qwenpaw/plugins/_drivers.py:447` binds exact workspace instances, generations, source revisions and session scopes; `:546` registers connection cleanup before awaiting open. Pending and failed cleanup ownership stays visible to the manager and plugin lifecycle rather than disappearing from published-handler maps.
- `src/qwenpaw/app/driver_runtime_service.py:271` establishes monitoring before activating cards; `:314` changes watcher lifetime by protocol availability and retained monitoring intent. The same two-second snapshot mechanism remains. Empty queries do not construct a real Manager.
- `src/qwenpaw/app/mcp_plugin_upgrade.py:89` inventories raw configured paths, including inactive workspaces. Default fingerprints, explicit-off precedence, check-only dependencies, staged origin records and explicit install receipts protect existing data and user intent. SDK drivers participate in generic startup ordering.
- `src/qwenpaw/app/mcp/oauth_bridge.py:173` consumes callback grants before awaiting, then checks generation, workspace instance and source revision; the plugin also checks its single-use PKCE session and credential CAS. Passive configuration reads use metadata paths.
- `plugins/protocol/mcp/frontend/index.tsx:12` owns one removable wrapper, aborts its requests on scope changes/unmount and keeps the original page. Core list mutations suppress stale workspace responses. Initial page reads do not install the plugin.
- Validation is substantive and honestly bounded: actual installed-wheel SDK author tests, real local transports and OAuth, independent cold processes, and cache-matched measurements. The final performance evidence supports removing unused Driver work; it does not claim a uniform whole-application memory/startup improvement.

## Issues

### Critical

None found.

### Important

#### I1 — Keep the last committed source callable when candidate preparation fails

**Locations:** `src/qwenpaw/drivers/manager.py:203`, `:237`, `:351`; `src/qwenpaw/plugins/_drivers.py:458` and `:474`.

`register_driver()` and `reload_driver()` call `_reserve()` before the candidate opens; transient replacement does the same for every candidate name. That immediately advances the revision checked by the old SDK handler. If candidate open raises, the old handler remains in `_handlers`, but every subsequent discovery/invocation rejects it as stale. Persistent replacement also saves the new card first, so simply restoring the revision is insufficient: `SDKDriverHandler.check()` rejects the old handler against the failed on-disk candidate. `list_drivers()` can still report the retained entry as active.

**Concrete scenario:** an existing working source is edited to a temporarily unreachable endpoint, or a same-name transient source is replaced and the replacement fails initialization. The old connection remains owned and open but is unusable. This contradicts the required prepare-before-switch/failure-retains-old-instance behavior and the transient method's atomic replacement contract. A request already using the old source can also be rejected while the candidate is still preparing.

**Fix:** separate candidate reservation/supersession from the currently committed active revision. Commit the active owner only after successful preparation and final freshness checks. Preserve a coherent active source snapshot on failure without overwriting newer external edits; do not merely suppress stale guards or blindly restore disk state. Keep disable/delete, policy restriction, credential revocation, workspace replacement and superseded-candidate checks effective. Cover API save/reload and manual-edit refresh, plus same-name transient replacement, using actual public SDK calls before/during/after failure. Existing generic transient failure coverage changes names (`old` to `new`/`broken`) and does not exercise this identity collision.

**Focused evidence:** actual public-only fake protocol, real `PluginTestHost` and production manager/adapter/governance; both initial calls succeeded, candidate open raised the expected synthetic preparation error, and both subsequent calls failed. Exact command:

```sh
.superpowers/sdd/2026-10-20-mcp-plugin/run-tests -c pyproject.toml -p tests.conftest /private/tmp/task_mcp_final_review_probe.py
```

At this invocation the file contained only the two parametrized replacement cases. Relevant actual output:

```text
FAILED ::test_failed_preparation_keeps_previous_source_callable[]
FAILED ::test_failed_preparation_keeps_previous_source_callable[session-a]
qwenpaw.plugins.errors.SDKError: Driver source revision is no longer active
2 failed in 1.03s
```

Both failures occur at the final `host.call_driver(...).structured_content['label'] == 'old'` assertion, after the successful initial call and the expected failed candidate open. Temporary test root: `/private/tmp/qwenpaw-mcp-test.EHw8D3/pytest`. An earlier probe setup incorrectly chose `plugin.py` instead of the existing fixture's `main.py`; that initial run produced two setup assertion failures in 0.42s and is not production evidence. It was corrected before the run above.

#### I2 — Preserve embedded resource text through the AgentScope boundary

**Locations:** `plugins/protocol/mcp/results.py:38`; `src/qwenpaw/drivers/adapters/agentscope_tool.py:113`. Relevant unchanged consumer behavior: `src/qwenpaw/plugins/_messages.py:223` converts file attachments to a `DataBlock` without using `ContentBlock.text`.

An MCP `resource` containing `TextResourceContents` becomes a public `ContentBlock(type='file', text=body, attachment=uri)`. The new Driver result path then calls `_to_block()` and emits only a URL-backed DataBlock. The actual document body is absent from the agent tool result. The pre-pluginization MCP result path at `agentscope_tool.py:73` emits embedded resource text directly. A server's `file://` or custom resource URI often cannot be fetched by the host/model, so retaining that URI is not equivalent to retaining the supplied document.

**Fix:** preserve embedded text as text in the final ToolChunk, while keeping attachment/resource metadata where appropriate. This can be done in the official plugin result mapping or the generic consumer, with a clearly defined public ContentBlock contract; avoid interpreting private MCP SDK objects in the host. Extend the existing all-content-types regression through the actual AgentScope consumer, not only the public DTO. Check text, image/audio, embedded text/blob, resource link, structured content and error status at that boundary. Embedded **blob** payloads are already converted to inline base64 attachments and `_to_block()` retains those bytes; I am not claiming an additional blob-loss defect.

**Focused evidence:** official plugin + public host + existing synthetic MCP peer, then production `_tool_chunk_from_driver_result` using actual AgentScope 2.0.10:

```sh
.superpowers/sdd/2026-10-20-mcp-plugin/run-tests -c pyproject.toml -p tests.conftest /private/tmp/task_mcp_final_review_probe.py -k embedded
```

The public DTO assertion for `important embedded document body` passed. Actual failure:

```text
AssertionError: [{'type': 'data', 'id': '0c935e0f942e4bd4811a3d93bd8871c7', 'source': {'type': 'url', 'url': 'file:///synthetic.txt', 'media_type': 'text/plain'}, 'name': None, ...}]
FAILED ::test_embedded_resource_text_reaches_agent_tool_chunk
1 failed, 2 deselected in 0.87s
```

Temporary test root: `/private/tmp/qwenpaw-mcp-test.xlelUQ/pytest`. Existing `test_all_content_types_and_structured_result_cross_public_boundary` proves DTO fidelity but stops before this lossy conversion.

#### I3 — Surface upgrade failures and needs-review diagnostics in the retained page

**Locations:** `src/qwenpaw/app/_app.py:538`; `src/qwenpaw/app/mcp/plugin_service.py:66`; `console/src/pages/Agent/MCP/index.tsx:312` (initial status alert).

Production startup discards the return value of `prepare_startup_plugins()`. The coordinator does not retain that result for the status endpoint. `plugin_state()` reads diagnostics only from a loaded PluginRecord, which does not exist for early failures such as invalid migration metadata or a dependency/copy check that fails before load. The page declares a diagnostics array but does not render it. Therefore an existing configured user's safe `needs_review` outcome becomes an unexplained `not_installed`/`inactive` page; a healthy sibling can make the global plugin active while another workspace remains blocked without an explanation. The plan expressly requires actionable diagnostics for preserved uncertain evidence and offline upgrade failures.

**Fix:** retain the latest sanitized coordinator outcome, including per-workspace eligibility reasons, and expose it in the passive status response. Render useful recovery guidance on `/mcp`, including when the global plugin is active but the selected workspace needs review. Keep raw parser/credential values out of responses. Distinguish ordinary unused/off state from a blocked upgrade; ensure successful retry refreshes/clears stale errors. Add a production-coordinator-to-status/page regression for invalid marker and an early offline dependency/installation failure (and a mixed healthy/needs-review workspace).

**Focused evidence:** seeded configured disabled card and a malformed migration record, then called actual `prepare_startup_plugins()` and the actual status function with the same host app. Internal outcome assertions passed: `needs_review`, `('invalid_upgrade_record',)`. Exact command:

```sh
.superpowers/sdd/2026-10-20-mcp-plugin/run-tests -c pyproject.toml -p tests.conftest /private/tmp/task_mcp_final_review_probe.py -k diagnostic
```

Actual output:

```text
AssertionError: {'upgrade': ('invalid_upgrade_record',), 'status_response': {'id': 'qwenpaw-mcp', 'installed': False, 'enabled': False, 'status': 'not_installed', ...}}
assert []
FAILED ::test_upgrade_review_diagnostic_reaches_status_api
1 failed, 3 deselected in 0.73s
```

Temporary test root: `/private/tmp/qwenpaw-mcp-test.zyZcgy/pytest`. This probe uses the production endpoint function directly; it does not claim another HTTP/AuthMiddleware exercise.

### Minor

#### M1 — Deferred Task 8 peer failure cleanup remains actionable

`tests/fixtures/mcp/optional_runtime_probe.py:447` starts the OAuth local peer before the cleanup `try/finally` at `:462`. Readiness or subsequent fixture preparation failure can bypass `peer.close()`. `LocalPeer.stop()` at `:152` terminates and waits ten seconds but has no timeout kill/wait fallback; `close()` also does not close its log if stop raises. Move startup/preparation into cleanup ownership and use bounded terminate→kill→wait with log closure in `finally`. This is a new acceptance-fixture failure-path issue, not evidence of a production resource leak. It remains nonblocking by itself and is suitable for the consolidated fix wave.

#### M2 — Reconcile “one-time unused record” wording with actual implementation

`src/qwenpaw/app/mcp_plugin_upgrade.py:339` returns `skipped` for unused users without writing a completed unused migration record. Startup inventories/classifies them again on each launch. The canonical spec says the unused decision completes a one-time record. The implemented behavior is conservative and does not install/connect/start periodic work, so this is not a runtime correctness blocker. The coordinator should explicitly accept/document repeated read-only startup classification or implement the promised bookkeeping without allowing a stale unused record to suppress later real configuration. Do not present the current path as having persisted a one-time unused completion.

## Explicit triage of all deferred Minor items

1. **Task 2/6 legacy shutdown/uninstall-hook deprecations:** retained compatibility-fixture warnings, not a new production failure. No blanket suppression or unrelated legacy-hook removal recommended.
2. **Task 4 eight static unmanaged-resource warnings:** nonblocking after reviewing custody. The lifecycle task is retained; failed bounded joins create retained reapers; `_await_rpc` keeps unfinished work in `_rpc_tasks`; abandonment moves it into `_rpc_cleanup_tasks`; callbacks remove only finished tasks. Request waiters are canceled/joined in `finally`, drain is shielded/joined in the lifecycle owner, and strict `close`/`close_owned` rejects pending cleanup. Source resource scope, descriptor and manager retain failed owners. The existing public-host regression proves nonquiescent unload until the stubborn RPC is released. This is bounded attempts plus retained custody, not guaranteed forced termination. The static report is still eight warnings, not warning-free; no checker exemption is warranted.
3. **Task 5/7 TestClient and JSDOM pseudo-element warnings; Task 8 lark/websockets/aibot warnings:** dependency/test-environment noise with no concrete changed-behavior defect established. Preserve visibility; no dependency upgrade is required by this review.
4. **Task 6 mypy/annotation informational notes:** the original note concerns an existing untyped registry constructor and passing hooks. No additional blocker. Final changed-file hooks reported passing and the durable record preserves this scope.
5. **Task 8 M1:** still present; retained as M1 above, with exact failure-path remediation. **Task 8 I1:** repaired; logical archive paths now use POSIX form and copied interpreter layout is derived from its original venv-relative path. Recorded focused distribution result is seven passes, not a native Windows run.

## Checks and evidence boundaries

Read the canonical plan including internal spec/global constraints/review focus, binding final-review notes, durable validation and decisions. Reviewed the supplied change package and current production paths in passes: SDK/governance/credentials/manager; lazy service/watcher/workspace; classifier/upgrade/loader/intent/receipts; compatibility APIs/OAuth; Console and plugin wrapper; transport cleanup and result adaptation. Read relevant tests and scoped reports to resolve specific risks rather than treating prior task approval as this review's conclusion.

Read the actual external SDK `report.json`: all seven return codes are zero, expected wheel/sdist hashes, actual installed-wheel import path, AgentScope 2.0.10. Its author-test log ends **327 passed, 3 warnings in 364.12s**. Read raw broad-source and repair tails: **1974 passed, 6 failed, 7 skipped, 31 warnings** initially; affected cold/distribution **15 passed**; corrected UV **1 passed**; subsequent distribution portability **7 passed**. Read Console **220 tests/27 files passed** and TypeScript/Prettier success. No broad suite, SDK release check, benchmark, localhost or network probe was rerun.

The three focused new-risk probes above used the supplied current-source isolated runner, temporary config/workspaces/secrets, null keyring and actual AgentScope 2.0.10. They are intentionally failing behavioral assertions; they do not invalidate earlier successful runs, but identify missing integration coverage. Final probe file now contains all four test cases. The root coordinator independently verified all 30 benchmark raw hashes/matrix/durations/cache/source provenance/zero final resources/normalization parity; I rely on that provided provenance and the durable report, not a new independent remeasurement.

## Declined to judge

- Native PyInstaller executables, signing and installation: unavailable here; verified evidence is installed wheel plus frozen spec-derived resource layout, not a native executable.
- All supported OS/Python-minor combinations and Windows named-pipe execution: only the reported macOS/Python 3.13 environment ran; Windows-style path regressions are not native Windows execution.
- Literal power loss and filesystem durability under a physical power cut: deterministic interrupted-write/copy/settings/marker/cancellation evidence is the available scope.
- External OAuth providers/accounts, internet-hosted MCP interoperability and model services: acceptance uses controlled local peers and mock/local OAuth; no external credentials/network were used.
- Automatic replay of failed non-idempotent MCP RPCs: not promised by the plan; reconnect evidence must not be interpreted as safe replay.
- Complete removal of upstream `mcp.*` imports/dependencies: explicitly outside the binding requirement; shared consumers remain and are separately counted.
- Agent Mail/ACP pluginization or new consumer business behavior: explicitly excluded; reviewed their host compatibility surface only.
- Whole-application performance, statistical significance, material RSS/startup improvements: benchmark isolates actual Workspace Driver descriptors and does not establish those broader claims.
- Static analysis as a security sandbox or complete permission audit: dynamic paths remain uncovered and permission review was not run; official plugin source/lifecycle was reviewed, not arbitrary malicious Python isolation.
- Forced termination of cancellation-suppressing Python coroutines: not technically guaranteed; reviewed retained ownership, failed quiescence and later cleanup instead.
- Independent operating-system-process credential writers: no multi-process CAS probe was run. The new revision checks use the existing path-shared in-process store lock; this review establishes the supported host's same-process concurrency, not a new cross-process storage guarantee. Coordinator should keep that boundary explicit if deployment assumptions differ.
- Future request deadline propagation where the host supplies a deadline: current production invocation inputs provide identities/cancellation and no deadline; the optional SDK deadline remains `None`. No new deadline scheduling behavior was promised or experimentally established.

## Recommendations and readiness

Use one consolidated fix wave for I1–I3; M1 is a small accompanying fixture repair. Resolve M2 explicitly in the decision record/spec rather than silently claiming exact one-time bookkeeping. Keep focused independent regressions through the real consumer boundaries, and scope the re-review to the fixes and their affected lifecycle/result/status paths. I1 must retain stale-publication and policy-revocation protections while restoring failure atomicity; do not solve it by removing identity checks.

**Ready to merge: With fixes (not ready at reviewed HEAD).** The architecture and recorded release coverage are strong, but failed updates currently break a retained source, embedded resource text is lost before reaching the agent, and safe startup recovery outcomes disappear before users can act on them. Fix and verify these three Important findings, then conduct the single scoped re-review.
