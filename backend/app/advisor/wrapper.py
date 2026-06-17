"""Advisor wrapper — schema-validated, audited model calls (design-spec §7).

This is the **plugin boundary**: the AI proposes, deterministic code validates.
Every advisor method follows the same pipeline (implementation-plan T3.3-T3.5):

  1. render a versioned template over its inputs (§6D);
  2. **redact** the rendered prompt before it leaves the machine (§12);
  3. call the provider configured for the model-role;
  4. **validate the reply against the template's declared response schema (the
     *template requirement*)** by parsing into the bound pydantic schema in
     strict mode (extra fields forbidden, so hallucinated keys are rejected);
     on failure run **one bounded repair** attempt, then a deterministic
     **fallback** (or escalate);
  5. write an ``ai_calls`` audit row with the final ``validation_status``.

This anti-hallucination contract applies to **every** AI-facing role that calls
the advisor — the experts (Company Expert, Plan Expert) included: a reply that
does not meet its template requirement is never acted on (§6D, §7). A template
that declares no response schema is rejected outright.

The provider is injected via a ``resolve_provider(role)`` callable so the whole
layer is testable with a fake provider and never touches the network.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from app.advisor.citations import UrlVerifier, http_url_exists, unresolved_citation_urls
from app.advisor.providers import AIProvider, CompletionRequest, CompletionResponse
from app.advisor.redaction import redact_text
from app.advisor.schemas import (
    Analysis,
    AnswerDraft,
    GeneratedSkill,
    PlanSpec,
    ProposedAction,
    Triage,
    Verdict,
)
from app.advisor.templates import Template, load_template
from app.config.policies import get_policies
from app.storage.repos import ai_calls as ai_calls_repo

T = TypeVar("T", bound=BaseModel)

# Audit logger for the AI boundary. Emits one record per model call (role,
# template, model, validation status, tokens, latency) and, on a failed/repaired
# reply, the model's response text so it can be inspected. Prompts are already
# redacted (`redact_text`) and the test log handler scrubs secrets again, so no
# token ever reaches a log file (§12).
logger = logging.getLogger("app.advisor")

# Provider selector: maps a model-role ("triage"/"planner"/"drafter") to a provider.
ProviderResolver = Callable[[str], AIProvider]

# A post-parse semantic check: returns a list of problems ("" = clean) for a
# structurally-valid reply (e.g. cited URLs that don't exist). An empty list
# means the reply passes; a non-empty list triggers repair/escalate.
PostValidator = Callable[[BaseModel], list[str]]

_FENCE_RE = re.compile(r"^```(?:json)?\s*\n(.*?)\n```$", re.DOTALL)


class AdvisorValidationError(RuntimeError):
    """Raised when model output fails validation after repair and no fallback exists."""

    def __init__(self, template_id: str) -> None:
        self.template_id = template_id
        super().__init__(f"advisor output failed validation after repair: {template_id}")


class MissingTemplateRequirement(RuntimeError):
    """Raised when a template declares no response schema to validate against.

    A role may never act on an unvalidated reply, so a template with no declared
    response schema (the *template requirement*) is rejected before any model
    call \u2014 the anti-hallucination guard that applies to every AI-facing role,
    experts included (\u00a76D, \u00a77).
    """

    def __init__(self, template_id: str) -> None:
        self.template_id = template_id
        super().__init__(f"template declares no response schema requirement: {template_id}")


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


def _validate(
    text: str, schema: type[T], post_validate: PostValidator | None
) -> tuple[T | None, list[str]]:
    """Strict parse + optional semantic check.

    Returns ``(value, [])`` when the reply both parses into ``schema`` and
    passes ``post_validate``; otherwise ``(None, problems)`` where ``problems``
    names the semantic failures (empty for a pure structural failure).
    """
    parsed = _try_parse(text, schema)
    if parsed is None:
        return None, []
    if post_validate is not None:
        problems = post_validate(parsed)
        if problems:
            return None, problems
    return parsed, []


def _tokens(resp: CompletionResponse) -> int | None:
    usage = resp.raw.get("usage") if isinstance(resp.raw, dict) else None
    if isinstance(usage, dict):
        value = usage.get("total_tokens")
        if isinstance(value, int):
            return value
    return None


def _repair_instruction(template: Template, problems: list[str] | None = None) -> str:
    """The single corrective nudge sent on a failed parse or semantic check."""
    if problems:
        joined = "; ".join(problems)
        return (
            f"Your previous response had these problems: {joined}. Reply again "
            "with a SINGLE JSON object that exactly matches the required schema "
            f"for {template.id}. Only cite sources you are certain exist. "
            "JSON only — no prose, no code fences."
        )
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
    # Cited-URL existence check (anti-hallucination, §7.1). Injectable so tests
    # run offline; defaults to the real, SSRF-guarded HTTP verifier.
    verify_url: UrlVerifier = http_url_exists
    # Whether to run that check at all. Defaults from the `verify_citation_urls`
    # policy knob (default on) so it can be disabled in config where our
    # deterministic fetch is blocked by anti-crawler defenses (§7.1).
    verify_citations: bool = field(default_factory=lambda: get_policies().verify_citation_urls)

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
        append: bool = False,
        context: str = "",
        request_id: int,
        job_id: int | None = None,
    ) -> Analysis:
        """Authoritative validation + classification (template ``analyzer.analyze``).

        Internal identifiers (request id/code, job id) are **never** rendered into
        the prompt — the model classifies on the user's content only. The ids are
        used solely for the deterministic envelope + the `ai_calls` audit row.

        ``context`` is an optional **id-free** summary of the prior turn (the
        conversation so far); when present the model can judge ``belongs`` and
        resolve back-references against it (§6C).
        """
        card = _format_card(text=text, title=title, append=append, context=context)
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

    def make_plan(
        self,
        *,
        goal: str,
        context: str = "",
        request_id: int,
        job_id: int | None = None,
    ) -> PlanSpec:
        """Draft a complex job's plan (template ``analyzer.plan``, §6B / T6.1).

        No deterministic fallback: a malformed plan **escalates** rather than
        running unvalidated phases/tasks (AI never drives the control path).

        ``context`` carries the id-free prior-turn summary so a plan that refers
        to earlier info (e.g. "the URL from before") can ground it (§6C).
        """
        return self._run(
            role="planner",
            template_name="analyzer.plan",
            variables={"goal": goal, "context": context},
            schema=PlanSpec,
            request_id=request_id,
            job_id=job_id,
            fallback=None,
        )

    def next_action(
        self,
        *,
        goal: str,
        catalog: str = "",
        progress: str = "",
        request_id: int,
        job_id: int | None = None,
    ) -> ProposedAction:
        """Propose the next skill call for a task (template ``worker.next_action``).

        No deterministic fallback: an unvalidatable proposal **escalates** rather
        than running an action the runtime can't trust (§8.4).
        """
        return self._run(
            role="planner",
            template_name="worker.next_action",
            variables={"goal": goal, "catalog": catalog, "progress": progress},
            schema=ProposedAction,
            request_id=request_id,
            job_id=job_id,
            fallback=None,
        )

    def generate_skill(
        self,
        *,
        goal: str,
        request_id: int,
        job_id: int | None = None,
    ) -> GeneratedSkill:
        """Generate a feature job's reusable skill code (template ``coder.generate``).

        No deterministic fallback: an unvalidatable proposal **escalates** rather
        than writing code that doesn't match the contract. The returned code is
        written **inert** and gated on confirmation before activation (§5/§6B).
        """
        return self._run(
            role="planner",
            template_name="coder.generate",
            variables={"goal": goal},
            schema=GeneratedSkill,
            request_id=request_id,
            job_id=job_id,
            fallback=None,
        )

    def review(
        self,
        *,
        subject: str,
        context: str = "",
        request_id: int,
        job_id: int | None = None,
    ) -> Verdict:
        """Company Expert sign-off (template ``expert.review``, §6B / T6.3).

        Falls back to a deterministic **decline** with a note if the model
        output can't be validated — never auto-approves on a parse failure
        (failing safe keeps unvalidated work from being signed off).
        """
        return self._run(
            role="planner",
            template_name="expert.review",
            variables={"subject": subject, "context": context},
            schema=Verdict,
            request_id=request_id,
            job_id=job_id,
            fallback=lambda: Verdict(
                decision="decline",
                comments=["fallback: review output could not be validated"],
            ),
        )

    def answer(
        self,
        *,
        text: str,
        hits: list[dict] | None = None,
        context: str = "",
        request_id: int,
        job_id: int | None = None,
    ) -> AnswerDraft:
        """Draft an answer (template ``junior.answer``).

        **Validation is non-fatal here** (owner policy): the worker always returns
        the model's templated answer. The citation/URL checks run and are
        **logged as annotations**, never used to reject:

        * an answer with **no citation** (the model answered from its own
          knowledge, or honestly couldn't find a source) is returned, with a log
          note that it is *ungrounded*;
        * a cited **URL that can't be verified** (§7.1) is returned, with a log
          note — useful while web access/verification is still limited.

        Only a genuine **schema failure** — the model produces no valid JSON even
        after one repair — escalates (``AdvisorValidationError``); the Junior
        Worker degrades that to an honest "couldn't answer" rather than crashing.
        """
        draft = self._run(
            role="drafter",
            template_name="junior.answer",
            variables={
                "text": text,
                "hits": json.dumps(hits or [], ensure_ascii=False),
                "context": context,
            },
            schema=AnswerDraft,
            request_id=request_id,
            job_id=job_id,
            fallback=None,
        )
        self._annotate_answer_validation(draft)
        return draft

    def _annotate_answer_validation(self, draft: AnswerDraft) -> None:
        """Log citation/URL validation as a **non-fatal** annotation (never raises).

        This is the "add the validation in the response but do not fail the
        request" policy: we still surface whether the answer is grounded + whether
        its URLs resolve, but the answer is returned regardless.
        """
        if not draft.citations:
            logger.warning(
                "answer is ungrounded: the model cited no source "
                "(answered from model knowledge or could not find one)"
            )
            return
        for problem in self._verify_answer_citations(draft):
            logger.warning("answer citation check: %s", problem)

    def _verify_answer_citations(self, draft: AnswerDraft) -> list[str]:
        """Return any cited URL that doesn't exist (anti-hallucination, §7.1).

        Skipped entirely when ``verify_citations`` is off (config knob
        ``verify_citation_urls``) — cited URLs are then kept as provenance but
        not fetched, the documented escape hatch for anti-crawler false negatives.
        The result is **logged, not raised** (see `_annotate_answer_validation`).
        """
        if not self.verify_citations:
            return []
        return [
            f"cited URL could not be verified to exist: {url}"
            for url in unresolved_citation_urls(draft.citations, self.verify_url)
        ]

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
        post_validate: PostValidator | None = None,
    ) -> T:
        template = load_template(template_name, templates_dir=self.templates_dir)
        # A role may never act on an unvalidated reply: refuse a template that
        # declares no response-schema requirement (anti-hallucination, §6D/§7).
        if not template.schema:
            raise MissingTemplateRequirement(template.id)
        # Redact at the wrapper boundary too (defense in depth over the provider).
        prompt = redact_text(template.render(**variables))
        provider = self.resolve_provider(role)
        logger.info(
            "advisor call: role=%s template=%s model=%s request_id=%s job_id=%s",
            role,
            template.id,
            provider.model,
            request_id,
            job_id,
        )
        # The exact prompt we send the model (already redacted), at DEBUG so it
        # always lands in the run-log file and streams to the console in --debug
        # mode. This is the "request sent to the model"; pair it with the matching
        # "advisor response" line below to read the full exchange. (The DB only
        # keeps a sha256 ref of the prompt, never its raw text — §12.)
        logger.debug(
            "advisor request: role=%s template=%s request_id=%s prompt=%s",
            role,
            template.id,
            request_id,
            prompt,
        )

        started = time.monotonic()
        value, status, last_response, tokens = self._call_with_repair(
            provider, prompt, schema, template, post_validate
        )
        latency_ms = int((time.monotonic() - started) * 1000)

        if value is None and fallback is not None:
            value, status = fallback(), "fallback"
        elif value is None:
            status = "failed"

        logger.info(
            "advisor result: role=%s template=%s status=%s tokens=%s latency_ms=%s",
            role,
            template.id,
            status,
            tokens,
            latency_ms,
        )
        # Always log the model's reply (redacted + truncated) so every call is
        # auditable in the run logs — you can see exactly what the model said and,
        # on a non-valid reply, why validation rejected it. The text is run
        # through the redaction guard first (model output shouldn't hold our
        # secrets, but be safe anyway, §12).
        logger.info(
            "advisor response: role=%s template=%s status=%s response=%s",
            role,
            template.id,
            status,
            redact_text(last_response or "")[:2000],
        )

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
        post_validate: PostValidator | None = None,
    ) -> tuple[T | None, str, str, int | None]:
        """One call + at most one repair. Returns (value|None, status, text, tokens).

        A reply must pass **both** the strict schema parse and the optional
        ``post_validate`` semantic check (e.g. cited-URL existence) to count as
        valid; otherwise the corrective instruction names the problems found.
        """
        messages = [{"role": "user", "content": prompt}]
        resp = provider.complete(CompletionRequest(messages=messages, temperature=0.0))
        parsed, problems = _validate(resp.text, schema, post_validate)
        if parsed is not None:
            return parsed, "valid", resp.text, _tokens(resp)

        # Bounded repair: one more attempt with a corrective instruction.
        repair_messages = messages + [
            {"role": "assistant", "content": resp.text},
            {"role": "user", "content": _repair_instruction(template, problems)},
        ]
        resp2 = provider.complete(CompletionRequest(messages=repair_messages, temperature=0.0))
        parsed2, _ = _validate(resp2.text, schema, post_validate)
        if parsed2 is not None:
            return parsed2, "repaired", resp2.text, _tokens(resp2)

        return None, "invalid", resp2.text, _tokens(resp2)


def _format_card(*, text: str, title: str, append: bool, context: str = "") -> str:
    """Render a minimal RequestCard block for the analyzer prompt (§6D).

    Deliberately **omits internal identifiers** (request id/code, job id): the
    model never needs them to classify, and they must not leave the machine in a
    prompt. Only the user-authored title/text + the append flag are rendered, plus
    an optional id-free conversation-context block (the prior turn).
    """
    lines = ["Request card:"]
    if title:
        lines.append(f"- title: {title}")
    lines.append(f"- appended detail: {'yes' if append else 'no'}")
    lines.append(f"- text: {text}")
    if context:
        lines.append("")
        lines.append(context)
    return "\n".join(lines)
