#!/usr/bin/env bash
#
# setup-env.sh - create the project virtualenv and install the exact, pinned
# dependencies. Offline-first and tamper-evident.
#
# Behavior:
#   * Creates .venv/ at the repo root if it does not exist.
#   * Installs from backend/requirements.lock with --require-hashes, so a
#     package whose bytes don't match the lock is REJECTED (supply-chain guard).
#   * Prefers the local wheelhouse (vendor/wheels): if present, installs fully
#     OFFLINE (--no-index). Otherwise downloads the pinned versions from PyPI
#     (still hash-verified). Force a mode with --offline / --online.
#   * Installs this repo's backend package in editable mode (no extra deps).
#
# Usage:
#   ./scripts/setup-env.sh            # auto: offline if wheelhouse exists
#   ./scripts/setup-env.sh --offline  # require the wheelhouse; fail if missing
#   ./scripts/setup-env.sh --online   # ignore the wheelhouse; fetch from PyPI
#
# Prerequisites: python3 (>=3.10) with the stdlib `venv` module
# (on Debian/Ubuntu: `sudo apt install python3-venv`).
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# The venv location can be overridden (handy for CI / testing the setup itself).
VENV_DIR="${AIR_VENV_DIR:-$REPO_ROOT/.venv}"
VENV_PY="$VENV_DIR/bin/python"
WHEELHOUSE="$REPO_ROOT/vendor/wheels"
LOCK="$REPO_ROOT/backend/requirements.lock"

MODE="auto"
for arg in "$@"; do
  case "$arg" in
    --offline) MODE="offline" ;;
    --online) MODE="online" ;;
    -h | --help)
      # Print the leading comment block (usage), minus the shebang, stopping
      # at the first non-comment line.
      awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "${BASH_SOURCE[0]}"
      exit 0
      ;;
    *)
      echo "error: unknown argument: $arg (try --help)" >&2
      exit 2
      ;;
  esac
done

if [[ ! -f "$LOCK" ]]; then
  echo "error: $LOCK not found. Run scripts/lock-deps.sh first." >&2
  exit 1
fi

# --- pick a base interpreter -------------------------------------------------
PYTHON="${PYTHON:-python3}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "error: '$PYTHON' not found. Install Python 3.10+ (with the venv module)." >&2
  exit 1
fi

# --- create the venv ---------------------------------------------------------
if [[ ! -x "$VENV_PY" ]]; then
  echo "==> Creating virtualenv at $VENV_DIR"
  if ! "$PYTHON" -m venv "$VENV_DIR"; then
    echo "error: could not create venv. On Debian/Ubuntu: sudo apt install python3-venv" >&2
    exit 1
  fi
else
  echo "==> Reusing existing virtualenv at $VENV_DIR"
fi

# --- decide offline vs online ------------------------------------------------
have_wheels() { compgen -G "$WHEELHOUSE/*.whl" >/dev/null 2>&1; }

INSTALL_FLAGS=()
if [[ "$MODE" == "offline" ]]; then
  if ! have_wheels; then
    echo "error: --offline requested but no wheels in $WHEELHOUSE." >&2
    echo "       Run scripts/lock-deps.sh (online) or copy vendor/wheels here." >&2
    exit 1
  fi
  INSTALL_FLAGS=(--no-index --find-links "$WHEELHOUSE")
  echo "==> Installing OFFLINE from $WHEELHOUSE"
elif [[ "$MODE" == "online" ]]; then
  echo "==> Installing from PyPI (pinned + hash-verified)"
else # auto
  if have_wheels; then
    INSTALL_FLAGS=(--no-index --find-links "$WHEELHOUSE")
    echo "==> Wheelhouse found - installing OFFLINE from $WHEELHOUSE"
  else
    echo "==> No wheelhouse - installing from PyPI (pinned + hash-verified)"
  fi
fi

# --- install pinned dependencies (hash-enforced) -----------------------------
echo "==> Installing pinned dependencies from $LOCK"
"$VENV_PY" -m pip install \
  --require-hashes \
  --disable-pip-version-check \
  "${INSTALL_FLAGS[@]}" \
  -r "$LOCK"

# --- install the local backend package (editable, no re-resolution) ----------
echo "==> Installing the backend package (editable)"
"$VENV_PY" -m pip install \
  --no-build-isolation \
  --no-deps \
  --disable-pip-version-check \
  -e "$REPO_ROOT/backend"

# --- smoke check -------------------------------------------------------------
echo "==> Verifying the install"
"$VENV_PY" -c "import app, pytest, ruff; print('environment OK')"

echo
echo "Done. Next steps (from the repo root):"
echo "  source .venv/bin/activate"
echo "  cd backend && python -m pytest -q"
