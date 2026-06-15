"""Sanity checks for the FakeProvider test double (implementation-plan T3.1)."""

from __future__ import annotations

import pytest

from app.advisor.providers import CompletionRequest, EmbedRequest
from tests.fakes import FakeProvider


def test_complete_returns_canned_text_and_captures_calls() -> None:
    provider = FakeProvider('{"ok": true}')
    resp = provider.complete(CompletionRequest(messages=[{"role": "user", "content": "hi"}]))
    assert resp.text == '{"ok": true}'
    assert resp.model == "fake-model"
    assert resp.raw["choices"][0]["message"]["content"] == '{"ok": true}'
    assert len(provider.calls) == 1


def test_complete_replays_in_order_then_clamps() -> None:
    provider = FakeProvider(["first", "second"])
    assert provider.complete(CompletionRequest(messages=[])).text == "first"
    assert provider.complete(CompletionRequest(messages=[])).text == "second"
    # Exhausted -> last response repeats (handy for "always-malformed" tests).
    assert provider.complete(CompletionRequest(messages=[])).text == "second"


def test_embed_returns_fixed_width_vectors() -> None:
    provider = FakeProvider("unused", embed_dim=4)
    resp = provider.embed(EmbedRequest(texts=["a", "b"]))
    assert resp.vectors == [[0.0, 0.0, 0.0, 0.0], [0.0, 0.0, 0.0, 0.0]]
    assert resp.model == "fake-model"


def test_empty_responses_rejected() -> None:
    with pytest.raises(ValueError, match="at least one"):
        FakeProvider([])
