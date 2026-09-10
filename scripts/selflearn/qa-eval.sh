#!/usr/bin/env bash
set -euo pipefail
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export QWENPAW_WORKING_DIR="${QWENPAW_WORKING_DIR:-$HOME/.qwenpaw_selflearn}"
OPTIONS=(--agent "${QWENPAW_QA_EVAL_AGENT:-default}")
if [[ -n "${QWENPAW_QA_EVAL_WORK_DIR:-}" ]]; then
  OPTIONS+=(--work-dir "$QWENPAW_QA_EVAL_WORK_DIR")
fi
exec "$PROJECT_ROOT/.venv/bin/python" -m qwenpaw.selflearn.qa_command "${OPTIONS[@]}" "$@"
