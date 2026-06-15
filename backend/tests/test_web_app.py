"""Tests for the WSGI dashboard app (implementation-plan T10.1).

Offline: the WSGI app is a plain callable, so we drive it with a synthetic
``environ`` dict and capture the response **without opening a socket**. Covers
health, the JSON API routes, the HTML index, 404/405, and the revoke mutation.
"""

from __future__ import annotations

import io
import json

import pytest

from app.storage.db import connect
from app.storage.migrations import migrate
from app.storage.repos import identities as identities_repo
from app.storage.repos import requests as requests_repo
from app.web.app import create_app


@pytest.fixture
def conn():
    c = connect()
    migrate(c)
    try:
        yield c
    finally:
        c.close()


class _Capture:
    """A minimal WSGI ``start_response`` that records status + headers."""

    def __init__(self) -> None:
        self.status = ""
        self.headers: list[tuple[str, str]] = []

    def __call__(self, status: str, headers: list[tuple[str, str]]) -> None:
        self.status = status
        self.headers = headers


def _call(app, method: str, path: str) -> tuple[int, dict, bytes]:
    """Invoke the WSGI app; return (status_code, headers_dict, body_bytes)."""
    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "wsgi.input": io.BytesIO(b""),
        "wsgi.errors": io.StringIO(),
    }
    capture = _Capture()
    chunks = app(environ, capture)
    body = b"".join(chunks)
    code = int(capture.status.split()[0])
    headers = dict(capture.headers)
    return code, headers, body


def _json(app, method: str, path: str):
    code, headers, body = _call(app, method, path)
    assert "application/json" in headers["Content-Type"]
    return code, json.loads(body.decode("utf-8"))


# --- health + index ---------------------------------------------------------


def test_healthz(conn) -> None:
    app = create_app(conn)
    code, data = _json(app, "GET", "/healthz")
    assert code == 200
    assert data == {"status": "ok"}


def test_index_is_html(conn) -> None:
    requests_repo.create_request(conn, title="hello world")
    app = create_app(conn)
    code, headers, body = _call(app, "GET", "/")
    assert code == 200
    assert "text/html" in headers["Content-Type"]
    text = body.decode("utf-8")
    assert "<title>Assistant dashboard</title>" in text
    assert "hello world" in text  # the request shows in the table


def test_index_escapes_html(conn) -> None:
    requests_repo.create_request(conn, title="<script>alert(1)</script>")
    app = create_app(conn)
    _, _, body = _call(app, "GET", "/")
    text = body.decode("utf-8")
    assert "<script>alert(1)</script>" not in text
    assert "&lt;script&gt;" in text  # escaped


# --- JSON API ---------------------------------------------------------------


def test_api_requests_index(conn) -> None:
    requests_repo.create_request(conn, title="first")
    requests_repo.create_request(conn, title="second")
    app = create_app(conn)
    code, data = _json(app, "GET", "/api/requests")
    assert code == 200
    assert [r["title"] for r in data] == ["second", "first"]


def test_api_request_detail_and_404(conn) -> None:
    req = requests_repo.create_request(conn, title="detail me")
    app = create_app(conn)

    code, data = _json(app, "GET", f"/api/requests/{req.id}")
    assert code == 200
    assert data["request"]["id"] == req.id

    code, data = _json(app, "GET", "/api/requests/9999")
    assert code == 404
    assert "error" in data


def test_api_system(conn) -> None:
    app = create_app(conn)
    code, data = _json(app, "GET", "/api/system")
    assert code == 200
    assert "metrics" in data and "usage" in data
    assert "cpu" in data["metrics"] and "disk" in data["metrics"]


def test_api_accounts_and_revoke(conn) -> None:
    owner_id = identities_repo.ensure_owner(conn)
    identities_repo.bind_identity(
        conn, user_id=owner_id, channel="telegram", channel_user_id="42", paired_via="host_code"
    )
    app = create_app(conn)

    code, data = _json(app, "GET", "/api/accounts")
    assert code == 200
    assert data[0]["channel_user_id"] == "42"

    # Revoke via POST.
    code, data = _json(app, "POST", "/api/accounts/telegram/42/revoke")
    assert code == 200
    assert data["revoked"] is True
    assert identities_repo.get_identity(conn, "telegram", "42").state == "revoked"

    # Revoking again → 404 (nothing left to revoke).
    code, data = _json(app, "POST", "/api/accounts/telegram/42/revoke")
    assert code == 404


# --- routing edge cases -----------------------------------------------------


def test_unknown_path_is_404(conn) -> None:
    app = create_app(conn)
    code, data = _json(app, "GET", "/api/nope")
    assert code == 404


def test_method_not_allowed_is_405(conn) -> None:
    app = create_app(conn)
    # /api/requests exists for GET, not POST.
    code, data = _json(app, "POST", "/api/requests")
    assert code == 405
