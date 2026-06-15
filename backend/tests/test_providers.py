"""Provider transport tests (implementation-plan T0.6 / T0.7, design-spec §7, §12).

We exercise `OpenAICompatibleProvider` against an `httpx.MockTransport` so the
real request-building path runs (headers, JSON body, URL) but nothing touches
the network. This locks:
  * the outbound `/chat/completions` request/response shape, and
  * that the redaction guard scrubs secrets before they leave the machine.

The autouse `_no_network` guard in conftest additionally guarantees no real
socket is opened.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from app.advisor.providers import (
    CompletionRequest,
    EmbedRequest,
    OpenAICompatibleProvider,
    build_provider,
)
from app.advisor.redaction import SecretLeakError
from app.config.settings import ProviderConfig

Handler = Callable[[httpx.Request], httpx.Response]


def _install_mock_transport(
    monkeypatch: pytest.MonkeyPatch, handler: Handler
) -> dict[str, httpx.Request]:
    """Patch httpx.Client so every request goes through `handler` (no network).

    Returns a dict that captures the last request the provider sent.
    """
    captured: dict[str, httpx.Request] = {}
    real_client = httpx.Client

    def _record(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return handler(request)

    def factory(*_args: object, **_kwargs: object) -> httpx.Client:
        # Drop timeout/base kwargs; route through the mock transport instead.
        return real_client(transport=httpx.MockTransport(_record))

    monkeypatch.setattr(httpx, "Client", factory)
    return captured


def _chat_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": "pong"}}],
            "model": "server-reported-model",
        },
    )


def _make_provider() -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        base_url="https://api.example.com/v1",
        model="test-model",
        api_key="sk-not-a-real-key-1234567890",
    )


def test_complete_request_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _install_mock_transport(monkeypatch, _chat_handler)
    provider = _make_provider()

    resp = provider.complete(
        CompletionRequest(
            messages=[{"role": "user", "content": "ping"}],
            temperature=0.5,
            max_tokens=16,
            response_format={"type": "json_object"},
        )
    )

    req = captured["request"]
    assert req.method == "POST"
    assert str(req.url) == "https://api.example.com/v1/chat/completions"
    assert req.headers["Content-Type"] == "application/json"
    assert req.headers["Authorization"] == "Bearer sk-not-a-real-key-1234567890"

    body = json.loads(req.content)
    assert body["model"] == "test-model"
    assert body["messages"] == [{"role": "user", "content": "ping"}]
    assert body["temperature"] == 0.5
    assert body["max_tokens"] == 16
    assert body["response_format"] == {"type": "json_object"}

    # Response is parsed into the typed shape.
    assert resp.text == "pong"
    assert resp.model == "test-model"
    assert resp.raw["choices"][0]["message"]["content"] == "pong"


def test_complete_omits_optional_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _install_mock_transport(monkeypatch, _chat_handler)
    provider = _make_provider()

    provider.complete(CompletionRequest(messages=[{"role": "user", "content": "hi"}]))

    body = json.loads(captured["request"].content)
    assert "max_tokens" not in body
    assert "response_format" not in body
    assert body["temperature"] == 0.2  # default


def test_complete_no_auth_header_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _install_mock_transport(monkeypatch, _chat_handler)
    provider = OpenAICompatibleProvider(
        base_url="http://localhost:11434/v1",
        model="llama3.1:8b",
    )

    provider.complete(CompletionRequest(messages=[{"role": "user", "content": "hi"}]))

    assert "Authorization" not in captured["request"].headers


def test_complete_redacts_secret_in_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _install_mock_transport(monkeypatch, _chat_handler)
    provider = _make_provider()

    planted = "ghp_0123456789abcdefghijklmnopqrstuvwxyz12"
    provider.complete(
        CompletionRequest(messages=[{"role": "user", "content": f"my pat is {planted}"}])
    )

    raw_body = captured["request"].content.decode()
    assert planted not in raw_body, "secret must not leave the machine"
    assert "[REDACTED]" in raw_body


def test_complete_surfaces_provider_error_body(monkeypatch: pytest.MonkeyPatch) -> None:
    # A 400 from a provider names the bad field in the body; the raised error
    # must carry that text (not a blind "HTTP 400") so the cause is actionable.
    def _bad_request(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "message": "Unsupported value: 'temperature' does not support 0.2",
                    "param": "temperature",
                }
            },
        )

    _install_mock_transport(monkeypatch, _bad_request)
    provider = _make_provider()

    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        provider.complete(CompletionRequest(messages=[{"role": "user", "content": "hi"}]))

    message = str(excinfo.value)
    assert "400" in message
    assert "temperature" in message  # the provider's reason is surfaced
    assert excinfo.value.response.status_code == 400  # still a normal HTTPStatusError


def test_complete_error_body_is_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    # If a provider echoes a secret in its error body, surfacing it must not leak.
    planted = "ghp_0123456789abcdefghijklmnopqrstuvwxyz12"

    def _unauthorized(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text=f"invalid token {planted}")

    _install_mock_transport(monkeypatch, _unauthorized)
    provider = _make_provider()

    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        provider.complete(CompletionRequest(messages=[{"role": "user", "content": "hi"}]))

    message = str(excinfo.value)
    assert planted not in message  # scrubbed before it reaches logs/messages
    assert "[REDACTED]" in message


def _embed_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "data": [
                {"embedding": [0.1, 0.2, 0.3]},
                {"embedding": [0.4, 0.5, 0.6]},
            ],
            "model": "server-reported-embed",
        },
    )


def test_embed_request_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _install_mock_transport(monkeypatch, _embed_handler)
    provider = _make_provider()

    resp = provider.embed(EmbedRequest(texts=["hello", "world"]))

    req = captured["request"]
    assert req.method == "POST"
    assert str(req.url) == "https://api.example.com/v1/embeddings"
    body = json.loads(req.content)
    assert body["model"] == "test-model"
    assert body["input"] == ["hello", "world"]

    assert resp.vectors == [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]
    assert resp.model == "test-model"


def test_embed_redacts(monkeypatch: pytest.MonkeyPatch) -> None:
    # A planted secret must never reach the /embeddings request body. Embedding
    # inputs strict-block (raise) rather than scrub in place.
    captured = _install_mock_transport(monkeypatch, _embed_handler)
    provider = _make_provider()

    planted = "ghp_0123456789abcdefghijklmnopqrstuvwxyz12"
    with pytest.raises(SecretLeakError):
        provider.embed(EmbedRequest(texts=["clean text", f"leak {planted}"]))

    # No request was sent at all, so the secret never left the machine.
    assert "request" not in captured


# --- build_provider: custom (Route C) providers -----------------------------


def test_build_provider_openai_compatible_sends_env_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # A BYO OpenAI-compatible provider: the key comes from the named env var and
    # rides the Authorization header to the configured base_url.
    captured = _install_mock_transport(monkeypatch, _chat_handler)
    cfg = ProviderConfig(
        kind="openai_compatible",
        model="gpt-4o",
        base_url="https://openrouter.ai/api/v1",
        api_key_env="OPENROUTER_API_KEY",
    )
    provider = build_provider(cfg, getenv={"OPENROUTER_API_KEY": "sk-xyz"}.get)
    assert isinstance(provider, OpenAICompatibleProvider)

    provider.complete(CompletionRequest(messages=[{"role": "user", "content": "hi"}]))
    req = captured["request"]
    assert str(req.url) == "https://openrouter.ai/api/v1/chat/completions"
    assert req.headers["Authorization"] == "Bearer sk-xyz"


def test_build_provider_openai_compatible_requires_base_url() -> None:
    cfg = ProviderConfig(kind="openai_compatible", model="gpt-4o")
    with pytest.raises(ValueError):
        build_provider(cfg, getenv=lambda _k: None)


def test_build_provider_ollama_appends_v1_and_needs_no_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _install_mock_transport(monkeypatch, _chat_handler)
    cfg = ProviderConfig(kind="ollama", model="llama3.1:8b", base_url="http://localhost:11434")
    provider = build_provider(cfg, getenv=lambda _k: None)

    provider.complete(CompletionRequest(messages=[{"role": "user", "content": "hi"}]))
    req = captured["request"]
    assert str(req.url) == "http://localhost:11434/v1/chat/completions"
    assert "Authorization" not in req.headers


# --- api_mode (chat request protocol) ---------------------------------------


def test_provider_defaults_to_chat_completions() -> None:
    provider = OpenAICompatibleProvider(base_url="https://x/v1", model="m")
    assert provider.api_mode == "chat_completions"


def test_provider_rejects_unsupported_api_mode() -> None:
    with pytest.raises(ValueError):
        OpenAICompatibleProvider(base_url="https://x/v1", model="m", api_mode="responses")


def test_build_provider_honors_api_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _install_mock_transport(monkeypatch, _chat_handler)
    cfg = ProviderConfig(
        kind="openai_compatible",
        model="m",
        base_url="https://x/v1",
        api_mode="chat_completions",
    )
    provider = build_provider(cfg, getenv=lambda _k: None)
    assert provider.api_mode == "chat_completions"

    provider.complete(CompletionRequest(messages=[{"role": "user", "content": "hi"}]))
    assert str(captured["request"].url) == "https://x/v1/chat/completions"
