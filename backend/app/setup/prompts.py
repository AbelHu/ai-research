"""Interactive prompt helpers for the setup wizard (implementation-plan T9.2).

`ask` / `confirm` / `secret` with **injectable** input/output so the whole wizard
is scriptable in tests (no real TTY, no network). Two guarantees the tests pin:

* **Skip-existing default** — each helper takes a *current* value; when one
  exists it is offered as the default, so pressing **Enter keeps it**. Secrets
  are shown only as a fixed mask, never revealed.
* **Secrets never leak** — a secret the user types is read via a no-echo reader
  and is **never written to the output stream or logs** (§12).
"""

from __future__ import annotations

import getpass
from collections.abc import Callable

Reader = Callable[[str], str]
Writer = Callable[[str], None]

# What we show in place of an existing secret value (never the real bytes).
SECRET_MASK = "********"


class Prompter:
    """Injectable console prompts. Defaults use stdin/stdout + no-echo getpass."""

    def __init__(
        self,
        *,
        reader: Reader = input,
        secret_reader: Reader = getpass.getpass,
        writer: Writer = print,
    ) -> None:
        self._reader = reader
        self._secret_reader = secret_reader
        self._writer = writer

    def say(self, message: str = "") -> None:
        """Print an informational line (status/help text)."""
        self._writer(message)

    def ask(self, label: str, *, current: str | None = None, default: str | None = None) -> str:
        """Prompt for a plain value. Enter keeps ``current`` (else ``default``)."""
        fallback = current if current is not None else default
        suffix = f" [{fallback}]" if fallback not in (None, "") else ""
        answer = self._reader(f"{label}{suffix}: ").strip()
        if not answer:
            return fallback or ""
        return answer

    def secret(self, label: str, *, current: str | None = None) -> str:
        """Prompt for a secret with **no echo**. Enter keeps an existing value.

        A present ``current`` is shown only as `SECRET_MASK`; the typed value is
        never echoed and never passed to the (loggable) writer.
        """
        suffix = f" [{SECRET_MASK}]" if current else ""
        answer = self._secret_reader(f"{label}{suffix}: ").strip()
        if not answer:
            return current or ""
        return answer

    def confirm(self, label: str, *, default: bool = False) -> bool:
        """Yes/no prompt. Enter takes ``default``."""
        hint = "[Y/n]" if default else "[y/N]"
        answer = self._reader(f"{label} {hint}: ").strip().lower()
        if not answer:
            return default
        return answer in ("y", "yes")
