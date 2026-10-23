# Qoder Harness

Optional, workspace-owned Qoder backend using the public QwenPaw plugin SDK 1.1
and `qoder-agent-sdk==1.0.9`. Install this plugin separately from the native
QwenPaw runtime. Registration declares metadata only; the first actual operation
opens state, resolves the CLI and creates SDK clients as needed.

The backend preserves Qoder streaming, compact, attachments, PAT/browser login,
models and reasoning effort, permission callbacks, read-only Skill discovery,
and effective QwenPaw Skill/MCP projection. Noninteractive logout and provider
MCP discovery are explicitly unsupported by the SDK integration.

Session mappings live in this workspace's
`plugin-data/qwenpaw-harness-qoder/qoder_sessions.json`. A missing target copies
and validates `harnesses/qoder_sessions.json` without altering it. Existing valid
target state wins; null or corrupt state is rejected without overwriting files.
Legacy session keys are claimed once by a trusted user/channel/session identity.
Other users, channels and workspaces get separate clients and mappings.

The host owns backend close. Calls bind permissions to their live public call
context and cancel/await pending approvals on exit. SDK clients, login/status
processes and filesystem operations remain owned until cleanup actually finishes;
close does not log out or delete Qoder's native conversations. Reset affects only
its admitted conversation identity. Async client state stays on the event loop;
bounded local filesystem/discovery operations run in workers and finish before
cancellation releases their owner or mapping lock.

Run plugin tests in an isolated environment with the host SDK and test
requirements installed. Tests use fake clients and inert process fixtures;
real accounts, CLI operation and platform release acceptance are separate checks.
Old builtin source remains only as unmounted migration residue until A6.


## Frontend build

The plugin owns its account settings, authentication polling and locale assets.
The Console renders this settings component only for its selected active owner.
API calls use public `host.fetch`, preserving Bearer authentication and the
selected Agent header. No private Console imports or copied SDK types are used.

Install normal build dependencies, then the actual public SDK npm directory
(exported from the installed host with `python -m qwenpaw.plugins.check .
--frontend /path/to/qwenpaw-plugin-sdk`):

```sh
cd frontend
npm install
npm install --no-save --package-lock=false /path/to/qwenpaw-plugin-sdk
npm run build
```

For checkout validation the public SDK directory is
`src/qwenpaw/plugins/frontend`. Release builders must explicitly prepare that
same exported package before building. `frontend/dist/index.js` is the actual
self-contained manifest entry; source/build files are excluded from the runtime
archive. Build before packing. Registration uses SDK `harnesses.register` and
host cleanup disposes the extension when disabled or removed.
