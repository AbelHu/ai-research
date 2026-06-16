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


def _render_index(conn: sqlite3.Connection) -> str:
    """A tiny server-rendered dashboard: system snapshot + the request list.

    The page **auto-refreshes** every ``_DASHBOARD_REFRESH_SECONDS`` via a
    ``<meta http-equiv="refresh">`` tag, so the data stays live without any
    client-side JavaScript (keeping the dashboard dependency-free).
    """
    usage = services.model_usage(conn)
    metrics = services.system_metrics()
    requests = services.request_overview(conn, limit=50)

    rows = "\n".join(
        f"<tr><td>{_escape(r['code'])}</td><td>{_escape(r['title'])}</td>"
        f"<td>{_escape(r['status'])}</td><td>{_escape(r['state'])}</td>"
        f"<td><a href='/api/requests/{r['id']}'>details</a></td></tr>"
        for r in requests
    )
    disk = metrics["disk"]
    cpu = metrics["cpu"]
    cpu_line = (
        f"<li>CPU load (1m): {_escape(cpu['load_1m'])} · cores: {_escape(cpu['cpu_count'])}</li>"
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta http-equiv="refresh" content="{_DASHBOARD_REFRESH_SECONDS}">
<title>Assistant dashboard</title>
<style>body{{font-family:system-ui,sans-serif;margin:2rem;max-width:60rem}}
table{{border-collapse:collapse;width:100%}}
td,th{{border:1px solid #ccc;padding:.3rem .5rem;text-align:left}}
.muted{{color:#666}}</style></head><body>
<h1>Assistant dashboard</h1>
<p class="muted">Local, read-only. Auto-refreshes every
{_DASHBOARD_REFRESH_SECONDS}s. JSON API under <code>/api/</code>;
health at <code>/healthz</code>.</p>
<h2>System</h2>
<ul>
<li>Model calls: {usage["total_calls"]} ({usage["total_tokens"]} tokens)</li>
{cpu_line}
<li>Disk: {_escape(disk["percent"])}% used</li>
</ul>
<h2>Requests ({len(requests)})</h2>
<table><tr><th>Code</th><th>Title</th><th>Status</th><th>State</th><th></th></tr>
{rows}
</table>
</body></html>"""


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

        # Browser view: a minimal HTML dashboard at the root.
        if method == "GET" and path == "/":
            return _html_response(start_response, 200, _render_index(conn))

        matched_path = False
        for route in _ROUTES:
            match = route.regex.match(path)
            if not match:
                continue
            matched_path = True
            if route.method != method:
                continue
            try:
                status_code, body = route.handler(conn, **match.groupdict())
            except Exception:  # noqa: BLE001 - never leak a traceback to the client
                return _json_response(start_response, 500, {"error": "internal error"})
            return _json_response(start_response, status_code, body)

        if matched_path:
            return _json_response(start_response, 405, {"error": "method not allowed"})
        return _json_response(start_response, 404, {"error": "not found", "path": path})

    return app
