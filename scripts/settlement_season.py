#!/usr/bin/env python3
"""Which season's ledger a SETTLEMENT payload belongs in.

    python scripts/settlement_season.py --payload CFB-settlements.json --ledger-dir /tmp/dest

*** WHY A SETTLEMENT'S SEASON CANNOT COME FROM THE SETTLEMENT ***
A settlement row carries a market ticker, a result, a payout and the time the
MARKET SETTLED. It carries no contest date, and the settlement time is the
wrong answer in exactly the case that matters: a bowl game played on
2027-01-02 settles in 2027 and belongs to the 2026 season.

*** SO IT COMES FROM THE WAGER ***
The wager this settles is already in the destination's ledger, filed under the
season its own contest date established when it was imported. That file is the
answer, and reading it is not an inference -- it is looking up the row the
settlement refers to by the key the settlement itself carries.

*** AND AN ORPHAN IS REFUSED HERE TOO ***
A settlement whose `source_bet_key` appears in NO season's wager ledger is a
payout with no home. The destination's importer refuses it as well, which is
the authoritative refusal; this one refuses earlier, before a clone is written
to, and names the count.

*** WHAT IT PRINTS ***
One four-digit year on stdout. Diagnostics, including counts, go to stderr.
No ticker, no payout, no key, no stake.

Exit codes:
    0  the season is on stdout
    2  unreadable payload or ledger, no match, or matches spanning two seasons
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

EXIT_OK = 0
EXIT_REFUSED = 2

WAGERS_SUBDIR = "wagers"
_SEASON_FILE = re.compile(r"^(\d{4})\.jsonl$")


def keys_by_season(ledger_dir: Path) -> dict[int, set[str]]:
    """{season: every source_bet_key in that season's wager ledger}.

    A line that is not decodable JSON is SKIPPED here rather than raised on.
    This script only decides which file to hand the importer; the importer
    reads the same ledger and is the one that must refuse on a corrupt line,
    with its own error. Failing here would turn a corrupt row anywhere in the
    file into "no season", which is a less specific complaint about a less
    specific problem.
    """
    out: dict[int, set[str]] = {}
    root = ledger_dir / WAGERS_SUBDIR
    if not root.is_dir():
        return out
    for path in sorted(root.glob("*.jsonl")):
        match = _SEASON_FILE.match(path.name)
        if not match:
            continue
        season = int(match.group(1))
        keys: set[str] = set()
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = row.get("source_bet_key")
            if isinstance(key, str) and key.strip():
                keys.add(key)
        if keys:
            out[season] = keys
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--payload", required=True)
    parser.add_argument(
        "--ledger-dir", required=True, help="a checkout of the destination's ledger branch"
    )
    args = parser.parse_args(argv)

    try:
        with open(args.payload, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"unreadable payload: {type(exc).__name__}", file=sys.stderr)
        return EXIT_REFUSED

    rows = payload.get("settlements") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows:
        print("the payload carries no settlements", file=sys.stderr)
        return EXIT_REFUSED

    wanted = {
        str(row["source_bet_key"])
        for row in rows
        if isinstance(row, dict) and row.get("source_bet_key")
    }
    if len(wanted) != len(rows):
        print(
            f"{len(rows) - len(wanted)} settlement(s) carry no source key and cannot be "
            "attributed to a wager",
            file=sys.stderr,
        )
        return EXIT_REFUSED

    by_season = keys_by_season(Path(args.ledger_dir))
    if not by_season:
        print(
            f"no wager ledger under {args.ledger_dir}/{WAGERS_SUBDIR}; a settlement cannot "
            "be filed before the wager it settles",
            file=sys.stderr,
        )
        return EXIT_REFUSED

    matched = {season for season, keys in by_season.items() if wanted & keys}
    orphans = wanted - set().union(*by_season.values())

    if orphans:
        print(
            f"{len(orphans)} settlement(s) refer to a wager this ledger has no record of. "
            "A payout attributed to a bet nobody recorded would count in every total while "
            "belonging to nothing",
            file=sys.stderr,
        )
        if orphans == wanted:
            # EVERY settlement orphaned is a different situation from a few.
            # It is what a correct system looks like when the wagers simply
            # have not been delivered yet -- a sequencing condition, not
            # corrupt data -- and saying so is the difference between an
            # operator checking the delivery run and an operator hunting a
            # bug that is not there. The refusal itself does not soften: a
            # settlement still may not be filed before the wager it settles.
            print(
                "  EVERY settlement in this batch is unmatched, which is what it looks "
                "like when the wagers have not been delivered yet. Check that the "
                "delivery workflow has landed them (a DRY RUN builds the rows and "
                "deliberately pushes nothing), then re-run this.",
                file=sys.stderr,
            )
        return EXIT_REFUSED
    if not matched:
        print("no settlement matched any season's wagers", file=sys.stderr)
        return EXIT_REFUSED
    if len(matched) > 1:
        print(
            f"these settlements belong to {sorted(matched)}. The batch has to be split; "
            "filing it under either year would misfile the other",
            file=sys.stderr,
        )
        return EXIT_REFUSED

    print(next(iter(matched)))
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
