"""Deterministic config writers for the setup wizard (implementation-plan T9.1).

Two pure, idempotent file surfaces the wizard calls — **no prompts, no network**:

* `EnvFile` — read/merge/write ``.env`` **preserving existing keys, comments, and
  order**. Setting an existing key updates it **in place** (never duplicated);
  a new key is appended. This is what lets the wizard fill gaps without
  clobbering a partly-configured machine.
* `current_route` / `set_provider_route` — detect + select the
  ``config/models.yaml`` provider route (Route A ``github_copilot`` device-flow
  login vs Route B ``github_models`` PAT) by editing only the ``fast`` and
  ``quality`` provider blocks, leaving the rest of the file (incl. the
  ``embedder`` provider and the how-to comments) untouched.

Nothing here reads secrets or calls out; the wizard supplies values.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# A non-comment ``KEY=value`` assignment (optionally indented); captures the key.
_ENV_ASSIGN = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=(?P<value>.*)$")


class EnvFile:
    """An order/comment-preserving editor for ``.env`` files.

    Parses into a list of raw lines + an index of assignment keys, so ``set``
    can update a key **in place** without disturbing comments, blank lines, or
    ordering. Unknown lines (comments, blanks) are preserved verbatim.
    """

    def __init__(self, text: str = "") -> None:
        # Keep lines without their trailing newline; we re-join with "\n".
        self._lines: list[str] = text.splitlines() if text else []

    @classmethod
    def load(cls, path: Path) -> EnvFile:
        """Load an existing ``.env`` (empty editor if the file is absent)."""
        if not path.exists():
            return cls("")
        return cls(path.read_text(encoding="utf-8"))

    def _find(self, key: str) -> int | None:
        for i, line in enumerate(self._lines):
            match = _ENV_ASSIGN.match(line)
            if match and match.group("key") == key:
                return i
        return None

    def get(self, key: str) -> str | None:
        """Return the raw (unquoted-as-written) value for ``key``, or ``None``."""
        idx = self._find(key)
        if idx is None:
            return None
        match = _ENV_ASSIGN.match(self._lines[idx])
        assert match is not None
        return match.group("value").strip()

    def has_value(self, key: str) -> bool:
        """Whether ``key`` is present **and** non-empty (the skip-existing test)."""
        value = self.get(key)
        return bool(value and value.strip())

    def set(self, key: str, value: str) -> None:
        """Set ``key`` to ``value`` in place (or append if new). Idempotent."""
        line = f"{key}={value}"
        idx = self._find(key)
        if idx is not None:
            self._lines[idx] = line
        else:
            self._lines.append(line)

    def dumps(self) -> str:
        """Serialize back to text (trailing newline, like a normal ``.env``)."""
        if not self._lines:
            return ""
        return "\n".join(self._lines) + "\n"

    def save(self, path: Path) -> None:
        """Write the file, creating parent dirs as needed."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.dumps(), encoding="utf-8")


# --- config/models.yaml provider route -------------------------------------

# The two provider routes the wizard selects between (design-spec §7.2). Only
# the `fast` + `quality` blocks differ; `embedder` always stays on github_models
# because GitHub Copilot has no embeddings endpoint.
ROUTE_COPILOT = "github_copilot"  # Route A: device-flow login, no PAT
ROUTE_MODELS = "github_models"  # Route B: GitHub Models PAT

# Default model ids per route. Copilot uses bare ids; Models uses {publisher}/id.
_ROUTE_MODELS_DEFAULTS = {
    ROUTE_COPILOT: {"fast": "gpt-4o-mini", "quality": "gpt-4o"},
    ROUTE_MODELS: {"fast": "openai/gpt-4o-mini", "quality": "openai/gpt-4o"},
}


@dataclass(frozen=True)
class _ProviderBlock:
    """Indices of a ``providers.<name>:`` block in the YAML line list."""

    start: int  # the `  name:` line
    end: int  # exclusive: first line that is not one of the block's fields


def _find_provider_block(lines: list[str], name: str) -> _ProviderBlock | None:
    """Locate a 2-space-indented ``providers`` child block by name.

    The block is its key line plus the immediately-following **more-indented**
    field lines (4+ spaces); it stops at the first line that isn't a field
    (blank, comment, or the next key), so surrounding comments are never
    swallowed.
    """
    key_re = re.compile(rf"^  {re.escape(name)}:\s*$")
    start = next((i for i, line in enumerate(lines) if key_re.match(line)), None)
    if start is None:
        return None
    end = start + 1
    while end < len(lines):
        line = lines[end]
        if line.strip() and line.startswith("    ") and not line.lstrip().startswith("#"):
            end += 1
        else:
            break
    return _ProviderBlock(start=start, end=end)


def _provider_block_lines(name: str, route: str, model: str) -> list[str]:
    """Render the field lines for one provider block in the given route."""
    lines = [f"  {name}:", f"    kind: {route}", f"    model: {model}"]
    if route == ROUTE_MODELS:
        lines.append("    api_key_env: GITHUB_MODELS_TOKEN")
        lines.append("    org_env: GITHUB_ORG")
    return lines


def _block_field(lines: list[str], block: _ProviderBlock, field: str) -> str | None:
    """Read a scalar ``field:`` value from a block (ignoring any inline comment)."""
    field_re = re.compile(rf"^    {re.escape(field)}:\s*(?P<value>.+?)\s*(?:#.*)?$")
    for line in lines[block.start + 1 : block.end]:
        match = field_re.match(line)
        if match:
            return match.group("value").strip()
    return None


def _block_matches(lines: list[str], block: _ProviderBlock, route: str, model: str) -> bool:
    """Whether a block already encodes the target route + model (skip-if-equal).

    Comparing semantics (not exact text) lets an already-correct block keep its
    inline comments instead of being rewritten on every run (idempotent).
    """
    if _block_field(lines, block, "kind") != route:
        return False
    if _block_field(lines, block, "model") != model:
        return False
    has_pat = _block_field(lines, block, "api_key_env") is not None
    return has_pat == (route == ROUTE_MODELS)


def current_route(models_path: Path, *, role: str = "drafter") -> str | None:
    """Return the provider ``kind`` backing ``role`` (for skip-existing logic).

    Reads the file deterministically (a small parse, no model load). ``None`` if
    the file/role/provider can't be resolved.
    """
    import yaml

    if not models_path.exists():
        return None
    raw = yaml.safe_load(models_path.read_text(encoding="utf-8")) or {}
    provider_name = (raw.get("roles") or {}).get(role)
    provider = (raw.get("providers") or {}).get(provider_name) if provider_name else None
    if not isinstance(provider, dict):
        return None
    kind = provider.get("kind")
    return str(kind) if kind is not None else None


def set_provider_route(
    models_path: Path,
    route: str,
    *,
    fast_model: str | None = None,
    quality_model: str | None = None,
) -> bool:
    """Switch the ``fast`` + ``quality`` providers to ``route``, in place.

    Edits only those two blocks (preserving every other line + comment) and
    returns ``True`` if the file changed. Raises ``ValueError`` for an unknown
    route or if the expected provider blocks are missing.
    """
    if route not in (_ROUTE_MODELS_DEFAULTS):
        raise ValueError(f"unknown provider route: {route!r}")
    defaults = _ROUTE_MODELS_DEFAULTS[route]
    models = {
        "fast": fast_model or defaults["fast"],
        "quality": quality_model or defaults["quality"],
    }

    lines = models_path.read_text(encoding="utf-8").splitlines()
    # Replace the later block first so earlier indices stay valid.
    blocks = []
    for name in ("fast", "quality"):
        block = _find_provider_block(lines, name)
        if block is None:
            raise ValueError(f"provider block {name!r} not found in {models_path}")
        blocks.append((name, block))
    blocks.sort(key=lambda nb: nb[1].start, reverse=True)

    changed = False
    for name, block in blocks:
        if _block_matches(lines, block, route, models[name]):
            continue  # already correct → leave it (and its comments) untouched
        replacement = _provider_block_lines(name, route, models[name])
        lines[block.start : block.end] = replacement
        changed = True

    if changed:
        models_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return changed
