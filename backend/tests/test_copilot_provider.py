"""Tests for the GitHub Copilot provider (Route A) — implementation-plan T7.2."""

from __future__ import annotations

import json

import httpx
import pytest

from app.advisor.auth import COPILOT_API_BASE, GitHubCopilotAuth
from app.advisor.providers import (
    CompletionRequest,
    EmbedRequest,
    GitHubCopilotProvider,
    GitHubCopilotResponsesProvider,
    build_provider,
)
from app.config.settings import ProviderConfig


class _StubAuth:
    """Stands in for GitHubCopilotAuth: hands back a fixed bearer."""

    def __init__(self, bearer: str = "copilot-bearer") -> None:
        self.bearer = bearer
        self.calls = 0

    def get_bearer(self) -> str:
        self.calls += 1
        return self.bearer


def _mock_client_factory(captured: dict):
    real_client = httpx.Client

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "pong"}}], "model": "gpt-4o"},
        )

    def factory(*_args: object, **_kwargs: object) -> httpx.Client:
        return real_client(transport=httpx.MockTransport(handler))

    return factory


def test_provider_sets_dynamic_bearer_and_copilot_headers(monkeypatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(httpx, "Client", _mock_client_factory(captured))
    auth = _StubAuth("copilot-xyz")
    provider = GitHubCopilotProvider(auth=auth, model="gpt-4o")

    resp = provider.complete(CompletionRequest(messages=[{"role": "user", "content": "ping"}]))

    req = captured["request"]
    assert str(req.url) == f"{COPILOT_API_BASE}/chat/completions"
    assert req.headers["Authorization"] == "Bearer copilot-xyz"
    assert req.headers["Editor-Version"]
    assert req.headers["Copilot-Integration-Id"] == "vscode-chat"
    assert auth.calls == 1  # bearer fetched at request time
    assert resp.text == "pong"


def test_bearer_is_fetched_per_request(monkeypatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(httpx, "Client", _mock_client_factory(captured))
    auth = _StubAuth()
    provider = GitHubCopilotProvider(auth=auth, model="gpt-4o")

    provider.complete(CompletionRequest(messages=[{"role": "user", "content": "a"}]))
    provider.complete(CompletionRequest(messages=[{"role": "user", "content": "b"}]))
    assert auth.calls == 2  # re-fetched so a refreshed token is always used


def test_redaction_still_applies(monkeypatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(httpx, "Client", _mock_client_factory(captured))
    provider = GitHubCopilotProvider(auth=_StubAuth(), model="gpt-4o")

    secret = "ghp_0123456789abcdefghijklmnopqrstuvwxyzABCD"
    provider.complete(CompletionRequest(messages=[{"role": "user", "content": f"tok {secret}"}]))

    body = json.loads(captured["request"].content)
    assert secret not in json.dumps(body)  # scrubbed before leaving the machine


def test_build_provider_selects_copilot(tmp_path) -> None:
    cfg = ProviderConfig(kind="github_copilot", model="gpt-4o")
    provider = build_provider(cfg)
    assert isinstance(provider, GitHubCopilotProvider)
    assert provider.model == "gpt-4o"
    # No token env var is consulted for Route A (the bearer comes from the cache).
    assert isinstance(provider._auth, GitHubCopilotAuth)


def test_copilot_provider_does_not_need_api_key_env() -> None:
    # build must not raise MissingCredentialError for github_copilot.
    cfg = ProviderConfig(kind="github_copilot", model="gpt-4o-mini")
    build_provider(cfg, getenv=lambda _k: None)  # no env at all → still builds


# --- Responses API transport (gpt-5.5 / gpt-5.3-codex, /responses only) -------


def _mock_responses_factory(captured: dict, *, output_text: str = ""):
    """Mock httpx.Client that returns an OpenAI Responses-API payload.

    The ``output`` array interleaves a hidden ``reasoning`` item with the final
    ``message`` item, matching what the real endpoint returns.
    """
    real_client = httpx.Client

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(
            200,
            json={
                "model": "gpt-5.3-codex",
                "status": "completed",
                "output_text": output_text,
                "output": [
                    {"type": "reasoning", "content": [{"type": "text", "text": "thinking"}]},
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "pong"}],
                    },
                ],
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        )

    def factory(*_args: object, **_kwargs: object) -> httpx.Client:
        return real_client(transport=httpx.MockTransport(handler))

    return factory


def test_responses_provider_sends_input_and_parses_message(monkeypatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(httpx, "Client", _mock_responses_factory(captured))
    provider = GitHubCopilotResponsesProvider(
        auth=_StubAuth("copilot-xyz"), model="gpt-5.3-codex", max_tokens=2048
    )

    resp = provider.complete(
        CompletionRequest(messages=[{"role": "user", "content": "ping"}], max_tokens=2048)
    )

    req = captured["request"]
    assert str(req.url) == f"{COPILOT_API_BASE}/responses"
    assert req.headers["Authorization"] == "Bearer copilot-xyz"
    body = json.loads(req.content)
    # Responses protocol: `input`, not `messages`; max_output_tokens, not max_tokens.
    assert body["input"] == [{"role": "user", "content": "ping"}]
    assert body["max_output_tokens"] == 2048
    assert "messages" not in body
    assert "max_tokens" not in body
    # Reasoning models reject a non-default temperature, so we never send one.
    assert "temperature" not in body
    # Text is pulled from the `message` item, skipping the `reasoning` item.
    assert resp.text == "pong"
    assert resp.model == "gpt-5.3-codex"


def test_responses_provider_prefers_output_text_when_present(monkeypatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(httpx, "Client", _mock_responses_factory(captured, output_text="hi there"))
    provider = GitHubCopilotResponsesProvider(auth=_StubAuth(), model="gpt-5.5")

    resp = provider.complete(CompletionRequest(messages=[{"role": "user", "content": "x"}]))
    assert resp.text == "hi there"


def test_responses_provider_redacts_secrets(monkeypatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(httpx, "Client", _mock_responses_factory(captured))
    provider = GitHubCopilotResponsesProvider(auth=_StubAuth(), model="gpt-5.3-codex")

    secret = "ghp_0123456789abcdefghijklmnopqrstuvwxyzABCD"
    provider.complete(CompletionRequest(messages=[{"role": "user", "content": f"tok {secret}"}]))

    assert secret not in captured["request"].content.decode()


def test_responses_provider_omits_max_output_tokens_when_unset(monkeypatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(httpx, "Client", _mock_responses_factory(captured))
    provider = GitHubCopilotResponsesProvider(auth=_StubAuth(), model="gpt-5.5")

    provider.complete(CompletionRequest(messages=[{"role": "user", "content": "x"}]))
    assert "max_output_tokens" not in json.loads(captured["request"].content)


def test_responses_provider_embed_not_supported() -> None:
    provider = GitHubCopilotResponsesProvider(auth=_StubAuth(), model="gpt-5.5")
    with pytest.raises(NotImplementedError):
        provider.embed(EmbedRequest(texts=["hi"]))


def test_build_provider_selects_responses_transport() -> None:
    cfg = ProviderConfig(kind="github_copilot", model="gpt-5.3-codex", api_mode="responses")
    provider = build_provider(cfg)
    assert isinstance(provider, GitHubCopilotResponsesProvider)
    assert provider.model == "gpt-5.3-codex"


def test_build_provider_defaults_to_chat_transport() -> None:
    # Without api_mode=responses, github_copilot stays on /chat/completions.
    cfg = ProviderConfig(kind="github_copilot", model="gpt-4o")
    provider = build_provider(cfg)
    assert isinstance(provider, GitHubCopilotProvider)
    assert not isinstance(provider, GitHubCopilotResponsesProvider)
