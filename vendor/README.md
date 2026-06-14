# vendor/wheels — offline dependency cache (wheelhouse)

This directory is the **offline cache** of every Python dependency, pinned to an
exact version. Together with [`backend/requirements.lock`](../../backend/requirements.lock)
(which carries the matching `sha256` hashes) it lets a new machine install the
whole environment **without touching the network** and verifies that no package
was tampered with.

> **`vendor/wheels/` is git-ignored** — the wheels are *not* pushed to the
> remote (they're large, platform-specific binaries). The committed source of
> truth is `backend/requirements.lock`; the wheels are rebuilt locally from it.

## After a fresh `git clone`

The wheelhouse will be empty. You have two options:

- **Just need a working env?** Run `./scripts/setup-env.sh` — with no wheelhouse
  it installs the pinned, hash-verified versions straight from PyPI. No extra
  step needed.
- **Want to install offline afterwards** (air-gapped, or to store the binaries)?
  Repopulate this directory from the lock first:

  ```bash
  ./scripts/fetch-wheels.sh        # downloads the exact, hash-pinned versions
  ./scripts/setup-env.sh --offline # then install with no network
  ```

## How it's used

`scripts/setup-env.sh` installs from here automatically when the directory has
wheels:

```bash
pip install --require-hashes -r backend/requirements.lock \
    --no-index --find-links vendor/wheels
```

`--require-hashes` makes pip reject any file whose bytes don't match the lock.

## Regenerating (only when bumping versions)

Run the maintainer script **online**; it rebuilds this directory and the lock:

```bash
./scripts/lock-deps.sh
```

## Platform note

The cache is platform-specific (built for the platform it was fetched on — here
Linux x86_64 / CPython 3.12) because wheels like `pydantic_core` and `ruff` are
compiled per-platform. For a different OS / CPU / Python version, rebuild on that
platform (`scripts/fetch-wheels.sh`, or `pip download --platform/--python-version`).
