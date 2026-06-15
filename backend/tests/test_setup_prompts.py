"""Tests for the setup prompt helpers (implementation-plan T9.2).

Offline + scripted: a fake reader replays answers and records the prompts it was
shown; a fake writer captures everything printed. Asserts Enter-keeps-current and
that a typed secret never reaches the output stream.
"""

from __future__ import annotations

from app.setup.prompts import SECRET_MASK, Prompter


class _Script:
    """A scripted reader that records the prompts it was shown."""

    def __init__(self, answers: list[str]) -> None:
        self._answers = list(answers)
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self._answers.pop(0)


def _prompter(answers, *, secrets=None):
    reader = _Script(answers)
    secret_reader = _Script(secrets if secrets is not None else [])
    out: list[str] = []
    p = Prompter(reader=reader, secret_reader=secret_reader, writer=out.append)
    return p, reader, secret_reader, out


def test_ask_returns_typed_value() -> None:
    p, _, _, _ = _prompter(["my-answer"])
    assert p.ask("Name") == "my-answer"


def test_ask_enter_keeps_current() -> None:
    p, reader, _, _ = _prompter([""])  # user presses Enter
    assert p.ask("Token", current="existing") == "existing"
    assert "[existing]" in reader.prompts[0]  # current shown as the default


def test_ask_falls_back_to_default_then_empty() -> None:
    p, _, _, _ = _prompter([""])
    assert p.ask("X", default="dflt") == "dflt"
    p2, _, _, _ = _prompter([""])
    assert p2.ask("X") == ""  # nothing typed, no current/default


def test_confirm_parsing_and_default() -> None:
    p, _, _, _ = _prompter(["y"])
    assert p.confirm("ok?") is True
    p, _, _, _ = _prompter(["n"])
    assert p.confirm("ok?", default=True) is False
    p, _, _, _ = _prompter([""])  # Enter → default
    assert p.confirm("ok?", default=True) is True
    p, _, _, _ = _prompter([""])
    assert p.confirm("ok?", default=False) is False


def test_secret_is_read_without_echo_and_not_written() -> None:
    p, reader, secret_reader, out = _prompter([], secrets=["sup3r-secret"])
    value = p.secret("API key")
    assert value == "sup3r-secret"
    # The secret came from the no-echo reader, not the normal reader.
    assert secret_reader.prompts and not reader.prompts
    # And it never reached the (loggable) output stream.
    assert all("sup3r-secret" not in line for line in out)


def test_secret_enter_keeps_current_and_masks_display() -> None:
    p, _, secret_reader, out = _prompter([], secrets=[""])  # Enter
    value = p.secret("API key", current="old-secret")
    assert value == "old-secret"
    # The existing secret is shown only as a mask, never in the clear.
    assert SECRET_MASK in secret_reader.prompts[0]
    assert "old-secret" not in secret_reader.prompts[0]
    assert all("old-secret" not in line for line in out)


def test_say_writes_status_lines() -> None:
    p, _, _, out = _prompter([])
    p.say("hello")
    assert out == ["hello"]
