"""WSGI dashboard app (design-spec §11; implementation-plan T10.1).

A **dependency-free** HTTP layer over the read-only services in
`app.web.services`, built on the stdlib (`wsgiref`). It exposes:

* ``GET  /healthz``                     — liveness probe (T10.1)
* ``GET  /``                            — a minimal HTML dashboard (browser view)
* ``GET  /api/requests``                — request index (T10.2)
* ``GET  /api/requests/<id>``           — one request's job→plan→…→ai_calls tree
* ``GET  /api/system``                  — host metrics + model usage (T10.3)
* ``GET  /api/accounts``                — paired-account allowlist (T10.5)
* ``POST /api/accounts/<channel>/<user>/revoke`` — revoke an account (T10.5)

The app is a plain WSGI callable, so it's **fully unit-testable without a
socket** (call it with an `environ` dict). The CLI (`app.cli.web`) serves it with
`wsgiref.simple_server` until Ctrl+C.

> **No authentication yet** (local use). When this is deployed to a cloud VM,
> auth wraps this same WSGI app as middleware — the routes/services don't change.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Iterable
from urllib.parse import parse_qs

from app.web import services

# A route handler: (conn, **path params) -> (status_code, json-serializable body).
Handler = Callable[..., "tuple[int, object]"]


class _Route:
    def __init__(self, method: str, pattern: str, handler: Handler) -> None:
        self.method = method
        # Path params are ``{name}`` segments matched non-greedily up to the next '/'.
        regex = re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern)
        self.regex = re.compile(f"^{regex}$")
        self.handler = handler


# --- JSON handlers ----------------------------------------------------------


def _requests_index(conn: sqlite3.Connection) -> tuple[int, object]:
    return 200, services.request_overview(conn)


def _request_detail(conn: sqlite3.Connection, request_id: str) -> tuple[int, object]:
    tree = services.request_tree(conn, int(request_id))
    if tree is None:
        return 404, {"error": "request not found", "request_id": request_id}
    return 200, tree


def _system(conn: sqlite3.Connection) -> tuple[int, object]:
    return 200, {"metrics": services.system_metrics(), "usage": services.model_usage(conn)}


def _usage(
    conn: sqlite3.Connection,
    *,
    bucket: str = "day",
    days: int | None = None,
    start: str | None = None,
    end: str | None = None,
) -> tuple[int, object]:
    try:
        payload = services.usage_aggregate(
            conn,
            bucket=bucket,
            days=days,
            start=start,
            end=end,
        )
        return 200, payload
    except ValueError as exc:
        return 400, {"error": str(exc)}


def _memories(conn: sqlite3.Connection) -> tuple[int, object]:
    return 200, services.memories_overview(conn)


def _accounts(conn: sqlite3.Connection) -> tuple[int, object]:
    return 200, services.list_paired_accounts(conn)


def _revoke_account(
    conn: sqlite3.Connection, channel: str, channel_user_id: str
) -> tuple[int, object]:
    revoked = services.revoke_account(conn, channel, channel_user_id)
    if not revoked:
        return 404, {
            "error": "no paired account to revoke",
            "target": f"{channel}:{channel_user_id}",
        }
    return 200, {"revoked": True, "target": f"{channel}:{channel_user_id}"}


def _healthz(conn: sqlite3.Connection) -> tuple[int, object]:
    return 200, {"status": "ok"}


_ROUTES: list[_Route] = [
    _Route("GET", "/healthz", _healthz),
    _Route("GET", "/api/requests", _requests_index),
    _Route("GET", "/api/requests/{request_id}", _request_detail),
    _Route("GET", "/api/system", _system),
    _Route("GET", "/api/usage", _usage),
    _Route("GET", "/api/memories", _memories),
    _Route("GET", "/api/accounts", _accounts),
    _Route("POST", "/api/accounts/{channel}/{channel_user_id}/revoke", _revoke_account),
]


# --- minimal HTML dashboard (browser view) ----------------------------------

# How often the browser dashboard reloads itself to show fresh data (seconds).
_DASHBOARD_REFRESH_SECONDS = 10


def _escape(text: object) -> str:
    return (
        str("" if text is None else text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _page_shell(*, title: str, body: str) -> str:
    """Common HTML shell for all dashboard pages."""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta http-equiv="refresh" content="{_DASHBOARD_REFRESH_SECONDS}">
<title>{_escape(title)}</title>
<style>body{{font-family:system-ui,sans-serif;margin:2rem;max-width:72rem}}
table{{border-collapse:collapse;width:100%}}
td,th{{border:1px solid #ccc;padding:.3rem .5rem;text-align:left;vertical-align:top}}
.muted{{color:#666}}
nav a{{margin-right:.8rem}}
code{{background:#f6f6f6;padding:.1rem .25rem;border-radius:.2rem}}</style></head><body>
{body}
</body></html>"""


def _nav() -> str:
    return (
        "<nav>"
        "<a href='/'>Home</a>"
        "<a href='/requests'>Requests</a>"
        "<a href='/memories'>Memories</a>"
        "<a href='/usage'>Usage</a>"
        "<a href='/accounts'>Accounts</a>"
        "</nav>"
    )


def _render_home(conn: sqlite3.Connection) -> str:
    """Homepage with quick health/usage snapshot and links to detail pages.

    The page **auto-refreshes** every ``_DASHBOARD_REFRESH_SECONDS`` via a
    ``<meta http-equiv="refresh">`` tag, so the data stays live without any
    client-side JavaScript (keeping the dashboard dependency-free).
    """
    usage = services.model_usage(conn)
    metrics = services.system_metrics()
    requests = services.request_overview(conn, limit=5)
    disk = metrics["disk"]
    cpu = metrics["cpu"]
    cpu_line = (
        f"<li>CPU load (1m): {_escape(cpu['load_1m'])} · cores: {_escape(cpu['cpu_count'])}</li>"
    )
    recent = "".join(
        f"<li>{_escape(r['title'])} "
        f"(<a href='/api/requests/{r['id']}'>API detail</a>)</li>"
        for r in requests
    )
    body = f"""
<h1>Assistant dashboard</h1>
{_nav()}
<p class="muted">Local, read-only. Auto-refreshes every {_DASHBOARD_REFRESH_SECONDS}s.</p>
<h2>System</h2>
<ul>
<li>Model calls: {usage["total_calls"]} ({usage["total_tokens"]} tokens)</li>
<li>Tavily credits used today: {_escape(usage['web_search_credits_used_today'])}</li>
<li>Tavily credits total (all time): {_escape(usage['web_search_credits_total'])}</li>
{cpu_line}
<li>Disk: {_escape(disk["percent"])}% used</li>
</ul>
<h2>Pages</h2>
<ul>
<li><a href='/requests'>Requests</a> — request list and API detail links</li>
<li><a href='/memories'>Memories</a> — detailed memory table</li>
<li><a href='/usage'>Usage</a> — model tokens and Tavily credits</li>
<li><a href='/accounts'>Accounts</a> — paired/revoked identities</li>
</ul>
<h2>Recent Requests</h2>
<ul>{recent or "<li class='muted'>No requests yet.</li>"}</ul>
<p class="muted">JSON API root: <code>/api/</code> · health: <code>/healthz</code></p>
"""
    return _page_shell(title="Assistant dashboard", body=body)


def _render_requests_page(conn: sqlite3.Connection) -> str:
    requests = services.request_overview(conn, limit=100)
    rows = "\n".join(
        f"<tr><td>{_escape(r['code'])}</td><td>{_escape(r['title'])}</td>"
        f"<td>{_escape(r['status'])}</td><td>{_escape(r['state'])}</td>"
        f"<td><a href='/api/requests/{r['id']}'>details</a></td></tr>"
        for r in requests
    )
    body = f"""
<h1>Requests ({len(requests)})</h1>
{_nav()}
<table><tr><th>Code</th><th>Title</th><th>Status</th><th>State</th><th></th></tr>
{rows}
</table>
"""
    return _page_shell(title="Requests", body=body)


def _render_memories_page(conn: sqlite3.Connection) -> str:
    memories = services.memories_overview(conn, limit=200)
    rows = "\n".join(
        f"<tr><td>{_escape(m['kind'])}</td>"
        f"<td>{_escape(m['summary'] or '—')}</td>"
        f"<td>{_escape(m['preview'])}</td>"
        f"<td>{_escape(m['importance'])}</td><td>{_escape(m['confidence'])}</td>"
        f"<td>{_escape(m['use_count'])}</td>"
        f"<td>{_escape(m['retention_class'])}</td>"
        f"<td>{_escape(m['source_ref'])}</td>"
        f"<td>{_escape(m['last_used_at'])}</td>"
        f"<td>{_escape(m['expires_at'])}</td></tr>"
        for m in memories
    )
    body = f"""
<h1>Memories ({len(memories)})</h1>
{_nav()}
<p class='muted'>JSON source: <a href='/api/memories'>/api/memories</a>.</p>
<table><tr><th>Kind</th><th>Summary</th><th>Preview</th><th>Importance</th><th>Confidence</th><th>Uses</th><th>Retention</th><th>Source</th><th>Last used</th><th>Expires</th></tr>
{rows}
</table>
"""
    return _page_shell(title="Memories", body=body)


def _render_usage_page(conn: sqlite3.Connection) -> str:
    usage = services.model_usage(conn)
    monthly = services.usage_aggregate(conn, bucket="month", days=365)
    model_rows = "\n".join(
        f"<tr><td>{_escape(m['model_id'])}</td><td>{_escape(m['calls'])}</td>"
        f"<td>{_escape(m['tokens'])}</td><td>{_escape(m['avg_latency_ms'])}</td></tr>"
        for m in usage["by_model"]
    )
    if not model_rows:
        model_rows = "<tr><td colspan='4' class='muted'>No AI calls yet.</td></tr>"
    bucket_rows = "\n".join(
        f"<tr><td>{_escape(b['bucket'])}</td><td>{_escape(b['credits'])}</td></tr>"
        for b in monthly["credits_by_bucket"]
    )
    if not bucket_rows:
        bucket_rows = "<tr><td colspan='2' class='muted'>No Tavily usage in range.</td></tr>"
    body = f"""
<h1>Usage</h1>
{_nav()}
<ul>
<li>Model calls: {usage['total_calls']} ({usage['total_tokens']} tokens)</li>
<li>Tavily credits used today: {usage['web_search_credits_used_today']}</li>
<li>Tavily credits total (all time): {usage['web_search_credits_total']}</li>
</ul>
<h2>AI Model Token Usage</h2>
<table><tr><th>Model</th><th>Calls</th><th>Tokens</th><th>Avg latency (ms)</th></tr>
{model_rows}
</table>
<h2>Tavily Credits by Month</h2>
<table><tr><th>Month</th><th>Credits</th></tr>
{bucket_rows}
</table>
<p class="muted">Aggregated usage API: <code>/api/usage?bucket=day|week|month&amp;days=30</code>
or <code>/api/usage?bucket=day&amp;start=YYYY-MM-DD&amp;end=YYYY-MM-DD</code>.</p>
"""
    return _page_shell(title="Usage", body=body)


def _render_accounts_page(conn: sqlite3.Connection) -> str:
    accounts = services.list_paired_accounts(conn)
    rows = "\n".join(
        f"<tr><td>{_escape(a['channel'])}</td><td>{_escape(a['channel_user_id'])}</td>"
        f"<td>{_escape(a['state'])}</td><td>{_escape(a['paired_via'])}</td>"
        f"<td>{_escape(a['paired_at'])}</td></tr>"
        for a in accounts
    )
    if not rows:
        rows = "<tr><td colspan='5' class='muted'>No paired accounts yet.</td></tr>"
    body = f"""
<h1>Accounts</h1>
{_nav()}
<p class='muted'>JSON source: <a href='/api/accounts'>/api/accounts</a>.</p>
<table><tr><th>Channel</th><th>User</th><th>State</th><th>Paired via</th><th>Paired at</th></tr>
{rows}
</table>
"""
    return _page_shell(title="Accounts", body=body)


_HTML_ROUTES: dict[str, Callable[[sqlite3.Connection], str]] = {
    "/": _render_home,
    "/requests": _render_requests_page,
    "/memories": _render_memories_page,
    "/usage": _render_usage_page,
    "/accounts": _render_accounts_page,
}


# --- WSGI application -------------------------------------------------------


def _json_response(start_response: Callable, status_code: int, body: object) -> list[bytes]:
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    status = f"{status_code} {_STATUS_TEXT.get(status_code, 'OK')}"
    start_response(status, [("Content-Type", "application/json; charset=utf-8")])
    return [payload]


def _html_response(start_response: Callable, status_code: int, html: str) -> list[bytes]:
    payload = html.encode("utf-8")
    status = f"{status_code} {_STATUS_TEXT.get(status_code, 'OK')}"
    start_response(status, [("Content-Type", "text/html; charset=utf-8")])
    return [payload]


_STATUS_TEXT = {
    200: "OK",
    404: "Not Found",
    405: "Method Not Allowed",
    500: "Internal Server Error",
}


def create_app(conn: sqlite3.Connection) -> Callable[[dict, Callable], Iterable[bytes]]:
    """Build the WSGI app over a single SQLite connection (single-threaded server).

    The connection is shared across requests; `wsgiref.simple_server` handles one
    request at a time in the serving thread, so this is safe for the local
    single-user dashboard.
    """

    def app(environ: dict, start_response: Callable) -> Iterable[bytes]:
        method = environ.get("REQUEST_METHOD", "GET").upper()
        path = environ.get("PATH_INFO", "/") or "/"
        query = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=False)

        # Browser view: HTML pages.
        if method == "GET" and path in _HTML_ROUTES:
            return _html_response(start_response, 200, _HTML_ROUTES[path](conn))

        matched_path = False
        for route in _ROUTES:
            match = route.regex.match(path)
            if not match:
                continue
            matched_path = True
            if route.method != method:
                continue
            try:
                kwargs = match.groupdict()
                if route.regex.pattern == "^/api/usage$":
                    bucket = (query.get("bucket") or ["day"])[0]
                    days_raw = (query.get("days") or [None])[0]
                    start = (query.get("start") or [None])[0]
                    end = (query.get("end") or [None])[0]
                    days = int(days_raw) if days_raw else None
                    kwargs = {
                        "bucket": bucket,
                        "days": days,
                        "start": start,
                        "end": end,
                    }
                status_code, body = route.handler(conn, **kwargs)
            except Exception:  # noqa: BLE001 - never leak a traceback to the client
                return _json_response(start_response, 500, {"error": "internal error"})
            return _json_response(start_response, status_code, body)

        if matched_path:
            return _json_response(start_response, 405, {"error": "method not allowed"})
        return _json_response(start_response, 404, {"error": "not found", "path": path})

    return app
