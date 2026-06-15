#!/usr/bin/env bash
#
# fetch-wheels.sh - (consumer, ONLINE) re-populate the offline wheel cache from
# the committed, hash-pinned lock.
#
# Use this after a fresh `git clone`: vendor/wheels/ is git-ignored (the wheels
# are not pushed to the remote), so this script rebuilds it by downloading the
# EXACT versions already pinned in backend/requirements.lock. Every file is
# verified against its sha256 hash, so you get the same vetted artifacts.
#
# Unlike scripts/lock-deps.sh, this does NOT re-resolve or change the lock - it
# only fetches what the lock already specifies.
#
# When do you need this?
#   * You don't, just to get a working env: scripts/setup-env.sh already
#     installs the pinned + hash-verified deps straight from PyPI when the
#     wheelhouse is absent.
#   * You DO want it if you plan to install OFFLINE afterwards (e.g. air-gapped
#     machines), or to store/vet the binaries locally.
#
# Usage:
#   ./scripts/fetch-wheels.sh
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

WHEELHOUSE="${AIR_WHEELHOUSE:-$REPO_ROOT/vendor/wheels}"
LOCK="$REPO_ROOT/backend/requirements.lock"

if [[ ! -f "$LOCK" ]]; then
  echo "error: $LOCK not found. Generate it first with scripts/lock-deps.sh." >&2
  exit 1
fi

# --- find a python that has pip ----------------------------------------------
# Prefer the project venv; otherwise fall back to a system python3 with pip.
PIP_PY=""
if [[ -x "$REPO_ROOT/.venv/bin/python" ]] &&
  "$REPO_ROOT/.venv/bin/python" -m pip --version >/dev/null 2>&1; then
  PIP_PY="$REPO_ROOT/.venv/bin/python"
elif "${PYTHON:-python3}" -m pip --version >/dev/null 2>&1; then
  PIP_PY="$(command -v "${PYTHON:-python3}")"
else
  echo "error: no pip available." >&2
  echo "       Run scripts/setup-env.sh --online first (creates .venv), then re-run." >&2
  exit 1
fi

echo "==> Using pip from: $PIP_PY"
echo "==> Downloading pinned, hash-verified wheels into $WHEELHOUSE"
mkdir -p "$WHEELHOUSE"
"$PIP_PY" -m pip download \
  --require-hashes \
  --only-binary=:all: \
  --disable-pip-version-check \
  -r "$LOCK" \
  -d "$WHEELHOUSE"

echo
echo "Done. The offline cache is ready. Install offline with:"
echo "  ./scripts/setup-env.sh --offline"
