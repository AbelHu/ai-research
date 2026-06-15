"""AI provider abstraction (design spec section 7).

The only place that knows how to talk to a specific model backend. Skills and
core control code never import this directly with a fixed model - they go
through roles configured in `config/models.yaml`.

Implemented now:
    - GitHubModelsProvider  (default; GitHub Models, OpenAI-compatible + GitHub auth)
    - OpenAICompatibleProvider (OpenAI, Azure, Ollama, or any OpenAI-style API)

Every outbound message is passed through the secret-redaction guard (O16)
before it leaves the machine.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from app.advisor.redaction import ensure_no_secrets, redact_messages, redact_text
from app.security import Secret

# GitHub Models endpoints (verified against the GitHub Models REST API).
GITHUB_MODELS_HOST = "https://models.github.ai"
GITHUB_MODELS_API_VERSION = "2026-03-10"

# Chat request protocol ("api_mode"). Every backend here speaks the OpenAI
# **Chat Completions** API (POST ``{base_url}/chat/completions``); the field lets
# a model definition pin it explicitly and leaves room for future protocols.
DEFAULT_API_MODE = "chat_completions"
_API_MODE_PATHS = {"chat_completions": "chat/completions"}
SUPPORTED_API_MODES = frozenset(_API_MODE_PATHS)


def _as_secret(value: Secret | str | None) -> Secret | None:
    """Normalize an API key into a `Secret` (or None) so it cannot leak."""
    if value is None or isinstance(value, Secret):
        return value
    return Secret(value)


# Cap on how much of a provider error body we surface (enough to name the bad
# field, short enough to keep logs/messages readable).
_ERROR_BODY_LIMIT = 600


def _raise_for_status(resp: httpx.Response) -> None:
    """Like ``resp.raise_for_status()`` but include the provider's error body.

    OpenAI-compatible providers describe *why* a request was rejected in the
    response body (e.g. "Unsupported value: 'temperature'…"). The stock
    ``raise_for_status`` drops it, leaving a blind "HTTP 400"; surfacing it turns
    the error actionable. The body is secret-scrubbed (O16/§12) and truncated
    before it goes into the exception message (and therefore into any log).
    """
    if not resp.is_error:
        return
    body = redact_text((resp.text or "").strip())
    if len(body) > _ERROR_BODY_LIMIT:
        body = body[:_ERROR_BODY_LIMIT] + "…"
    detail = f" — {body}" if body else ""
    request = resp.request
    raise httpx.HTTPStatusError(
        f"{resp.status_code} {resp.reason_phrase} from {request.method} {request.url}{detail}",
        request=request,
        response=resp,
    )


@dataclass
class CompletionRequest:
    messages: list[dict]
    temperature: float = 0.2
    max_tokens: int | None = None


@dataclass
class CompletionResponse:
    text: str
    model: str
    raw: dict = field(default_factory=dict)


@dataclass
class EmbedRequest:
    texts: list[str]


@dataclass
class EmbedResponse:
    vectors: list[list[float]]
    model: str


class AIProvider(Protocol):
    """Transport contract. Implementations are selected purely by config."""

    model: str

    def complete(self, req: CompletionRequest) -> CompletionResponse: ...

    def embed(self, req: EmbedRequest) -> EmbedResponse: ...


class OpenAICompatibleProvider:
    """Works with any OpenAI-style /chat/completions and /embeddings API."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: Secret | str | None = None,
        api_mode: str = DEFAULT_API_MODE,
        extra_headers: dict | None = None,
        timeout: float = 60.0,
    ) -> None:
        if api_mode not in _API_MODE_PATHS:
            supported = ", ".join(sorted(SUPPORTED_API_MODES))
            raise ValueError(f"unsupported api_mode {api_mode!r} (supported: {supported})")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_mode = api_mode
        self._chat_path = _API_MODE_PATHS[api_mode]
        self._api_key = _as_secret(api_key)
        self._extra_headers = extra_headers or {}
        self._timeout = timeout

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json", **self._extra_headers}
        if self._api_key:
            # reveal() is the single boundary where the real token is used.
            headers.setdefault("Authorization", f"Bearer {self._api_key.reveal()}")
        return headers

    def complete(self, req: CompletionRequest) -> CompletionResponse:
        # O16: scrub secrets from every message before it leaves the machine.
        payload: dict = {
            "model": self.model,
            "messages": redact_messages(req.messages),
            "temperature": req.temperature,
        }
        if req.max_tokens is not None:
            payload["max_tokens"] = req.max_tokens
        # We deliberately do NOT send `response_format` (OpenAI JSON mode): the
        # request body is already JSON (Content-Type: application/json) and the
        # prompt templates ask for a JSON reply, which the advisor wrapper parses
        # (code-fence-tolerant) + repairs. Some providers (reasoning / local
        # models) reject `response_format` with a 400, so omitting it keeps the
        # transport compatible with the widest set of OpenAI-style endpoints.

        with httpx.Client(timeout=self._timeout) as client:
            resp = client.post(
                f"{self.base_url}/{self._chat_path}",
                headers=self._headers(),
                json=payload,
            )
            _raise_for_status(resp)
            data = resp.json()

        text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        return CompletionResponse(text=text, model=self.model, raw=data)

    def embed(self, req: EmbedRequest) -> EmbedResponse:
        # O16/§12: never embed text that contains secrets. Unlike chat content,
        # an embedding input cannot be safely scrubbed in place (a redacted
        # string yields a meaningless vector), so we strict-block instead.
        ensure_no_secrets(req.texts)
        with httpx.Client(timeout=self._timeout) as client:
            resp = client.post(
                f"{self.base_url}/embeddings",
                headers=self._headers(),
                json={"model": self.model, "input": req.texts},
            )
            _raise_for_status(resp)
            data = resp.json()

        vectors = [item["embedding"] for item in data.get("data", [])]
        return EmbedResponse(vectors=vectors, model=self.model)


class GitHubModelsProvider(OpenAICompatibleProvider):
    """GitHub Models: OpenAI-compatible body + GitHub auth and headers.

    When `org` is set, requests use the org-attributed endpoint so usage is
    tracked against your enterprise organization.
    """

    def __init__(self, *, token: Secret | str, model: str, org: str | None = None) -> None:
        if org:
            base_url = f"{GITHUB_MODELS_HOST}/orgs/{org}/inference"
        else:
            base_url = f"{GITHUB_MODELS_HOST}/inference"
        super().__init__(
            base_url=base_url,
            model=model,
            api_key=token,
            extra_headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": GITHUB_MODELS_API_VERSION,
            },
        )
        self.org = org


class GitHubCopilotProvider(OpenAICompatibleProvider):
    """GitHub Copilot (Route A): OpenAI-compatible body + a device-flow bearer.

    Unlike the PAT-based providers, the bearer is **fetched per request** from
    the device-flow auth layer (`GitHubCopilotAuth.get_bearer`), which
    transparently exchanges/refreshes the short-lived Copilot API token. The raw
    token never appears in config — it comes from the git-ignored auth cache.
    """

    def __init__(self, *, auth, model: str, timeout: float = 60.0) -> None:
        from app.advisor.auth import (
            COPILOT_API_BASE,
            COPILOT_INTEGRATION_ID,
            EDITOR_VERSION,
            USER_AGENT,
        )

        super().__init__(
            base_url=COPILOT_API_BASE,
            model=model,
            api_key=None,
            extra_headers={
                "Editor-Version": EDITOR_VERSION,
                "Copilot-Integration-Id": COPILOT_INTEGRATION_ID,
                "Openai-Intent": "conversation-panel",
                "User-Agent": USER_AGENT,
            },
            timeout=timeout,
        )
        self._auth = auth

    def _headers(self) -> dict:
        # The bearer is dynamic: ask the auth layer (it refreshes as needed) and
        # reveal it only here, at the HTTP boundary.
        headers = {"Content-Type": "application/json", **self._extra_headers}
        headers["Authorization"] = f"Bearer {self._auth.get_bearer()}"
        return headers


def build_provider(provider_cfg, *, getenv=os.getenv) -> AIProvider:
    """Construct a provider from a `ProviderConfig` (see config/settings.py).

    `getenv` is injectable for testing.
    """
    kind = provider_cfg.kind

    if kind == "github_models":
        token = getenv(provider_cfg.api_key_env or "GITHUB_MODELS_TOKEN")
        if not token:
            raise MissingCredentialError(provider_cfg.api_key_env or "GITHUB_MODELS_TOKEN")
        org = getenv(provider_cfg.org_env) if provider_cfg.org_env else None
        return GitHubModelsProvider(token=Secret(token), model=provider_cfg.model, org=org or None)

    if kind == "github_copilot":
        # Route A: no api_key_env — the bearer comes from the device-flow cache.
        from app.advisor.auth import GitHubCopilotAuth

        return GitHubCopilotProvider(auth=GitHubCopilotAuth(), model=provider_cfg.model)

    if kind == "openai_compatible":
        if not provider_cfg.base_url:
            raise ValueError("openai_compatible provider requires `base_url`")
        raw_key = getenv(provider_cfg.api_key_env) if provider_cfg.api_key_env else None
        return OpenAICompatibleProvider(
            base_url=provider_cfg.base_url,
            model=provider_cfg.model,
            api_key=Secret(raw_key) if raw_key else None,
            api_mode=provider_cfg.api_mode,
        )

    if kind == "ollama":
        # Ollama exposes an OpenAI-compatible API at /v1 and needs no key.
        base_url = (provider_cfg.base_url or "http://localhost:11434").rstrip("/")
        if not base_url.endswith("/v1"):
            base_url = f"{base_url}/v1"
        return OpenAICompatibleProvider(
            base_url=base_url, model=provider_cfg.model, api_mode=provider_cfg.api_mode
        )

    raise ValueError(f"unknown provider kind: {kind!r}")


class MissingCredentialError(RuntimeError):
    """Raised when a provider's API key env var is not set."""

    def __init__(self, env_var: str) -> None:
        self.env_var = env_var
        super().__init__(
            f"Environment variable {env_var!r} is not set. "
            f"Add it to your .env file (see .env.example)."
        )
