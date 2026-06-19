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
#   * (Linux, optional) Installs `bubblewrap` for the Coder sandbox if it's
#     missing - non-fatal; the sandbox falls back to resource limits without it.
#   * (Opt-in) With --with-browser, also installs Playwright + a Chromium binary
#     for the browser.* skills. This step is ONLINE and NOT hash-pinned (it
#     downloads a ~150 MB browser), so it is OFF by default and non-fatal.
#
# Usage:
#   ./scripts/setup-env.sh            # auto: offline if wheelhouse exists
#   ./scripts/setup-env.sh --offline  # require the wheelhouse; fail if missing
#   ./scripts/setup-env.sh --online   # ignore the wheelhouse; fetch from PyPI
#   ./scripts/setup-env.sh --with-browser  # also install Playwright + Chromium (online)
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
WITH_BROWSER=0
for arg in "$@"; do
  case "$arg" in
    --offline) MODE="offline" ;;
    --online) MODE="online" ;;
    --with-browser) WITH_BROWSER=1 ;;
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

# --- optional: bubblewrap for the Coder sandbox (Linux only) -----------------
# The Coder validates generated skills in a sandbox; with bubblewrap (`bwrap`)
# each check runs in a no-network user namespace. It is OPTIONAL - without it the
# sandbox falls back to `resource` limits - so any failure here is a warning,
# never a setup error. Non-interactive sudo (`sudo -n`) is used so this step can
# never hang on a password prompt.
ensure_bubblewrap() {
  if command -v bwrap >/dev/null 2>&1; then
    echo "==> bubblewrap present ($(command -v bwrap)) - Coder sandbox isolation enabled"
    return 0
  fi
  if [[ "$(uname -s)" != "Linux" ]]; then
    echo "==> Note: bubblewrap is Linux-only; the Coder sandbox will use the rlimit fallback."
    return 0
  fi

  local -a cmd=()
  if command -v apt-get >/dev/null 2>&1; then
    cmd=(apt-get install -y bubblewrap)
  elif command -v dnf >/dev/null 2>&1; then
    cmd=(dnf install -y bubblewrap)
  elif command -v pacman >/dev/null 2>&1; then
    cmd=(pacman -S --noconfirm bubblewrap)
  elif command -v zypper >/dev/null 2>&1; then
    cmd=(zypper install -y bubblewrap)
  fi
  if [[ ${#cmd[@]} -eq 0 ]]; then
    echo "==> [warn] bubblewrap missing and no known package manager; install it"
    echo "           manually for Coder sandbox isolation (optional)."
    return 0
  fi

  echo "==> Installing bubblewrap (Coder sandbox isolation)"
  if sudo -n "${cmd[@]}" >/dev/null 2>&1; then
    echo "    bubblewrap installed."
  else
    echo "    [warn] could not install automatically (needs sudo). Run: sudo ${cmd[*]}"
  fi
}
ensure_bubblewrap

# --- optional: headless-browser skill deps (opt-in; NOT offline) -------------
# The browser.* skills (app/skills/browser.py) drive headless Chromium via
# Playwright. This is OPTIONAL and OFF BY DEFAULT because it breaks the
# offline/hash-pinned guarantee: it installs an unpinned PyPI package and then
# downloads a ~150 MB Chromium binary from Microsoft's CDN. Enable it with
# `--with-browser`. Failures here are warnings, never setup errors.
install_browser() {
  [[ "$WITH_BROWSER" == "1" ]] || return 0

  echo "==> Installing headless-browser support (Playwright + Chromium) [--with-browser]"
  echo "    note: this step is online and NOT hash-pinned (it downloads a browser binary)."
  if ! "$VENV_PY" -m pip install --disable-pip-version-check -e "$REPO_ROOT/backend[browser]"; then
    echo "    [warn] could not install the 'browser' extra (Playwright); skipping."
    return 0
  fi
  if ! "$VENV_PY" -m playwright install chromium; then
    echo "    [warn] 'playwright install chromium' failed; re-run:"
    echo "           $VENV_PY -m playwright install chromium"
    return 0
  fi
  # Chromium also needs system shared libraries (libnss3, libnspr4, libasound2,
  # …). `playwright install-deps` installs them via the OS package manager,
  # which needs root: run it directly when already root, else try non-interactive
  # sudo, else print the manual command so the user can finish it.
  local deps=("$VENV_PY" -m playwright install-deps chromium)
  if [[ "$(id -u)" -eq 0 ]]; then
    "${deps[@]}" || echo "    [warn] install-deps failed; install Chromium's system libs manually."
  elif command -v sudo >/dev/null 2>&1 && sudo -n "${deps[@]}" >/dev/null 2>&1; then
    echo "    installed Chromium's system libraries."
  else
    echo "    [warn] Chromium also needs system libraries. Finish with:"
    echo "           sudo ${deps[*]}"
  fi
  echo "    browser.search / browser.fetch are usable once the system libs above are present."
}
install_browser

echo
echo "Done. Next steps (from the repo root):"
echo "  source .venv/bin/activate"
echo "  cd backend && python -m pytest -q"
