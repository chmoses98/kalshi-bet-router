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

*** WHICH SEASON IS NOT THE SAME QUESTION AS "IS EVERY ROW ATTRIBUTABLE" ***
This script answers the first: which season's importer and ledger a batch
belongs to. It does NOT answer the second, and it must not refuse the whole
batch over it.

A settlement whose `source_bet_key` is in NO season's wager ledger is an
UNMATCHED row. That is not necessarily bad data. A wager legitimately goes

    generated -> delivered onto a proposal -> merged onto the canonical ledger

and its Kalshi market can settle while it is still in the middle state: the
wager is on the router's open pull request (CFB is held for observation, so
nothing merges on its own) and not yet on `accounting-data`. Measured in
production on 2026-09-26: 43 CFB settlements, 41 of whose wagers were on the
ledger and 2 of whose wagers were on the open wager proposal. Refusing the
batch here threw away the 41 over the 2, on every run, until a person merged
the wager proposal.

So when the rows that DO match prove exactly one season, that season is the
answer and the batch goes to that season's importer, unmatched rows included.
The destination's settlement importer is the authority on an unmatched row:
it refuses it PER ROW ("no wager with this source_bet_key is in the <season>
ledger"), writes nothing for it, writes the rest, and exits non-zero so the
workflow reports the delivery as PARTIAL. A refused row is never written as
canonical, never disappears (its receipt says REFUSED and reconciliation
counts it), and is re-offered on the next run, when its wager may have landed.

It cannot misfile an unmatched row, either: the importer writes a settlement
only if its wager is in THAT season's ledger, so a row whose wager belongs to
another season is refused, not filed under this one.

*** WHAT STILL FAILS CLOSED, BEFORE ANY IMPORTER RUNS ***
  * a row with no source key -- it cannot be attributed to anything;
  * no wager ledger at all;
  * EVERY row unmatched -- there is then no evidence of a season, and this
    script never guesses one: not from today's clock, not from the settlement
    time, not from a ticker;
  * matches spanning two seasons -- filing the batch under either year would
    misfile the other, and this does not split batches.

*** WHAT IT PRINTS ***
One four-digit year on stdout. Diagnostics, including counts, go to stderr.
No ticker, no payout, no key, no stake.

Exit codes:
    0  the season is on stdout (some rows may be unmatched; stderr counts them)
    2  unreadable payload or ledger, a row with no source key, no row matched,
       or matches spanning two seasons
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

    all_keys = set().union(*by_season.values())
    matched_keys = wanted & all_keys
    unmatched = wanted - all_keys
    matched = {season for season, keys in by_season.items() if wanted & keys}

    if not matched_keys:
        print(
            f"{len(unmatched)} settlement(s) refer to a wager this ledger has no record of, "
            "and not one settlement in this batch matched a wager -- so no season can be "
            "established, and none is guessed. A payout attributed to a bet nobody recorded "
            "would count in every total while belonging to nothing",
            file=sys.stderr,
        )
        # EVERY settlement unmatched is what a correct system looks like when
        # the wagers simply have not been delivered yet -- a sequencing
        # condition, not corrupt data -- and saying so is the difference
        # between an operator checking the delivery run and an operator
        # hunting a bug that is not there. The refusal itself does not
        # soften: with no match there is no season.
        print(
            "  EVERY settlement in this batch is unmatched, which is what it looks "
            "like when the wagers have not been delivered yet. Check that the "
            "delivery workflow has landed them (a DRY RUN builds the rows and "
            "deliberately pushes nothing), then re-run this.",
            file=sys.stderr,
        )
        return EXIT_REFUSED
    if len(matched) > 1:
        print(
            f"these settlements belong to {sorted(matched)}. The batch has to be split; "
            "filing it under either year would misfile the other",
            file=sys.stderr,
        )
        return EXIT_REFUSED

    season = next(iter(matched))
    print("settlement attribution:", file=sys.stderr)
    print(f"  canonical wager matches: {len(matched_keys)} (all in season {season})",
          file=sys.stderr)
    print(f"  unmatched settlement parents: {len(unmatched)}", file=sys.stderr)
    if unmatched:
        # NOT a refusal of the batch, and NOT an acceptance of these rows.
        # Which season this batch belongs to is proved by the rows that
        # matched; whether an unmatched row may be filed at all is the
        # destination importer's decision, made per row, and it refuses a
        # settlement whose wager is not in the season's ledger.
        print(
            f"  continuing to the destination importer for season {season}; the "
            f"{len(unmatched)} unmatched row(s) remain subject to its per-row refusal. "
            "A wager that is delivered but not yet merged (on an open proposal) looks "
            "exactly like this, and its settlement is re-offered on the next run.",
            file=sys.stderr,
        )

    print(season)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
