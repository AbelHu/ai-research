"""Tests for the `Secret` type (design-spec O16, §12).

`Secret` must never expose its value through `repr`/`str`/`format`/logging, is
immutable, integrates with pydantic, and is the type providers hold for API
keys so a logged provider can't leak credentials.
"""

from __future__ import annotations

import logging

import pytest
from pydantic import BaseModel

from app.advisor.providers import OpenAICompatibleProvider
from app.config.settings import Settings
from app.security import REDACTED, Secret

PLANTED = "ghp_0123456789abcdefghijklmnopqrstuvwxyz12"


def test_reveal_returns_value() -> None:
    assert Secret(PLANTED).reveal() == PLANTED


def test_str_and_repr_are_redacted() -> None:
    s = Secret(PLANTED)
    assert str(s) == REDACTED
    assert PLANTED not in repr(s)
    assert repr(s) == f"Secret({REDACTED})"


def test_fstring_and_format_are_redacted() -> None:
    s = Secret(PLANTED)
    assert f"token={s}" == f"token={REDACTED}"
    assert format(s, ">20") == REDACTED


def test_percent_logging_does_not_leak(caplog: pytest.LogCaptureFixture) -> None:
    s = Secret(PLANTED)
    with caplog.at_level(logging.INFO):
        logging.getLogger("t").info("using %s", s)
    assert PLANTED not in caplog.text
    assert REDACTED in caplog.text


def test_is_immutable() -> None:
    s = Secret(PLANTED)
    with pytest.raises(AttributeError):
        s._value = "other"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        del s._value  # type: ignore[attr-defined]


def test_bool_and_len_without_revealing() -> None:
    assert bool(Secret("x")) is True
    assert bool(Secret("")) is False
    assert len(Secret("abcd")) == 4


def test_equality_and_hash() -> None:
    assert Secret("a") == Secret("a")
    assert Secret("a") != Secret("b")
    # A Secret never equals a raw string (avoids accidental comparisons).
    assert (Secret("a") == "a") is False
    assert Secret("a") in {Secret("a")}


def test_rejects_non_str() -> None:
    with pytest.raises(TypeError):
        Secret(1234)  # type: ignore[arg-type]


class _Model(BaseModel):
    token: Secret | None = None


def test_pydantic_coerces_str_to_secret() -> None:
    m = _Model(token="abc")
    assert isinstance(m.token, Secret)
    assert m.token.reveal() == "abc"
    assert _Model().token is None


def test_pydantic_json_dump_is_redacted() -> None:
    dumped = _Model(token="supersecret").model_dump_json()
    assert "supersecret" not in dumped
    assert REDACTED in dumped


def test_settings_token_field_is_secret_and_redacts() -> None:
    settings = Settings(github_models_token="tok-12345")
    assert isinstance(settings.github_models_token, Secret)
    assert settings.github_models_token.reveal() == "tok-12345"
    # The token must not appear if the settings object is logged/repr'd.
    assert "tok-12345" not in repr(settings)


def test_provider_holds_secret_and_does_not_leak() -> None:
    provider = OpenAICompatibleProvider(
        base_url="https://api.example.com/v1",
        model="m",
        api_key=PLANTED,
    )
    # Stored as a Secret, revealed only at the header boundary.
    assert isinstance(provider._api_key, Secret)
    assert provider._headers()["Authorization"] == f"Bearer {PLANTED}"
    # Dumping the provider's attributes must not expose the key.
    assert PLANTED not in repr(vars(provider))


def test_provider_accepts_secret_directly() -> None:
    provider = OpenAICompatibleProvider(
        base_url="https://api.example.com/v1",
        model="m",
        api_key=Secret(PLANTED),
    )
    assert provider._headers()["Authorization"] == f"Bearer {PLANTED}"
