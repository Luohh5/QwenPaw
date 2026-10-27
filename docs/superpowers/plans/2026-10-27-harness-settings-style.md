# Harness Settings Style Restoration Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development. Functional code and test code have separate authors.

**Goal:** Restore the previous card-based third-party agent configuration design in both create and edit dialogs.

**Architecture:** Keep the host's provider selector generic and driven by the backend catalog. Restore the original presentation inside each plugin's own Settings component, with plugin-owned bundled styles. Reuse the existing host design tokens, spacing, icons and responsive behavior.

**Tech Stack:** React, TypeScript, existing Console Less/CSS, Vite plugin library builds.

**Spec:** User's 2026-10-27 request and attached screenshot: rounded provider cards with icon blocks and a selected check, section headings and separators, a labelled binary field with help and refresh icons, a green detected-runtime strip with truncated path and copy action, a separate account card, and three capability cards. This is restoration of a supplied design, not a new design exploration.

## Global Constraints

- Preserve plugin ownership: Codex/Qoder authentication, executable detection, account details, capabilities and vendor wording stay in `plugins/harness/<vendor>/frontend`.
- Core provider cards are catalog-driven; do not hardcode vendors or restore the screenshot's unsupported Claude Code placeholder.
- Do not add a discovery request for unselected providers or change authentication, polling, cancellation, backend APIs or the Python SDK.
- Use concise colocated code and existing public frontend SDK capabilities. No new runtime dependencies.
- Preserve keyboard access, narrow-screen layout, dark theme, long paths and long account identifiers.
- Production author does not edit test files. Independent QA may update only assertions made stale by changed accessible markup, and must retain existing behavioral coverage.
- ZIP artifacts may be updated only in `/Users/luohh/Documents/Qwenpaw/harness插件化`; do not update the user's installed plugin copies or restart/modify the backend on 9099.
- Keep the existing `refactor/plugin_harness` checkout. No push, publish or unrelated code changes.

## Review Focus

- Unknown third-party backend: generic selector and schema fallback still work.
- Disabled or failed plugin: unavailable card and no active Settings component.
- Long executable path/account identity: no horizontal overflow and copy retains the full path.
- Light/dark and desktop/mobile: controls remain readable and usable.
- Opening/closing and switching providers: existing detection/login lifetime and polling behavior remain unchanged.

### Task 1: Restore the supplied layout without changing plugin boundaries

**Files:**
- Modify `console/src/pages/Settings/Agents/components/AgentBackendFields.tsx` and its existing `.module.less` only as needed for generic card presentation.
- Modify `plugins/harness/{codex,qoder}/frontend/src/Settings.tsx`, adjacent styles, and frontend build configuration only as needed to bundle styles into independently loadable plugin scripts.
- Use existing localized wording. Plugin locales or generic selector translations may change only if required for the restored visible layout.
- Test existing Console suites for AgentBackendFields, vendorHarnessSettings, registry/loader, locales and font-size coverage.

**Interfaces:** Keep `HarnessSettingsProps`, backend descriptors and public SDK registration unchanged; no vendor-specific imports in Console production code.

- [x] Compare baseline `0db9a8e625e73917a0d113e255b3147134022db8` markup/styles and current plugin Settings; run the two existing settings test files as a baseline.
- [x] Restore selector heading, card hierarchy, icon containers, selected check and unavailable state using existing metadata only.
- [x] Restore each plugin's binary status, account and capability sections using the screenshot's existing design; bundle plugin styles so the current script-only loader delivers them.
- [x] Independent QA runs focused behavior tests and checks public SDK builds; add no redundant presentation-only tests.
- [x] Build both plugin frontends and Console, and perform an isolated browser visual check with desktop/mobile and light/dark views.
- [x] Review the task diff for spec compliance and code quality; fix any material findings through the production author.
- [x] Repack and verify the two canonical local ZIPs if frontend payloads changed; record verification and commit only task-scoped changes with normal hooks.

## Verification

- Separate production and test authors; only the supported icon namespace was added to the existing vendor test fixture. No new tests or weakened assertions.
- Baseline settings tests: 11 passed. Existing related ownership, loading, SDK and locale coverage: 86 passed across eight suites. After decorative-icon and responsive-grid polish, both directly affected suites passed again: 11 tests, including exact translated button names.
- Both plugin TypeScript/Vite builds and the full Console production build passed after final changes. Prettier checks and `git diff --check` passed.
- Browser preview used actual host components and built plugin JavaScript with synthetic account/status data, without connecting to 9099. Checked Codex/Qoder switching, create/edit presentation, desktop width 1280, mobile width 390, light/dark palettes, three capability cards, long paths/account names, and absence of horizontal overflow.
- Scoped review: spec PASS; code quality APPROVE; decorative icon finding addressed.
- Both existing `1.0.0` ZIPs in the user-designated directory were rebuilt using the repository packer. Each has 11 entries, all matching current source; only `frontend/dist/index.js` changed relative to the previous ZIP. Existing installed plugins were not changed.
- Codex ZIP SHA-256: `af69bc30bb33866a0acc410131d5a72ffabbf079424ca27e3b8cdf8334cf84ee`.
- Qoder ZIP SHA-256: `f7f81dd08a12123c52e674be8fece937f0149ee3d57656d07198bf0e0bab4a80`.
