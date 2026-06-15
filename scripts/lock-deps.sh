#!/usr/bin/env bash
#
# lock-deps.sh - (maintainer, ONLINE) refresh the pinned dependency lock and the
# offline wheelhouse. Run this ONLY when you intentionally bump versions.
#
# What it does:
#   1. Reads the exact set of packages currently installed in .venv.
#   2. Re-downloads every wheel (plus the setuptools/wheel build backend) into
#      vendor/wheels/ - the offline cache.
#   3. Regenerates backend/requirements.lock with exact versions + sha256
#      hashes, so future installs are reproducible and tamper-evident.
#
# Typical bump workflow:
#   - edit version ranges in backend/pyproject.toml
#   - ./.venv/bin/python -m pip install -e backend[dev]   # resolve new versions
#   - ./scripts/lock-deps.sh                              # freeze + cache them
#   - run the test suite, review the lock diff, commit.
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

VENV_PY="$REPO_ROOT/.venv/bin/python"
WHEELHOUSE="$REPO_ROOT/vendor/wheels"
LOCK="$REPO_ROOT/backend/requirements.lock"

if [[ ! -x "$VENV_PY" ]]; then
  echo "error: $VENV_PY not found. Create the dev env first (scripts/setup-env.sh)." >&2
  exit 1
fi

echo "==> Collecting currently-installed versions"
TMP_REQ="$(mktemp)"
trap 'rm -f "$TMP_REQ"' EXIT
"$VENV_PY" -m pip freeze --exclude-editable >"$TMP_REQ"
# The build backend is needed for offline editable installs but is not a
# runtime dependency, so add it explicitly.
printf 'setuptools\nwheel\n' >>"$TMP_REQ"

echo "==> Rebuilding wheelhouse at $WHEELHOUSE"
rm -rf "$WHEELHOUSE"
mkdir -p "$WHEELHOUSE"
"$VENV_PY" -m pip download \
  --only-binary=:all: \
  -r "$TMP_REQ" \
  -d "$WHEELHOUSE"

echo "==> Regenerating $LOCK"
"$VENV_PY" "$REPO_ROOT/scripts/_gen_lock.py" "$WHEELHOUSE" "$LOCK"

echo
echo "Done. Review the diff, run the tests, then commit:"
echo "  backend/requirements.lock   (the source of truth - always commit)"
echo
echo "vendor/wheels/ is git-ignored (not pushed). Anyone can rebuild it from"
echo "the lock with scripts/fetch-wheels.sh."
