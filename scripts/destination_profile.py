#!/usr/bin/env python3
"""Print one destination's profile, for the delivery workflow's shell to read.

    python scripts/destination_profile.py CFB
    python scripts/destination_profile.py CFB --field repo
    python scripts/destination_profile.py --list

*** WHY A SCRIPT AND NOT A `case` STATEMENT ***
The delivery workflow used to know MLB's repository, ledger branch, importer
command and allowed path prefix as shell literals. So did the backfill
workflow, and the recovery workflow, each with its own copy. Four places that
could disagree about one destination, and adding a sport meant remembering all
four -- which is exactly why production could route MLB while the one-time
backfill could route three sports.

`kalshi_router.destinations.PROFILES` is the one place now, and this is how a
shell reads it. Nothing here decides anything.

*** WHAT IS SAFE TO PRINT ***
A repository name, a branch name, a path prefix and a command template. All
four are already public facts about this system -- the repositories are public
and the workflows are in this repository. NO WAGER, no ticker, no price and no
stake can reach this script: it never opens a payload.

Exit codes:
    0  printed
    2  no such destination, or no such field
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from kalshi_router.destinations import (  # noqa: E402
    PROFILES,
    UnknownDestinationError,
    describe,
    profile_for,
    render_command,
)

EXIT_OK = 0
EXIT_UNKNOWN = 2


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sport", nargs="?", help="the destination, e.g. MLB or CFB")
    parser.add_argument(
        "--list", action="store_true", help="print every routable destination, one per line"
    )
    parser.add_argument(
        "--field",
        default=None,
        help=(
            "print one field as a bare string instead of the whole JSON object. A list "
            "field prints one element per line, which is what a shell array wants."
        ),
    )
    parser.add_argument(
        "--importer",
        choices=("wager", "settlement"),
        default=None,
        help="render that importer's command, one argument per line, with placeholders filled",
    )
    parser.add_argument("--payload", default=None)
    parser.add_argument("--work", default=None)
    parser.add_argument("--code", default=None)
    parser.add_argument("--receipts", default=None)
    parser.add_argument("--season", default=None)
    args = parser.parse_args(argv)

    if args.list:
        for sport in sorted(s.value for s in PROFILES):
            print(sport)
        return EXIT_OK

    if not args.sport:
        print("name a destination, or pass --list", file=sys.stderr)
        return EXIT_UNKNOWN

    try:
        profile = profile_for(args.sport)
    except UnknownDestinationError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_UNKNOWN

    if args.importer:
        template = (
            profile.wager_importer
            if args.importer == "wager"
            else profile.settlement_importer
        )
        if template is None:
            print(
                f"{profile.sport.value} has no settlement importer: that destination settles "
                "its own bets, and a second authority on one fact is worse than none",
                file=sys.stderr,
            )
            return EXIT_UNKNOWN
        try:
            rendered = render_command(
                template,
                payload=args.payload or "",
                work=args.work or "",
                code=args.code or args.work or "",
                receipts=args.receipts or "",
                season=args.season,
            )
        except UnknownDestinationError as exc:
            print(str(exc), file=sys.stderr)
            return EXIT_UNKNOWN
        # ONE ARGUMENT PER LINE, and the caller reads it with `mapfile`. A
        # single space-joined string would break the first time a path had a
        # space in it, which on a runner is a matter of when rather than if.
        for part in rendered:
            print(part)
        return EXIT_OK

    described = describe(args.sport)
    if args.field:
        if args.field not in described:
            print(
                f"no field {args.field!r}; known: {sorted(described)}", file=sys.stderr
            )
            return EXIT_UNKNOWN
        value = described[args.field]
        if isinstance(value, list):
            for item in value:
                print(item)
        elif value is None:
            print("")
        elif isinstance(value, bool):
            print("true" if value else "false")
        else:
            print(value)
        return EXIT_OK

    print(json.dumps(described, indent=2, sort_keys=True))
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
