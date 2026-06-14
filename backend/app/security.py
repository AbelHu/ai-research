"""A type for values that must never be logged (design spec O16, §12).

`Secret` wraps a sensitive string - an API token, password, or connection
secret - so it cannot leak through logs, reprs, tracebacks, or accidental
string interpolation. The real value is reachable only through `reveal()`,
which is called at the exact boundary where the secret is required (for
example, when building an HTTP ``Authorization`` header).

Everywhere else the value renders as ``[REDACTED]``::

    >>> token = Secret("ghp_realtokenvalue")
    >>> str(token)
    '[REDACTED]'
    >>> f"using {token}"
    'using [REDACTED]'
    >>> token.reveal()
    'ghp_realtokenvalue'

The type also integrates with pydantic v2: a settings field declared as
``Secret | None`` coerces an env string into a `Secret` on load and redacts it
on serialization, so a dumped/logged settings object never exposes the value.
"""

from __future__ import annotations

from typing import Any

# The single canonical placeholder shown wherever a secret would otherwise
# appear. Imported (and re-exported) by app.advisor.redaction.
REDACTED = "[REDACTED]"


class Secret:
    """An opaque wrapper around a sensitive string value.

    The wrapped value is accessible only via `reveal()`. The instance is
    immutable and leak-proof: ``repr``/``str``/``format`` all yield
    ``[REDACTED]``.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if not isinstance(value, str):
            raise TypeError(f"Secret value must be a str, got {type(value).__name__}")
        # Bypass our own __setattr__ guard to set the one allowed slot.
        object.__setattr__(self, "_value", value)

    def reveal(self) -> str:
        """Return the underlying secret value. Call only at the point of use."""
        return self._value

    # --- leak-proofing ---------------------------------------------------
    def __repr__(self) -> str:
        return f"Secret({REDACTED})"

    def __str__(self) -> str:
        return REDACTED

    def __format__(self, _spec: str) -> str:
        return REDACTED

    # --- immutability ----------------------------------------------------
    def __setattr__(self, _name: str, _value: Any) -> None:
        raise AttributeError("Secret is immutable")

    def __delattr__(self, _name: str) -> None:
        raise AttributeError("Secret is immutable")

    # --- safe helpers (do not reveal the value) --------------------------
    def __bool__(self) -> bool:
        return bool(self._value)

    def __len__(self) -> int:
        return len(self._value)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Secret):
            return self._value == other._value
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self._value)

    # --- pydantic v2 integration ----------------------------------------
    @classmethod
    def __get_pydantic_core_schema__(cls, _source: Any, _handler: Any) -> Any:
        from pydantic_core import core_schema

        return core_schema.no_info_plain_validator_function(
            cls._coerce,
            serialization=core_schema.plain_serializer_function_ser_schema(
                lambda _secret: REDACTED,
                return_schema=core_schema.str_schema(),
                when_used="always",
            ),
        )

    @classmethod
    def _coerce(cls, value: Any) -> Secret:
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            return cls(value)
        raise TypeError(f"Secret must be created from a str, got {type(value).__name__}")
