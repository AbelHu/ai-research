"""Advisor wrapper — schema-validated, audited model calls (design-spec §7).

This is the **plugin boundary**: the AI proposes, deterministic code validates.
Every advisor method follows the same pipeline (implementation-plan T3.3-T3.5):

  1. render a versioned template over its inputs (§6D);
  2. **redact** the rendered prompt before it leaves the machine (§12);
  3. call the provider configured for the model-role;
  4. parse the reply into a pydantic schema; on failure run **one bounded
     repair** attempt, then a deterministic **fallback** (or escalate);
  5. write an ``ai_calls`` audit row with the final ``validation_status``.

The provider is injected via a ``resolve_provider(role)`` callable so the whole
layer is testable with a fake provider and never touches the network.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from app.advisor.providers import AIProvider, CompletionRequest, CompletionResponse
from app.advisor.redaction import redact_text
from app.advisor.schemas import Analysis, AnswerDraft, Triage
from app.advisor.templates import Template, load_template
from app.storage.repos import ai_calls as ai_calls_repo

T = TypeVar("T", bound=BaseModel)

# Provider selector: maps a model-role ("triage"/"planner"/"drafter") to a provider.
ProviderResolver = Callable[[str], AIProvider]

_FENCE_RE = re.compile(r"^```(?:json)?\s*\n(.*?)\n```$", re.DOTALL)


class AdvisorValidationError(RuntimeError):
    """Raised when model output fails validation after repair and no fallback exists."""

    def __init__(self, template_id: str) -> None:
        self.template_id = template_id
        super().__init__(f"advisor output failed validation after repair: {template_id}")


def _extract_json(text: str) -> str:
    """Strip an optional ```json code fence so bare-or-fenced JSON both parse."""
    stripped = text.strip()
    match = _FENCE_RE.match(stripped)
    return match.group(1).strip() if match else stripped


def _try_parse(text: str, schema: type[T]) -> T | None:
    """Parse + validate ``text`` into ``schema``; return ``None`` on any failure."""
    try:
        data = json.loads(_extract_json(text))
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        return schema.model_validate(data)
    except ValidationError:
        return None


def _tokens(resp: CompletionResponse) -> int | None:
    usage = resp.raw.get("usage") if isinstance(resp.raw, dict) else None
    if isinstance(usage, dict):
        value = usage.get("total_tokens")
        if isinstance(value, int):
            return value
    return None


def _repair_instruction(template: Template) -> str:
    """The single corrective nudge sent on a failed parse."""
    return (
        "Your previous response was not valid. Reply again with a SINGLE JSON "
        "object that exactly matches the required schema for "
        f"{template.id}. JSON only — no prose, no code fences."
    )


@dataclass
class Advisor:
    """Validated, audited access to the AI advisor (§7)."""

    resolve_provider: ProviderResolver
    conn: sqlite3.Connection
    templates_dir: Path | None = None

    # -- public typed methods (T3.6-T3.8) -----------------------------------

    def triage(self, text: str, *, request_id: int, job_id: int | None = None) -> Triage:
        """Cheap first-pass classification (template ``triage.classify``)."""
        return self._run(
            role="triage",
            template_name="triage.classify",
            variables={"text": text},
            schema=Triage,
            request_id=request_id,
            job_id=job_id,
            fallback=lambda: Triage(
                kind="ask",
                clarity="unclear",
                complexity="complex",
                confidence=0.0,
                rationale="fallback: triage output could not be validated",
            ),
        )

    def analyze(
        self,
        *,
        text: str,
        title: str = "",
        request_code: str = "",
        append: bool = False,
        request_id: int,
        job_id: int | None = None,
    ) -> Analysis:
        """Authoritative validation + classification (template ``analyzer.analyze``)."""
        card = _format_card(text=text, title=title, request_code=request_code, append=append)
        return self._run(
            role="planner",
            template_name="analyzer.analyze",
            variables={"card": card},
            schema=Analysis,
            request_id=request_id,
            job_id=job_id,
            fallback=lambda: Analysis(
                belongs=True,
                kind="ask",
                clarity="unclear",
                complexity="complex",
                confidence=0.0,
                rationale="fallback: analysis output could not be validated",
                clarify=["Could you clarify what you'd like me to do?"],
            ),
        )

    def answer(
        self,
        *,
        text: str,
        hits: list[dict] | None = None,
        request_id: int,
        job_id: int | None = None,
    ) -> AnswerDraft:
        """Draft a cited answer (template ``junior.answer``).

        No deterministic fallback: an answer must carry a real citation, so an
        unvalidatable draft **escalates** (raises) rather than fabricating one.
        """
        return self._run(
            role="drafter",
            template_name="junior.answer",
            variables={"text": text, "hits": json.dumps(hits or [], ensure_ascii=False)},
            schema=AnswerDraft,
            request_id=request_id,
            job_id=job_id,
            fallback=None,
        )

    # -- core pipeline (T3.3-T3.5) ------------------------------------------

    def _run(
        self,
        *,
        role: str,
        template_name: str,
        variables: dict[str, object],
        schema: type[T],
        request_id: int,
        job_id: int | None,
        fallback: Callable[[], T] | None,
    ) -> T:
        template = load_template(template_name, templates_dir=self.templates_dir)
        # Redact at the wrapper boundary too (defense in depth over the provider).
        prompt = redact_text(template.render(**variables))
        provider = self.resolve_provider(role)

        started = time.monotonic()
        value, status, last_response, tokens = self._call_with_repair(
            provider, prompt, schema, template
        )
        latency_ms = int((time.monotonic() - started) * 1000)

        if value is None and fallback is not None:
            value, status = fallback(), "fallback"
        elif value is None:
            status = "failed"

        ai_calls_repo.record_ai_call(
            self.conn,
            request_id=request_id,
            job_id=job_id,
            role=role,
            model_id=provider.model,
            template=template.id,
            prompt_ref=ai_calls_repo.content_ref(prompt),
            response_ref=ai_calls_repo.content_ref(last_response),
            tokens=tokens,
            latency_ms=latency_ms,
            validation_status=status,
        )

        if value is None:
            raise AdvisorValidationError(template.id)
        return value

    def _call_with_repair(
        self,
        provider: AIProvider,
        prompt: str,
        schema: type[T],
        template: Template,
    ) -> tuple[T | None, str, str, int | None]:
        """One call + at most one repair. Returns (value|None, status, text, tokens)."""
        messages = [{"role": "user", "content": prompt}]
        resp = provider.complete(
            CompletionRequest(
                messages=messages, temperature=0.0, response_format={"type": "json_object"}
            )
        )
        parsed = _try_parse(resp.text, schema)
        if parsed is not None:
            return parsed, "valid", resp.text, _tokens(resp)

        # Bounded repair: one more attempt with a corrective instruction.
        repair_messages = messages + [
            {"role": "assistant", "content": resp.text},
            {"role": "user", "content": _repair_instruction(template)},
        ]
        resp2 = provider.complete(
            CompletionRequest(
                messages=repair_messages, temperature=0.0, response_format={"type": "json_object"}
            )
        )
        parsed2 = _try_parse(resp2.text, schema)
        if parsed2 is not None:
            return parsed2, "repaired", resp2.text, _tokens(resp2)

        return None, "invalid", resp2.text, _tokens(resp2)


def _format_card(*, text: str, title: str, request_code: str, append: bool) -> str:
    """Render a minimal RequestCard block for the analyzer prompt (§6D)."""
    lines = ["Request card:"]
    if request_code:
        lines.append(f"- code: {request_code}")
    if title:
        lines.append(f"- title: {title}")
    lines.append(f"- appended detail: {'yes' if append else 'no'}")
    lines.append(f"- text: {text}")
    return "\n".join(lines)
