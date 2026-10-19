# WeChat group identity compatibility implementation plan

> **For agentic workers:** Use superpowers:subagent-driven-development. Functional code and tests have separate owners.

**Goal:** Admit WeChat group messages through the real Runner–Host boundary while retaining shared group history, actor identity and reply routing.

**Architecture:** At native message construction, use the existing group session key as its non-empty user_id. Leave common normalization, ACL identities, session IDs and platform reply metadata unchanged. The old Runtime already mapped an empty user_id to session_id.

**Spec:** User-approved minimal fix in this conversation: platform.py uses `session if group else sender`; full Runner→Host regression coverage, no weakened public validation.

## Global Constraints

- Work in the existing `codex/channel-pluginization` checkout. Preserve all existing dirty changes. No commit/push or real WeChat credentials/network calls.
- Keep production code and tests concise; separate subagents own them. Only run affected tests. No package installation.
- Select this checkout's `src` and `packages/qwenpawmail-mcp/src`, not the venv's sibling editable repository.
- Do not relax Host validation, add fabricated users, or mark known senders anonymous. Retain actual sender_id/acl_sender_id and original context-token reply metadata.

## Review Focus

- Group messages must be accepted by actual Host admission, not a fake acknowledgement.
- Same group/different members share stable session/user identity; different groups remain separate; direct messages retain sender user_id.
- User and session mapping must retain the old runtime/history-file behavior.
- ACL still evaluates the actual member; replies still use the original sender and context metadata.
- Invalid known-actor messages with missing user_id still fail common protocol validation.

### Task 1: Independent regressions and RED

**Files:** Prefer a new small suite under `tests/fixtures/channel_plugins/` and a new focused wrapper under `tests/unit/plugins/channels/`. Reuse existing formal WeChat/runtime/HTTP fixtures. Do not modify pre-existing dirty tests.

- [x] Read existing `wechat_w4_formal_support.py`, formal runtime tests and mock iLink HTTP fixture.
- [x] Exercise actual Platform→Driver→Runner IPC→Host admission→queue for group and DM messages, using only external iLink HTTP simulation. Never fabricate Host success or bypass normalization.
- [x] Verify same-group members, cross-group isolation, member ACL identities, persisted reply sender/context, and compatible history filename. Pin malformed empty known-actor user_id rejection with actual normalization.
- [x] Run RED against unchanged platform.py, record expected failure(s), fixture/setup limitations and hashes. Freeze assertions; notify controller before source edits.
- [x] After implementation, run GREEN plus only the relevant existing WeChat tests and formatting/lint checks. Report exact outer/child counts where visible; no full suite.

### Task 2: Minimal source fix, separate functional owner

**File:** `plugins/channel/wechat/qwenpaw_channel_wechat/platform.py:232`.

- [x] Wait for independent RED evidence.
- [x] Replace `"user_id": "" if group else sender` with `"user_id": session if group else sender` in `_message`. No other behavioral changes.
- [x] Run scoped formatting/syntax/diff checks, never modify tests. Report the exact diff and source hash.

### Task 3: Verify and review

- [x] Confirm frozen regressions and affected tests pass; independently inspect the one-line diff and final hashes.
- [x] Obtain independent spec and quality review. Route any fixes to the proper source/test owner.
- [x] Report the fix, test evidence and simulation limits. Leave changes in the current checkout.

## Verification outcome

Final regression wrapper passed (6 child cases); three affected existing wrappers passed (11 collected child cases). Real Runner IPC/Host admission/queue use simulated local iLink HTTP. Formal test setup builds authenticated official artifacts from the current source, reads persisted route_refs before terminal cleanup, and retains all identity/ACL/reply expectations. Source changes exactly one line; local generated bundle refreshed. Independent review passed with one nonblocking compatibility limit: legacy ChatSpec records with empty group user_id may yield a new list/index entry; historical session file path remains compatible. No index migration or real WeChat/account acceptance is claimed.
