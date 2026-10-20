- **I3 residual — repaired live workspace is actually activated after retry: ADDRESSED.** `src/qwenpaw/app/mcp_plugin_upgrade.py:274–276` now refreshes classification/admission, reconciles existing live workspaces, and only then publishes the refreshed result. The newly added `_reconcile_live_workspaces` at lines 286–309 reads current saved-off intent, iterates only registry-published workspace objects, filters ineligible workspaces, skips absent/stopped facades, and invokes their existing `activate_protocol("mcp")`. It does not construct dormant agents, force a plugin reload, replay the projector, or introduce another runtime owner. This closes the demonstrated active/eligible-with-no-runtime defect.

### New breakage in the amendment

- **Critical: none found. Important: none found. Minor: none found within this amendment.**
- **Runtime/admission check:** the called facade validates exact live workspace identity (`src/qwenpaw/app/driver_runtime_service.py:77–96,271–275`; `src/qwenpaw/plugins/_drivers.py:67–76`). Source activation enters existing tracked operations, repeats workspace/protocol checks before manager creation, and attaches the existing governed protocol (`driver_runtime_service.py:360–401,469–480`). Existing stop/cancellation/reclaim and manager/descriptor custody remain authoritative; the amendment does not detach or discard ownership itself. The native plugin lifecycle lock still surrounds retry, and the additional current-settings read checks saved explicit-off intent before reconciliation. A propagated reconciliation error follows the existing finite failed-result path rather than publishing the successful result.
- **Enabled/disabled check:** the existing activation implementation requests a watcher for configured sources and opens only enabled cards (`driver_runtime_service.py:271–297`). The new regression proves an enabled repaired source actually invokes through the production facade, while a disabled repaired source retains its saved disabled value and receives only the existing two-second watcher, with no manager/transport.
- **Sibling check:** reconciliation reads `Workspace.driver_manager` through its existing non-constructing service lookup (`src/qwenpaw/app/workspace/workspace.py:205–207`). The regression proves the healthy sibling retains its manager, watcher and task identities with no reopen/close, the still-blocked live sibling remains ineligible with no manager/watcher and unchanged saved bytes, and the dormant sibling is never constructed. All opened transports close on shutdown.

### Checks and evidence

- **Exact scope:** only the prior I3 live-retry residual and new breakage in `.superpowers/sdd/2026-10-20-mcp-plugin/review-029011ac9..773f1fc5b.diff`, base `029011ac9`, head `773f1fc5b`. The package changes one production file, the associated integration regression, and the root's documented narrow completion ruling. Read the appended production/test reports and the directly implicated existing runtime/admission/custody code. No Git diff regeneration, source/test/index/HEAD/branch/dependency changes, children, second broad review, or reopening of I1/I2/M1/M2.
- **Independent RED inspected:** `/private/tmp/final-fix-i3-live-retry-guards-red.log` ends with both enabled/disabled cases failing, **2 failed, 7 deselected in 1.44s**. The durable report distinguishes earlier fixture/setup failures from this behavioral RED.
- **Actual focused GREEN log inspected:** `/private/tmp/final-fix-i3-live-retry-green.log` contains:

```text
..                                                                       [100%]
2 passed, 7 deselected in 1.32s
```

- **Exact focused command, recorded by the independent test author:**

```sh
.superpowers/sdd/2026-10-20-mcp-plugin/run-tests -c pyproject.toml tests/integration/test_mcp_recovery_diagnostics.py -k real_retry_recovers_live > /private/tmp/final-fix-i3-live-retry-green.log 2>&1
```

- **Actual covering GREEN log inspected:** `/private/tmp/final-fix-i3-live-covering-green.log` ends **61 passed, 1 warning in 10.30s**. The warning is the existing TestClient/httpx deprecation. Exact recorded command:

```sh
.superpowers/sdd/2026-10-20-mcp-plugin/run-tests -c pyproject.toml tests/integration/test_mcp_recovery_diagnostics.py tests/integration/test_mcp_plugin_api.py tests/unit/app/test_lazy_driver_runtime.py tests/unit/app/test_mcp_config_watcher_activation.py tests/integration/test_mcp_plugin_upgrade.py::test_valid_sibling_install_does_not_activate_ambiguous_workspace tests/integration/test_mcp_plugin_upgrade.py::test_newer_explicit_off_during_upgrade_wins_over_stale_snapshot tests/integration/test_mcp_plugin_upgrade.py::test_nonempty_facade_cannot_resurrect_explicitly_disabled_plugin tests/integration/test_mcp_plugin_upgrade.py::test_preinstalled_enabled_plugin_with_no_sources_remains_registered > /private/tmp/final-fix-i3-live-covering-green.log 2>&1
```

- **Test assertions inspected:** `tests/integration/test_mcp_recovery_diagnostics.py:229–409` uses the official optional plugin, controlled offline peer, actual published workspaces/lazy facades and real install endpoint. Healthy invocation succeeds before repair; recovered invocation succeeds after retry. It asserts exact peer opens/calls/closes, source enabled intent, watcher interval/task, healthy ownership, still-blocked saved bytes and absent dormant workspace. These assertions answer the prior probe's missing behavior, so no reviewer test rerun or new probe was warranted.
- **Source hooks inspected:** `final-fix-code-i3-residual-hooks-frozen.txt` records configured checks exit 0, mypy1 clean, pylint10.00, and final comma plus Black `--check` exit 0 with the file unchanged. Earlier formatting within that log is not represented as an initially unchanged run. No verification commands were rerun by this reviewer.

### Out-of-scope observations and declined judgments

- **New out-of-scope code observations:** none.
- **Prior reports preserved:** `final-rereview.md` remains the original failed review; this separate amendment closes only its I3 residual. I1/I2/M1/M2 retain their addressed verdicts without a second review. The original broad review's static custody warnings, dependency and platform bounds remain as recorded.
- **Current artifacts/release:** not judged here. The refreshed wheel/sdist identity, full seven-stage SDK command, frontend production artifact and current-source zero-work smoke are separate root-owned gates; their latest outputs were not supplied as complete for this review. This report makes no current-artifact pass claim.
- **Historical evidence:** the original SDK327 result, thirty numeric benchmark rows and the short counter smoke pinned to `029011ac9` retain their original identities. None is relabeled as verification or performance measurement of `773f1fc5b` by this amendment.
- **Other declined judgments:** exhaustive scheduling interleavings beyond the inspected guards and existing affected regressions; real remote providers; other native OS/Python combinations; native frozen executable/signing; physical power-loss durability; whole-application CPU/RSS improvement. Those require separate evidence and do not justify reopening untouched code or repeating broad suites in this narrowly authorized pass.

### Verdict

- **Narrow I3 amendment: ADDRESSED, no new Critical/Important breakage found.** The specific live-retry code blocker is closed. The prior review's other addressed findings remain closed. Code-review findings are resolved within the authorized scope; final goal/release completion still depends on the root-owned current-artifact gates and must not be inferred from this code verdict alone.
