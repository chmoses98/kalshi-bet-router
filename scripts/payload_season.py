#!/usr/bin/env python3
"""Which season's ledger a payload belongs in. NEVER today's clock.

    python scripts/payload_season.py --payload payloads/CFB.json --sport CFB

*** WHY THIS IS NOT `date +%Y` ***
A College Football season spans two calendar years. A January bowl game played
on 2027-01-02 belongs to the 2026 season, and a delivery run that filed it
under 2027 would put a real wager in a ledger nobody reconciles -- silently,
because both files exist and both are valid.

So the season comes from the CONTEST DATES the payload itself carries, through
the same rule the destination uses: a game before August belongs to the
previous year's season.

*** AND WHY IT REFUSES A SPLIT PAYLOAD ***
A payload whose games span two seasons cannot be filed under one of them, and
choosing either would misfile the other. Splitting it is the right answer and a
delivery workflow is not where that decision belongs, so this refuses and names
both years. It has not happened -- the scheduled job delivers a bounded window
of recent activity -- and "it has not happened" is not a reason to guess when
it does.

*** WHAT IT PRINTS ***
One four-digit year on stdout, and nothing else. Diagnostics go to stderr.
A game DATE is a public fact about a football schedule, not a betting fact, but
this still prints no ticker, price, stake or key.

Exit codes:
    0  the season is on stdout
    2  unreadable payload, no dates, or dates spanning two seasons
"""

from __future__ import annotations

import argparse
import json
import sys

EXIT_OK = 0
EXIT_REFUSED = 2

#: The month a new season starts. Anything earlier belongs to the previous
#: year's season, which is what makes a January bowl game file correctly.
SEASON_START_MONTH = 8

#: Where each destination's row keeps the contest date. Each ledger spells it
#: its own way and guessing is how a delivery reads None and files everything
#: under one year.
DATE_FIELDS = ("game_date", "gameDate", "contest_date", "date")


def season_of(game_date: str) -> int | None:
    parts = game_date.split("-")
    if len(parts) < 2:
        return None
    try:
        year, month = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not 1 <= month <= 12:
        return None
    return year if month >= SEASON_START_MONTH else year - 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--payload", required=True)
    parser.add_argument("--sport", required=True)
    args = parser.parse_args(argv)

    try:
        with open(args.payload, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"unreadable payload: {type(exc).__name__}", file=sys.stderr)
        return EXIT_REFUSED

    rows = payload.get("rows") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows:
        print("the payload carries no rows", file=sys.stderr)
        return EXIT_REFUSED

    seasons: set[int] = set()
    undated = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        value = next((row[f] for f in DATE_FIELDS if row.get(f)), None)
        if not value:
            undated += 1
            continue
        season = season_of(str(value))
        if season is None:
            undated += 1
            continue
        seasons.add(season)

    if undated:
        # A row with no readable date cannot be filed, and filing the batch
        # around it would write that row into a season nobody established.
        print(
            f"{undated} row(s) carry no readable contest date; the season cannot be "
            "established for this batch",
            file=sys.stderr,
        )
        return EXIT_REFUSED
    if not seasons:
        print("no row carried a contest date", file=sys.stderr)
        return EXIT_REFUSED
    if len(seasons) > 1:
        print(
            f"this payload spans {sorted(seasons)}. It has to be split; filing it under "
            "either year would misfile the other, and a delivery workflow is not where "
            "that decision belongs",
            file=sys.stderr,
        )
        return EXIT_REFUSED

    print(next(iter(seasons)))
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
