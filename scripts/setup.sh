#!/usr/bin/env bash
#
# setup.sh - one command to take an EXISTING checkout to a runnable app.
#
# Two steps, in order:
#   1. scripts/setup-env.sh - create .venv/ and install the pinned dependencies.
#   2. python -m app.cli.setup - the guided configuration wizard (AI provider,
#      Telegram bot token, owner pairing), ending with `verify --dry-run`.
#
# It performs NO git operations: cloning the repo is a prerequisite, not setup's
# job. All real logic lives in the Python wizard (unit-tested); this wrapper just
# sequences the two commands.
#
# Usage (run from anywhere inside the checkout):
#   ./scripts/setup.sh              # env, then guided setup (skips what works)
#   ./scripts/setup.sh --check      # env, then report config status (no changes)
#   ./scripts/setup.sh --reconfigure
#
# Any arguments are forwarded to the wizard. To control the env install mode,
# set AIR_SETUP_ENV_ARGS (e.g. AIR_SETUP_ENV_ARGS=--offline ./scripts/setup.sh).
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# 1) Environment: virtualenv + pinned dependencies (no network if a wheelhouse).
# Invoke via `bash` so it works even if the script's executable bit was lost on
# checkout (e.g. a Windows/`core.filemode=false` clone).
# shellcheck disable=SC2086 # intentional word-splitting of the optional args
bash "$REPO_ROOT/scripts/setup-env.sh" ${AIR_SETUP_ENV_ARGS:-}

# 2) Configuration wizard + verification (forward any args, e.g. --check).
VENV_PY="${AIR_VENV_DIR:-$REPO_ROOT/.venv}/bin/python"
cd "$REPO_ROOT/backend"
exec "$VENV_PY" -m app.cli.setup "$@"
