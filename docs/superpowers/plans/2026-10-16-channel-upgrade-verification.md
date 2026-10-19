# Channel upgrade verification and recovery

> Execute with independent production-code and test authors. Keep checks scoped to changed installation, update, packaging and channel lifecycle behavior.

**Goal:** Previously enabled official channels remain usable after installer environment recreation or an installed-wheel upgrade, without a manual repair action.

**Scope:** `scripts/install.sh`, `scripts/install.ps1`, `scripts/install.bat`, explicit package-update integration, shared official-channel preparation, and focused regression tests.

**Constraints:**
- Preserve configuration, credentials, workspace ownership and channel enablement.
- Respect plugin disable/uninstall intent and keep unused SDKs out of light installations.
- Do required dependency work during explicit installation/update; normal startup and discovery never install dependencies.
- Keep existing dependency integrity, compatibility and child-process checks.
- Use real wheels, real isolated environments, and real loader/child paths; mock external platform I/O only where necessary.
- Do not commit documentation or alter unrelated user files. Do not commit/push without a new request.

## Task 1: Reproduce and fix installer environment recreation

- [x] Test author runs the actual shell installation policy against an isolated environment and offline artifacts; demonstrate loss of a previously enabled channel dependency.
- [x] Production author implements a shared, explicit upgrade-preparation boundary called by all three installers, limited to persisted enabled official channels.
- [x] Verify no manual repair, stable credentials/configuration, disabled/uninstalled intent and failure exit propagation.
- [x] Check Windows installer integration statically where native runners are unavailable; report that limit.

## Task 2: Verify real wheel upgrades

- [x] Build and install a candidate host wheel outside the source checkout; perform an actual pip upgrade in the same environment.
- [x] Exercise retained channel source, dependency admission/probe, receive/send or an equivalent controlled transport path, stop and restart.
- [x] Integrate the shared preparation boundary into explicit `qwenpaw update` if needed; old update-worker code must invoke the newly installed implementation in a fresh interpreter.
- [x] Verify unrelated SDKs are not installed and ordinary startup remains download-free.

## Verification and review

- [x] Run focused new regressions plus existing CLI update, installer command, migration and dependency-gate coverage affected by the final diff.
- [x] Independent review of production changes and tests; resolve material findings with the respective authors.
- [x] Run configured checks on changed files; report actual results and native-platform/real-account limits.
