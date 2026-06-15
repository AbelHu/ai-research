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


# A small controlled vocabulary for the deterministic bag-of-words embedder
# below. Each word is one dimension, so texts sharing words are "near".
_EMBED_VOCAB = (
    "paris",
    "france",
    "capital",
    "weather",
    "rain",
    "recipe",
    "soup",
    "onion",
    "vector",
    "search",
)


def fake_embed(texts, vocab=_EMBED_VOCAB):
    """A deterministic, offline bag-of-words embedder for vector-search tests.

    Each text maps to a fixed-width vector counting how often each vocabulary
    word appears (case-insensitive). Texts that share words get a high cosine
    similarity, so nearest-neighbour assertions are predictable without a model.
    """
    out = []
    for text in texts:
        lowered = text.lower()
        out.append([float(lowered.count(word)) for word in vocab])
    return out
