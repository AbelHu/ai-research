"""Deterministic test doubles (implementation-plan T3.1).

`FakeProvider` implements the `AIProvider` transport contract without any
network: `complete()` replays a canned list of responses (the last one repeats
once the list is exhausted, which makes "always-malformed" cases trivial), and
`embed()` returns fixed-width zero vectors. Every request is captured on
``.calls`` so tests can assert what would have left the machine (e.g. redaction).
"""

from __future__ import annotations

from app.advisor.providers import (
    CompletionRequest,
    CompletionResponse,
    EmbedRequest,
    EmbedResponse,
)


class FakeProvider:
    """A canned, offline `AIProvider` for deterministic tests."""

    def __init__(
        self,
        responses: str | list[str],
        *,
        model: str = "fake-model",
        embed_dim: int = 8,
    ):
        self._responses = [responses] if isinstance(responses, str) else list(responses)
        if not self._responses:
            raise ValueError("FakeProvider needs at least one canned response")
        self.model = model
        self._embed_dim = embed_dim
        self._index = 0
        self.calls: list[CompletionRequest | EmbedRequest] = []

    def complete(self, req: CompletionRequest) -> CompletionResponse:
        self.calls.append(req)
        # Replay in order; clamp to the last response so a single canned reply
        # is returned for both the initial call and the repair attempt.
        text = self._responses[min(self._index, len(self._responses) - 1)]
        self._index += 1
        return CompletionResponse(
            text=text,
            model=self.model,
            raw={"choices": [{"message": {"content": text}}]},
        )

    def embed(self, req: EmbedRequest) -> EmbedResponse:
        self.calls.append(req)
        vectors = [[0.0] * self._embed_dim for _ in req.texts]
        return EmbedResponse(vectors=vectors, model=self.model)
