"""Confirm + activate a feature job's generated skill (design-spec §5/§6B; T6.10).

A **feature** job's deliverable is a reusable skill, written **inert** under
``app/skills/generated/<request-code>/`` and announced to the owner ("confirm to
activate it"). Activation is a separate, explicit step gated on confirmation
(`confirm_generated_code`): the owner reviews the code, then runs this command to
load + register it.

Run from the ``backend/`` directory:

    python -m app.cli.confirm --list            # show generated bundles + status
    python -m app.cli.confirm <request-code>    # review-confirmed → activate it
    python -m app.cli.confirm <request-code> --decline   # leave it inert

Activation flips the bundle's manifest to ``active``; from then on every process
re-registers it at startup (`codegen.load_active`), so the skill survives a
restart. A running server/worker picks it up on its next start.
"""

from __future__ import annotations

import argparse
import sys

from app.skills import codegen


def _print_bundles() -> None:
    bundles = codegen.list_bundles()
    if not bundles:
        print("No generated skill bundles found.")
        return
    print(f"{len(bundles)} generated bundle(s):")
    for bundle in bundles:
        files = ", ".join(bundle.files) or "—"
        print(f"  {bundle.job_code}  [{bundle.status}]  files: {files}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli.confirm",
        description="Confirm + activate a feature job's generated skill.",
    )
    parser.add_argument(
        "code", nargs="?", help="the request code of the generated bundle to activate"
    )
    parser.add_argument(
        "--list", action="store_true", help="list generated bundles and their status"
    )
    parser.add_argument(
        "--decline",
        action="store_true",
        help="decline activation: leave the code inert (nothing is loaded)",
    )
    args = parser.parse_args(argv)

    if args.list or not args.code:
        _print_bundles()
        if not args.code:
            return 0

    if args.decline:
        print(f"Declined: {args.code} stays inert (no code loaded or registered).")
        return 0

    try:
        activated = codegen.confirm_and_activate(codegen.GENERATED_ROOT, args.code, confirmed=True)
    except codegen.GeneratedActivationError as exc:
        print(f"[fail] could not activate {args.code!r}: {exc}")
        return 1

    if not activated:
        print(f"[warn] no new skills registered for {args.code!r} (already active or empty?).")
        return 0
    print(f"Activated {len(activated)} skill(s) from {args.code}: {', '.join(activated)}")
    print("It is now registered and will re-load automatically on future starts.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
