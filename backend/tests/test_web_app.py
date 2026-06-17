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
from app.storage.repos import ai_calls as ai_calls_repo
from app.storage.repos import api_usage as api_usage_repo
from app.storage.repos import identities as identities_repo
from app.storage.repos import job_queue as job_queue_repo
from app.storage.repos import memories as memories_repo
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
    path_info, _, query = path.partition("?")
    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path_info,
        "QUERY_STRING": query,
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
    req = requests_repo.create_request(conn, title="usage seed")
    job = requests_repo.create_job(conn, request_id=req.id, kind="task", complexity="complex")
    job_queue_repo.enqueue(conn, job.id)
    ai_calls_repo.record_ai_call(
        conn,
        request_id=req.id,
        model_id="gpt-4o",
        tokens=123,
        latency_ms=900,
        validation_status="valid",
    )
    api_usage_repo.increment(conn, "tavily", amount=2)
    app = create_app(conn)
    code, headers, body = _call(app, "GET", "/")
    assert code == 200
    assert "text/html" in headers["Content-Type"]
    text = body.decode("utf-8")
    assert "<title>Assistant dashboard</title>" in text
    assert "hello world" in text  # the request shows in the table
    assert "Tavily credits used today: 2" in text
    assert "Tavily credits total (all time): 2" in text
    assert "Jobs in queue: 1" in text
    # Homepage links to dedicated detail pages.
    assert "href='/usage'" in text
    assert "href='/memories'" in text
    assert "href='/requests'" in text


def test_requests_page_shows_job_queue_attempts_and_error(conn) -> None:
    req = requests_repo.create_request(conn, title="queue demo")
    job = requests_repo.create_job(conn, request_id=req.id, kind="task", complexity="complex")
    job_queue_repo.enqueue(conn, job.id)
    job_queue_repo.claim_next(conn)
    job_queue_repo.requeue_pending(conn, job.id, "request timed out")
    app = create_app(conn)
    _, _, body = _call(app, "GET", "/requests")
    text = body.decode("utf-8")
    assert "Job Queue" in text
    assert "Attempts" in text
    assert "request timed out" in text
    assert "queue demo" in text


def test_index_auto_refreshes(conn) -> None:
    app = create_app(conn)
    _, _, body = _call(app, "GET", "/")
    text = body.decode("utf-8")
    # A dependency-free meta refresh keeps the dashboard data live.
    assert '<meta http-equiv="refresh"' in text
    assert "Auto-refreshes" in text


def test_index_shows_memories(conn) -> None:
    memories_repo.create_memory(
        conn,
        content="user prefers compact cards in dashboard",
        summary="dark mode preference",
        kind="preference",
        confidence=0.9,
        retention_class="short",
        source_ref="https://example.com/preferences",
    )
    app = create_app(conn)
    _, _, body = _call(app, "GET", "/")
    text = body.decode("utf-8")
    assert "href='/memories'" in text


def test_memories_page_shows_memory_details(conn) -> None:
    memories_repo.create_memory(
        conn,
        content="user prefers compact cards in dashboard",
        summary="dark mode preference",
        kind="preference",
        confidence=0.9,
        retention_class="short",
        source_ref="https://example.com/preferences",
    )
    app = create_app(conn)
    _, _, body = _call(app, "GET", "/memories")
    text = body.decode("utf-8")
    assert "<h1>Memories" in text
    assert "dark mode preference" in text
    assert "user prefers compact cards in dashboard" in text
    assert "https://example.com/preferences" in text
    assert "/api/memories" in text


def test_usage_page_shows_model_token_table(conn) -> None:
    req = requests_repo.create_request(conn, title="usage detail")
    ai_calls_repo.record_ai_call(
        conn,
        request_id=req.id,
        model_id="gpt-4o",
        tokens=123,
        latency_ms=900,
        validation_status="valid",
    )
    app = create_app(conn)
    _, _, body = _call(app, "GET", "/usage")
    text = body.decode("utf-8")
    assert "AI Model Token Usage" in text
    assert "gpt-4o" in text
    assert "123" in text


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
    api_usage_repo.increment(conn, "tavily", amount=1)
    req = requests_repo.create_request(conn, title="sys queue")
    job = requests_repo.create_job(conn, request_id=req.id, kind="task", complexity="complex")
    job_queue_repo.enqueue(conn, job.id)
    app = create_app(conn)
    code, data = _json(app, "GET", "/api/system")
    assert code == 200
    assert "metrics" in data and "usage" in data
    assert "queue" in data
    assert "cpu" in data["metrics"] and "disk" in data["metrics"]
    assert "web_search_credits_used_today" in data["usage"]
    assert "web_search_credits_total" in data["usage"]
    assert data["queue"]["total_jobs"] == 1


def test_api_usage_with_bucket_and_range(conn) -> None:
    req = requests_repo.create_request(conn, title="usage api")
    first = ai_calls_repo.record_ai_call(
        conn,
        request_id=req.id,
        model_id="gpt-4o",
        tokens=40,
    )
    second = ai_calls_repo.record_ai_call(
        conn,
        request_id=req.id,
        model_id="gpt-4o-mini",
        tokens=20,
    )
    with conn:
        conn.execute("UPDATE ai_calls SET created_at = ? WHERE id = ?", ("2026-06-01", first))
        conn.execute("UPDATE ai_calls SET created_at = ? WHERE id = ?", ("2026-06-02", second))
    api_usage_repo.increment(conn, "tavily", day="2026-06-01", amount=2)
    api_usage_repo.increment(conn, "tavily", day="2026-06-02", amount=1)

    app = create_app(conn)
    code, data = _json(app, "GET", "/api/usage?bucket=day&start=2026-06-01&end=2026-06-02")
    assert code == 200
    assert data["bucket"] == "day"
    assert data["range"] == {"start": "2026-06-01", "end": "2026-06-02"}
    assert data["totals"]["tokens"] == 60
    assert data["totals"]["tavily_credits"] == 3


def test_api_usage_rejects_invalid_bucket(conn) -> None:
    app = create_app(conn)
    code, data = _json(app, "GET", "/api/usage?bucket=hour")
    assert code == 400
    assert "invalid bucket" in data["error"]


def test_api_memories(conn) -> None:
    memories_repo.create_memory(
        conn, content="remember the gold price url", summary="gold price source", kind="fact"
    )
    app = create_app(conn)
    code, data = _json(app, "GET", "/api/memories")
    assert code == 200
    assert any(m["summary"] == "gold price source" for m in data)


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
