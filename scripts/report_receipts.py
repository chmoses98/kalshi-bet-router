#!/usr/bin/env python3
"""What the destination's importer said it did. COUNTS AND FIELD NAMES ONLY.

    python scripts/report_receipts.py --receipts receipts-CFB.json

*** WHAT REACHES A PUBLIC LOG ***
Row counts, verdict counts, how many rows came back with a canonical id, how
many failed, and the NAMES of fields the destination disagreed on. That is
everything an operator needs to act on a refusal.

*** WHAT DOES NOT ***
The ticker, the stake, the price, the contract count, the source key, and the
`existing`/`incoming` VALUES of a conflict. This repository is public and so
are its Actions logs; a stake echoed here is a disclosure no later redaction
undoes.

*** WHY FIELD NAMES ARE THE EXCEPTION ***
A refusal nobody can identify is a refusal nobody can resolve. A CONFLICT is
correct and deliberate -- the destination's stored row disagrees with this
reading and only a person may decide which is right -- but until somebody
does, every scheduled run refuses the same row and goes red, and a log that
says only "CONFLICT: 1" gives them nothing to go on. The NAME of the field
that disagrees is actionable and is not a bet.

Reads every destination's receipt shape through `kalshi_router.receipts`, so
the log says the same thing about MLB's list of camelCase rows and CFB's
object of snake_case counts.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from kalshi_router.receipts import (  # noqa: E402
    conflicting_field_names,
    normalise,
    verdict_counts,
)

EXIT_OK = 0
EXIT_UNREADABLE = 2


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipts", required=True)
    parser.add_argument(
        "--indent", default="  ", help="leading whitespace, so the workflow's log lines up"
    )
    args = parser.parse_args(argv)

    try:
        with open(args.receipts, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        # NOT a failure of this step. The importer's own exit code decides
        # whether the delivery failed; an unreadable receipt file makes the
        # merge gate WAIT, which is the correct fail-closed outcome and is
        # decided there rather than here.
        print(f"{args.indent}receipts could not be read ({type(exc).__name__})")
        return EXIT_UNREADABLE

    receipts = normalise(payload)
    pad = args.indent
    print(f"{pad}rows: {len(receipts)}")
    for verdict, count in verdict_counts(receipts).items():
        print(f"{pad}  {verdict}: {count}")
    print(f"{pad}canonical ids returned: {sum(1 for r in receipts if r.identity)}")
    print(f"{pad}failed rows: {sum(1 for r in receipts if not r.success)}")

    names = conflicting_field_names(receipts)
    if names:
        print(f"{pad}fields the destination disagrees on (names only): {', '.join(names)}")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
