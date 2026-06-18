"""Temporary live probe: re-check previously-failing Copilot models.

Sends a tiny prompt to each model and reports status / latency / finish_reason /
completion tokens / a text snippet (or the error). Throwaway diagnostic.
"""

from __future__ import annotations

import sys
import time

sys.path.insert(0, "app/..")  # ensure `app` import works from backend/

from app.advisor.auth import GitHubCopilotAuth
from app.advisor.providers import CompletionRequest, GitHubCopilotProvider

MODELS = [
    "gpt-4o-mini",          # current `fast` (baseline sanity)
    "gpt-4.1",              # current `coder` (baseline sanity)
    "gpt-5.5",              # was 400 — user's preferred `fast`
    "gpt-5.3-codex",        # was 400 — ideal `coder`
    "claude-sonnet-4.6",    # was empty
    "claude-opus-4.8",      # current `quality` (slow/empty risk)
]

PROMPT = "Reply with exactly one word: pong"


def probe(model: str) -> None:
    auth = GitHubCopilotAuth()
    provider = GitHubCopilotProvider(auth=auth, model=model, timeout=120.0, max_tokens=4096)
    t0 = time.monotonic()
    try:
        resp = provider.complete(
            CompletionRequest(messages=[{"role": "user", "content": PROMPT}], temperature=0.0)
        )
    except Exception as exc:  # noqa: BLE001 - diagnostic
        dt = time.monotonic() - t0
        print(f"{model:22s} ERROR   {dt:6.1f}s  {type(exc).__name__}: {str(exc)[:160]}")
        return
    dt = time.monotonic() - t0
    raw = resp.raw or {}
    choices = raw.get("choices") or []
    finish = choices[0].get("finish_reason") if choices else "(no choices)"
    usage = raw.get("usage") or {}
    ctoks = usage.get("completion_tokens")
    text = (resp.text or "").replace("\n", " ")[:60]
    print(
        f"{model:22s} OK      {dt:6.1f}s  finish={finish!s:12s} "
        f"ctoks={ctoks!s:6s} text={text!r}"
    )


if __name__ == "__main__":
    only = sys.argv[1:]
    for m in MODELS:
        if only and m not in only:
            continue
        probe(m)
