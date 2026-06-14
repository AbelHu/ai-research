"""Characterization tests for the secret-redaction guard (design-spec O16, §12).

Each redaction rule gets a positive case (a planted secret is detected and
removed) and the guard as a whole gets a negative case (normal prose is left
untouched, no false positives). These lock the current behavior so later
refactors can't silently weaken the "never send secrets to a model" guarantee.
"""

from __future__ import annotations

import pytest

from app.advisor.redaction import (
    REDACTED,
    SecretLeakError,
    ensure_no_secrets,
    find_secrets,
    redact_messages,
    redact_text,
)

# A fake GitHub PAT: `ghp_` + 36 alphanumerics. Not a real credential.
FAKE_GH_PAT = "ghp_0123456789abcdefghijklmnopqrstuvwxyz12"
# A fake fine-grained PAT: `github_pat_` + >=50 chars.
FAKE_GH_FG_PAT = "github_pat_" + "A1" * 30
# A fake OpenAI-style key: `sk-` + >=20 chars.
FAKE_OPENAI_KEY = "sk-abcdEFGH0123456789ijklMNOP"
FAKE_PRIVATE_KEY = (
    "-----BEGIN RSA PRIVATE KEY-----\n"
    "MIIEowIBAAKCAQEA0aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789\n"
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ\n"
    "-----END RSA PRIVATE KEY-----"
)


@pytest.mark.parametrize(
    ("label", "text", "secret"),
    [
        ("github_pat", f"my token is {FAKE_GH_PAT} ok", FAKE_GH_PAT),
        ("github_fine_grained_pat", f"use {FAKE_GH_FG_PAT} please", FAKE_GH_FG_PAT),
        ("openai_key", f"OPENAI={FAKE_OPENAI_KEY}", FAKE_OPENAI_KEY),
        ("aws_access_key", "key AKIAIOSFODNN7EXAMPLE here", "AKIAIOSFODNN7EXAMPLE"),
        (
            "bearer_token",
            "Authorization: Bearer abcDEF123456ghiJKL",
            "abcDEF123456ghiJKL",
        ),
        (
            "secret_assignment",
            'password = "hunter2supersecret"',
            "hunter2supersecret",
        ),
        (
            "connection_string_password",
            "postgres://admin:p4ssw0rdValue@db.example.com:5432/app",
            "p4ssw0rdValue",
        ),
        ("private_key", FAKE_PRIVATE_KEY, "PRIVATE KEY"),
    ],
)
def test_find_secrets_detects_each_rule(label: str, text: str, secret: str) -> None:
    found = find_secrets(text)
    labels = {lbl for lbl, _ in found}
    assert label in labels, f"expected rule {label!r} to fire on: {text!r}"


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        (f"my token is {FAKE_GH_PAT} ok", FAKE_GH_PAT),
        (f"use {FAKE_GH_FG_PAT} please", FAKE_GH_FG_PAT),
        (f"OPENAI={FAKE_OPENAI_KEY}", FAKE_OPENAI_KEY),
        ("key AKIAIOSFODNN7EXAMPLE here", "AKIAIOSFODNN7EXAMPLE"),
        ("Authorization: Bearer abcDEF123456ghiJKL", "abcDEF123456ghiJKL"),
        ('password = "hunter2supersecret"', "hunter2supersecret"),
        (
            "postgres://admin:p4ssw0rdValue@db.example.com:5432/app",
            "p4ssw0rdValue",
        ),
    ],
)
def test_redact_text_removes_secret(text: str, secret: str) -> None:
    out = redact_text(text)
    assert secret not in out
    assert REDACTED in out


def test_redact_text_removes_private_key_body() -> None:
    out = redact_text(f"here is the key:\n{FAKE_PRIVATE_KEY}\nthanks")
    assert "MIIEowIBAAKCAQEA" not in out
    assert REDACTED in out


def test_secret_assignment_preserves_key_name_context() -> None:
    # Grouped rules redact only the value, keeping the surrounding key name.
    out = redact_text('api_key = "myS3cretValue123"')
    assert "api_key" in out
    assert "myS3cretValue123" not in out
    assert REDACTED in out


def test_no_false_positive_on_normal_prose() -> None:
    prose = (
        "The quick brown fox jumps over the lazy dog. "
        "Please review the pull request and update the changelog before release. "
        "Version 2.0 ships on Friday with three new features."
    )
    assert find_secrets(prose) == []
    assert redact_text(prose) == prose


def test_redact_text_handles_empty_and_none() -> None:
    assert redact_text("") == ""
    assert find_secrets("") == []


def test_redact_messages_scrubs_only_string_content() -> None:
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": f"token: {FAKE_GH_PAT}"},
        {"role": "tool", "content": None},
    ]
    out = redact_messages(messages)

    # Original messages are not mutated.
    assert FAKE_GH_PAT in messages[1]["content"]
    # Redacted copy no longer contains the secret.
    assert FAKE_GH_PAT not in out[1]["content"]
    assert REDACTED in out[1]["content"]
    # Non-secret content is preserved; non-string content is left as-is.
    assert out[0]["content"] == "You are a helpful assistant."
    assert out[2]["content"] is None


def test_ensure_no_secrets_passes_clean_text() -> None:
    # Returns None and does not raise when there is nothing to find.
    assert ensure_no_secrets(["just some normal text", "version 2.0 ships Friday"]) is None


def test_ensure_no_secrets_raises_on_hit() -> None:
    with pytest.raises(SecretLeakError) as exc:
        ensure_no_secrets(["fine", f"token: {FAKE_GH_PAT}"])
    # The error reports rule labels, never the secret value itself.
    assert "github_pat" in exc.value.labels
    assert FAKE_GH_PAT not in str(exc.value)
