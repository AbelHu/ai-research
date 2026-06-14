"""Secret redaction guard (design spec O16, section 12).

Scrubs secrets/passwords/keys from any text *before* it is sent to an AI
model provider. Redaction is applied only to the copy that leaves the machine;
stored data is never modified here.

Two entry points:
    redact_text(text)   -> text with secrets replaced by [REDACTED]
    find_secrets(text)  -> list of (label, matched_snippet) for detected secrets

`redact_messages` applies redaction to a list of chat messages.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.security import REDACTED  # re-exported: app.advisor.redaction.REDACTED still works


class SecretLeakError(RuntimeError):
    """Raised when text containing secrets would be sent to a model.

    Used where redaction-in-place is not safe (e.g. embedding inputs, where a
    partially-redacted string would produce a meaningless vector). The matched
    rule *labels* are reported, never the secret values themselves.
    """

    def __init__(self, labels: list[str]) -> None:
        self.labels = sorted(set(labels))
        super().__init__(
            "Refusing to send text containing secrets to a model "
            f"(matched rules: {', '.join(self.labels)})."
        )


@dataclass(frozen=True)
class _Rule:
    label: str
    pattern: re.Pattern[str]
    # When the pattern has a capturing group, only that group is replaced
    # (so we keep surrounding context like the key name). Otherwise the whole
    # match is replaced.
    group: int = 0


# Order matters: more specific rules first.
_RULES: list[_Rule] = [
    _Rule(
        "private_key",
        re.compile(
            r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----.*?"
            r"-----END (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----",
            re.DOTALL,
        ),
    ),
    _Rule("github_pat", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b")),
    _Rule("github_fine_grained_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{50,}\b")),
    _Rule("openai_key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    _Rule("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    _Rule("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    _Rule("slack_token", re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}\b")),
    _Rule("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    _Rule(
        "bearer_token",
        re.compile(r"(?i)\b(?:authorization\s*:\s*)?bearer\s+([A-Za-z0-9._~+/=-]{12,})"),
        group=1,
    ),
    # key=value / key: value assignments for sensitive names.
    _Rule(
        "secret_assignment",
        re.compile(
            r"(?i)\b(password|passwd|pwd|secret|api[_-]?key|access[_-]?token|"
            r"auth[_-]?token|client[_-]?secret|private[_-]?key|token)\b"
            r"\s*[:=]\s*[\"']?([^\s\"',;]{4,})[\"']?"
        ),
        group=2,
    ),
    # Credentials embedded in connection strings: proto://user:password@host
    _Rule(
        "connection_string_password",
        re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s:/@]+:([^\s:/@]+)@"),
        group=1,
    ),
]


def find_secrets(text: str) -> list[tuple[str, str]]:
    """Return a list of (label, matched_snippet) for any detected secrets."""
    if not text:
        return []
    found: list[tuple[str, str]] = []
    for rule in _RULES:
        for match in rule.pattern.finditer(text):
            snippet = match.group(rule.group) if rule.group else match.group(0)
            if snippet:
                found.append((rule.label, snippet))
    return found


def redact_text(text: str) -> str:
    """Replace any detected secrets in `text` with [REDACTED]."""
    if not text:
        return text
    result = text
    for rule in _RULES:

        def _replace(match: re.Match[str], rule: _Rule = rule) -> str:
            if rule.group:
                whole = match.group(0)
                secret = match.group(rule.group)
                return whole.replace(secret, REDACTED)
            return REDACTED

        result = rule.pattern.sub(_replace, result)
    return result


def redact_messages(messages: list[dict]) -> list[dict]:
    """Return a copy of chat messages with secrets redacted from content."""
    redacted: list[dict] = []
    for msg in messages:
        new_msg = dict(msg)
        content = new_msg.get("content")
        if isinstance(content, str):
            new_msg["content"] = redact_text(content)
        redacted.append(new_msg)
    return redacted


def ensure_no_secrets(texts: list[str]) -> None:
    """Strict guard: raise `SecretLeakError` if any text contains a secret.

    For payloads that must not be sent at all when a secret is present (e.g.
    embedding inputs), as opposed to `redact_text`, which scrubs in place.
    """
    labels: list[str] = []
    for text in texts:
        labels.extend(label for label, _ in find_secrets(text))
    if labels:
        raise SecretLeakError(labels)
