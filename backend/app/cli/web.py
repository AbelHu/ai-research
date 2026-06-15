"""Run the web dashboard (design-spec §11; implementation-plan T10.1).

Run from the ``backend/`` directory:

    python -m app.cli.web                 # serve on http://127.0.0.1:8000 until Ctrl+C
    python -m app.cli.web --port 9000     # choose a port
    python -m app.cli.web --host 0.0.0.0  # listen on all interfaces (see the note)

Serves the read-only dashboard + JSON API over the stdlib (`wsgiref`) — no extra
dependency. It blocks until **Ctrl+C**. Open ``http://127.0.0.1:8000`` in a local
browser, or hit the JSON API under ``/api/`` (health at ``/healthz``).

> **No authentication yet** — intended for **local** use (it binds to
> ``127.0.0.1`` by default). Deploying to a cloud VM with public access means
> putting auth (and TLS) in front of this same app; ``--host 0.0.0.0`` exposes it
> on the network, so only use it behind a trusted boundary.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from wsgiref.simple_server import make_server

from app.config.settings import REPO_ROOT
from app.storage.db import connect
from app.storage.migrations import migrate
from app.web.app import create_app

DEFAULT_DB_NAME = "app.db"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli.web",
        description="Serve the read-only assistant dashboard (local, no auth).",
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"bind host (default: {DEFAULT_HOST})")
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help=f"bind port (default: {DEFAULT_PORT})"
    )
    parser.add_argument(
        "--db", type=Path, default=None, help="database file (default: data/app.db)"
    )
    args = parser.parse_args(argv)

    db_path = args.db or (REPO_ROOT / "data" / DEFAULT_DB_NAME)
    conn = connect(db_path)
    try:
        migrate(conn)
        app = create_app(conn)
        with make_server(args.host, args.port, app) as httpd:
            print(f"[ok]   Dashboard on http://{args.host}:{args.port}  (Ctrl+C to stop)")
            try:
                httpd.serve_forever()
            except KeyboardInterrupt:
                print("\n[ok]   Stopped.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
