"""Run the assistant as a service: web dashboard + Telegram gateway (T10.1).

Run from the ``backend/`` directory:

    python -m app.cli.web                 # gateway + dashboard until Ctrl+C
    python -m app.cli.web --port 9000     # choose a dashboard port
    python -m app.cli.web --host 0.0.0.0  # listen on all interfaces (see the note)
    python -m app.cli.web --no-bot        # dashboard only (don't start the gateway)

This is the long-running **service** entry point. It does two things in one
process:

* runs the **Telegram gateway** in a background thread (long-poll → gateway →
  reply), so paired users are answered while the process is up, and
* serves the read-only **dashboard + JSON API** over the stdlib (`wsgiref`).

The gateway starts automatically when ``TELEGRAM_BOT_TOKEN`` is set (skip it with
``--no-bot``); without a token the dashboard still runs standalone. It blocks
until **Ctrl+C**. Open ``http://127.0.0.1:8000`` in a local browser, or hit the
JSON API under ``/api/`` (health at ``/healthz``).

> **No authentication yet** — intended for **local** use (it binds to
> ``127.0.0.1`` by default). Deploying to a cloud VM with public access means
> putting auth (and TLS) in front of this same app; ``--host 0.0.0.0`` exposes it
> on the network, so only use it behind a trusted boundary.
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
from pathlib import Path
from wsgiref.simple_server import make_server

from dotenv import load_dotenv

from app.advisor.providers import MissingCredentialError
from app.cli.telegram import build_bot, serve
from app.config.settings import REPO_ROOT, get_settings, load_models_config
from app.runlog import setup_run_logging
from app.storage.db import connect
from app.storage.migrations import migrate
from app.web.app import create_app

logger = logging.getLogger("app.cli.web")

DEFAULT_DB_NAME = "app.db"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000


def _bot_enabled(settings, *, no_bot: bool) -> bool:
    """Whether the background Telegram gateway should start.

    Off when ``--no-bot`` is given or no ``TELEGRAM_BOT_TOKEN`` is configured, so
    the dashboard still runs standalone on a machine without a bot.
    """
    if no_bot:
        return False
    token = settings.telegram_bot_token
    return bool(token is not None and token.reveal().strip())


def _run_gateway(db_path: Path) -> None:
    """Background-thread target: own SQLite connection + Telegram long-poll loop.

    A SQLite connection may not cross threads, so the gateway opens its **own**
    (the WAL database is shared safely with the web server's connection). Any
    failure is logged and ends the thread without taking down the web app.
    """
    conn = None
    try:
        settings = get_settings()
        models = load_models_config()
        conn = connect(db_path)
        migrate(conn)
        adapter, advisor = build_bot(conn, settings=settings, models=models)
        logger.info("background Telegram gateway started")
        serve(conn, adapter, advisor)
    except (MissingCredentialError, KeyError, FileNotFoundError) as exc:
        logger.error("Telegram gateway not started (config error): %s", exc)
    except Exception as exc:  # noqa: BLE001 - keep the web app alive regardless
        logger.error("Telegram gateway stopped: %s", exc)
    finally:
        if conn is not None:
            conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli.web",
        description="Run the assistant as a service: web dashboard + Telegram gateway.",
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"bind host (default: {DEFAULT_HOST})")
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help=f"bind port (default: {DEFAULT_PORT})"
    )
    parser.add_argument(
        "--db", type=Path, default=None, help="database file (default: data/app.db)"
    )
    parser.add_argument(
        "--no-bot",
        action="store_true",
        help="serve the dashboard only; don't start the Telegram gateway",
    )
    parser.add_argument(
        "-d", "--debug", action="store_true", help="stream logs (incl. model responses) to console"
    )
    args = parser.parse_args(argv)

    load_dotenv(REPO_ROOT / ".env", override=False)
    setup_run_logging("web", console_level=logging.DEBUG if args.debug else logging.INFO)

    db_path = args.db or (REPO_ROOT / "data" / DEFAULT_DB_NAME)
    conn = connect(db_path)
    try:
        # Migrate once up front so the gateway thread's own connection finds the
        # schema already in place (its migrate() is then an idempotent no-op).
        migrate(conn)

        settings = get_settings()
        if _bot_enabled(settings, no_bot=args.no_bot):
            threading.Thread(
                target=_run_gateway, args=(db_path,), name="telegram-gateway", daemon=True
            ).start()
            print("[ok]   Telegram gateway starting in the background.")
        elif args.no_bot:
            print("[ok]   --no-bot: serving the dashboard only.")
        else:
            print("[warn] TELEGRAM_BOT_TOKEN not set — dashboard only (no Telegram gateway).")

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
