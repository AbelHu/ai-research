"""Run the Telegram bot via long-poll (design-spec §10; implementation-plan T8.3).

Run from the ``backend/`` directory after setting ``TELEGRAM_BOT_TOKEN`` (and
logging in / configuring a model):

    python -m app.cli.telegram             # start the long-poll loop
    python -m app.cli.telegram --once      # drain pending updates once and exit

Each inbound message goes through the gateway: ``/pair <code>`` binds a chat
account to the owner; a paired sender's message is answered end-to-end by the
control loop; an unpaired sender is refused (and told how to pair). Only the
paired owner can drive the system (§10.1).

This is the live, opt-in runner (network). The adapter + ingress are unit-tested
offline; this module wires them to a real bot token + DB.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

from app.advisor.providers import MissingCredentialError
from app.advisor.wrapper import Advisor
from app.channels.telegram import TelegramAdapter, TelegramError
from app.cli.ask import build_resolver
from app.config.policies import get_policies
from app.config.settings import REPO_ROOT, get_settings, load_models_config
from app.gateway.allowlist import RefusalRateLimiter
from app.gateway.ingress import handle_inbound
from app.runlog import setup_run_logging
from app.storage.db import connect
from app.storage.migrations import migrate

logger = logging.getLogger("app.channels.telegram")

DEFAULT_DB_NAME = "app.db"


def serve(
    conn,
    adapter: TelegramAdapter,
    advisor: Advisor,
    *,
    once: bool = False,
) -> int:
    """Long-poll Telegram, dispatching each update through the gateway.

    ``once`` drains the currently-pending updates and returns (handy for a
    smoke test); otherwise it loops until interrupted (Ctrl-C).
    """
    policy = get_policies()
    rate_limiter = RefusalRateLimiter(
        max_per_window=policy.refusal_rate_limit_max,
        window_seconds=policy.refusal_rate_limit_window_seconds,
    )
    offset: int | None = None
    logger.info("telegram runner started (once=%s)", once)
    while True:
        try:
            updates = adapter.get_updates(offset=offset)
        except TelegramError as exc:
            logger.error("getUpdates failed: %s", exc)
            return 1
        for update in updates:
            offset = int(update["update_id"]) + 1
            inbound = adapter.parse_inbound(update)
            if inbound is None:
                continue
            result = handle_inbound(
                conn, inbound, advisor=advisor, policy=policy, rate_limiter=rate_limiter
            )
            logger.info(
                "inbound from %s:%s → %s",
                inbound.channel,
                inbound.channel_user_id,
                result.action,
            )
            if result.reply is not None:
                try:
                    adapter.send(result.reply)
                except TelegramError as exc:
                    logger.error("send failed: %s", exc)
        if once:
            return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli.telegram",
        description="Run the Telegram bot (long-poll) behind the paired-owner allowlist.",
    )
    parser.add_argument(
        "--db", type=Path, default=None, help="database file (default: data/app.db)"
    )
    parser.add_argument("--once", action="store_true", help="drain pending updates once, then exit")
    parser.add_argument(
        "-d", "--debug", action="store_true", help="stream logs (incl. model responses) to console"
    )
    args = parser.parse_args(argv)

    load_dotenv(REPO_ROOT / ".env", override=False)
    setup_run_logging("telegram", console_level=logging.DEBUG if args.debug else logging.INFO)

    settings = get_settings()
    token = settings.telegram_bot_token
    if token is None or not token.reveal().strip():
        print("[fail] TELEGRAM_BOT_TOKEN is not set (add it to .env).")
        return 1

    try:
        models = load_models_config()
        resolver = build_resolver(models)
    except (MissingCredentialError, KeyError, FileNotFoundError) as exc:
        print(f"[fail] configuration error: {exc}")
        return 1

    adapter = TelegramAdapter(
        settings.telegram_bot_token,
        webhook_secret=settings.telegram_webhook_secret,
    )
    db_path = args.db or (REPO_ROOT / "data" / DEFAULT_DB_NAME)
    conn = connect(db_path)
    try:
        migrate(conn)
        advisor = Advisor(resolve_provider=resolver, conn=conn)
        print("[ok]   Telegram bot running. Press Ctrl-C to stop.")
        return serve(conn, adapter, advisor, once=args.once)
    except KeyboardInterrupt:
        print("\n[ok]   Stopped.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
