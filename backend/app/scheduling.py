"""Interval scheduling helpers for the periodic scheduler (design-spec §9, §11).

The ``schedules.schedule_cron`` column holds a small, **dependency-free interval
spec** (full cron isn't needed for TTL maintenance / periodic digests and would
pull in a parser the offline, hash-pinned build avoids):

* ``@hourly`` / ``@daily`` / ``@weekly`` — common shortcuts.
* ``@every <N><unit>`` — ``s`` seconds, ``m`` minutes, ``h`` hours, ``d`` days
  (e.g. ``@every 6h``, ``@every 30m``).
* a bare ``<N><unit>`` (``6h``) or a bare integer number of seconds (``3600``).

:func:`next_run_after` computes the next due time from the last run.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

_SHORTCUTS = {"@hourly": 3600, "@daily": 86400, "@weekly": 604800}
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_EVERY = re.compile(r"^(\d+)\s*([smhd])$")

# Fallback when a spec is missing or unrecognized — keep periodic work moving.
DEFAULT_INTERVAL = timedelta(days=1)


def parse_interval(spec: str | None) -> timedelta | None:
    """Parse an interval spec into a `timedelta` (``None`` if empty/unrecognized)."""
    if not spec:
        return None
    text = spec.strip().lower()
    if text in _SHORTCUTS:
        return timedelta(seconds=_SHORTCUTS[text])
    if text.startswith("@every "):
        text = text[len("@every ") :].strip()
    if text.isdigit():
        return timedelta(seconds=int(text))
    match = _EVERY.match(text)
    if match:
        return timedelta(seconds=int(match.group(1)) * _UNIT_SECONDS[match.group(2)])
    return None


def next_run_after(
    spec: str | None,
    after: datetime,
    *,
    default: timedelta = DEFAULT_INTERVAL,
) -> datetime:
    """The next due time = ``after`` + the spec's interval (or ``default``)."""
    return after + (parse_interval(spec) or default)
