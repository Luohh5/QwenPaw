# Codex Harness

Optional `qwenpaw-harness-codex` plugin for QwenPaw SDK 1.1 / QwenPaw 2.3.0b1
or later. It provides backend `codex`, independently of the Qoder plugin.

Install this plugin explicitly through QwenPaw's plugin manager. Its pinned
`openai-codex==0.144.4` dependency supplies the Codex executable on supported
platforms; a standalone executable can be selected through the `binary` setting
or `CODEX_BINARY`. Dependency preparation belongs to explicit plugin installation
or repair, never ordinary host startup. Listing/registering the backend does not
resolve a binary, start a process, or create the backend.

After installation, select Codex for an agent. Existing Codex model, reasoning,
approval/sandbox settings and native account remain valid. The backend forwards
text, reasoning, tools, image/file attachments, model/effort selection and the
`compact`, `review`, `skills`, `status`, `new` and `clear` commands. Account login
and logout are explicit management operations. Approvals use the current host
call's public approval callback; unknown or completed calls cannot request them.

Each workspace stores its mappings in
`plugin-data/qwenpaw-harness-codex/codex_sessions.json`. On its first actual open,
a missing target is atomically copied from `harnesses/codex_sessions.json` after
validation. The old file remains untouched and an existing new target wins.
Corrupt state raises an error instead of replacing it with an empty map. Legacy
plain session mappings are claimed once into trusted user/channel/session keys;
a different user or channel with the same session ID receives its own thread.
Regenerable projection configuration stays in memory; native Codex account and
thread data are not moved or deleted by plugin disable/uninstall/close.

PawPort requires this plugin to be active before inspecting Codex sources. Its
online operations use the host's admitted archive facade. The existing explicit
source-home local-only behavior and rollout recovery remain available after that
plugin check; this plugin does not introduce an import path without the plugin.

For development, install `requirements-test.txt` in an isolated environment
containing the host SDK, then run `python -m pytest plugins/harness/codex/tests`.
Tests use synthetic app-server messages, temporary state and public SDK contexts;
they do not require a Codex account or launch the real executable. CLI/platform
acceptance is a separate release check.

During extraction staging, the main application still selects its existing
builtin implementation. This directory is validated in an isolated SDK host;
do not activate two implementations of backend `codex` in one host. Core routing
and removal of the old implementation are separate migration steps.
