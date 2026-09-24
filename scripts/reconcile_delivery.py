#!/usr/bin/env python3
"""RECONCILE one destination's delivery BY IDENTITY. Counts only.

    python scripts/reconcile_delivery.py --sport NFL --work "$work" --payload "$payload" --receipts "$receipts"

The payload a production run builds holds EVERY eligible post-cutover wager for the sport, every run -- not just
the new ones. So after the delivery step, each source key in it must be in exactly one of these states:

    ON_LEDGER            its source key is on the destination's canonical ledger branch (merged)
    PROPOSED_NOT_MERGED  the importer accepted it (NEW / DUPLICATE_NOOP) and it is on the router's proposal, but
                         the proposal has not merged yet (the gate is waiting, or the destination is held for
                         observation) -- a legitimate, visible pending state
    REFUSED              the destination's importer refused it (REFUSED / CONFLICT), with its reason on the receipt
    UNACCOUNTED          none of the above: a wager the router built that is on no ledger, on no proposal and
                         was refused by nobody. The one state that must never exist.

Before 2026-09-24 there was a fourth state nobody could see: 2026 week 2's NFL wagers were refused BEFORE a
payload existed (no destination profile) and counted as by-design, so no receipt and no ledger ever named them.
That half is now a BLOCKED health state in the production filter; this is the other half -- the wagers that do
reach a payload are proven, by identity rather than by row count, to have landed somewhere accountable.

The ledger is read from a FRESH fetch of the destination's ledger branch (after any merge this run made), from
git objects only. This repository's logs are public: only counts are printed, never a key, a ticker or a stake.
Exit 0 when nothing is UNACCOUNTED, 1 otherwise, 2 when the inputs could not be read.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from kalshi_router.destinations import UnknownDestinationError, profile_for  # noqa: E402
from kalshi_router.receipts import normalise  # noqa: E402

ON_LEDGER, PROPOSED, REFUSED, UNACCOUNTED = "ON_LEDGER", "PROPOSED_NOT_MERGED", "REFUSED", "UNACCOUNTED"
ACCEPTED = frozenset({"NEW", "DUPLICATE_NOOP", "CORRECTED"})
KEY_FIELDS = ("source_bet_key", "sourceBetKey")


def _git(work, *args, stdin=None):
    return subprocess.run(["git", "-C", work, *args], input=stdin, capture_output=True, text=True, check=False)


def payload_keys(payload: dict, kind: str = "wagers") -> list:
    if kind == "settlements":
        rows = payload.get("settlements") if isinstance(payload, dict) else None
    else:
        rows = payload.get("rows") if isinstance(payload, dict) else None
        if rows is None and isinstance(payload, dict):
            rows = payload.get("bets") or payload.get("wagers") or []
    keys = []
    for row in rows or []:
        if isinstance(row, dict):
            key = next((row.get(f) for f in KEY_FIELDS if row.get(f)), None)
            keys.append(key)
    return keys


def _keys_from_text(text: str) -> set:
    out = set()
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            for f in KEY_FIELDS:
                if row.get(f):
                    out.add(row[f])
    return out


def ledger_keys(work: str, ref: str, profile, kind: str = "wagers") -> set:
    """Every source key on `ref` of the destination's canonical wager (or settlement) ledger."""
    settlements = kind == "settlements"
    if profile.record_layout == "json_per_file":
        matching = [p for p in profile.committable_prefixes if ("settlement" in p) == settlements]
        if not matching:
            raise RuntimeError(f"the profile names no {kind} ledger")
        prefix = matching[0]
        listing = _git(work, "ls-tree", "-r", "--name-only", ref, "--", prefix)
        if listing.returncode != 0:
            raise RuntimeError("could not list the ledger")
        paths = [p for p in listing.stdout.splitlines() if p.endswith(".json")]
        if not paths:
            return set()
        batch = subprocess.run(["git", "-C", work, "cat-file", "--batch"],
                               input="".join(f"{ref}:{p}\n" for p in paths).encode(),
                               capture_output=True, check=False)
        keys, data, i = set(), batch.stdout, 0
        # --batch output: b"<sha> blob <size>\n<content>\n" per object; sizes are BYTES.
        while i < len(data):
            nl = data.index(b"\n", i)
            header = data[i:nl].split()
            if len(header) < 3 or header[1] != b"blob":
                i = nl + 1
                continue
            size = int(header[2])
            body = data[nl + 1: nl + 1 + size]
            i = nl + 1 + size + 1
            try:
                doc = json.loads(body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            for f in KEY_FIELDS:
                if isinstance(doc, dict) and doc.get(f):
                    keys.add(doc[f])
        return keys
    keys = set()
    for path in sorted(p for p in profile.mergeable_paths if ("settlement" in p) == settlements):
        shown = _git(work, "show", f"{ref}:{path}")
        if shown.returncode == 0:
            keys |= _keys_from_text(shown.stdout)
    return keys


def classify(keys: list, on_ledger: set, receipts) -> dict:
    by_key = {r.source_key: r for r in receipts if r.source_key}
    counts = {ON_LEDGER: 0, PROPOSED: 0, REFUSED: 0, UNACCOUNTED: 0}
    reasons: dict = {}
    for key in keys:
        r = by_key.get(key)
        # A destination that REFUSED this run's row outranks the key being on the ledger: a CONFLICT means the
        # ledger holds this order with DIFFERENT economics, which is not "delivered" -- it needs a person.
        if r is not None and not (r.verdict in ACCEPTED and r.success):
            counts[REFUSED] += 1
            reasons[str(r.verdict)] = reasons.get(str(r.verdict), 0) + 1
            continue
        if key and key in on_ledger:
            counts[ON_LEDGER] += 1
            continue
        if r is not None and r.verdict in ACCEPTED and r.success:
            counts[PROPOSED] += 1
        elif r is not None and not (r.verdict in ACCEPTED and r.success):
            counts[REFUSED] += 1
            reasons[str(r.verdict)] = reasons.get(str(r.verdict), 0) + 1
        else:
            counts[UNACCOUNTED] += 1
    return {"counts": counts, "refused_by_verdict": dict(sorted(reasons.items())), "payload_rows": len(keys)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sport", required=True)
    ap.add_argument("--work", required=True, help="the destination clone this delivery ran in")
    ap.add_argument("--payload", required=True)
    ap.add_argument("--receipts", required=True)
    ap.add_argument("--kind", default="wagers", choices=["wagers", "settlements"])
    ap.add_argument("--ref", default=None, help="ledger ref to read (default: a fresh fetch of the ledger branch)")
    a = ap.parse_args(argv)
    try:
        profile = profile_for(a.sport)
        payload = json.load(open(a.payload, encoding="utf-8"))
        try:
            receipts = normalise(json.load(open(a.receipts, encoding="utf-8")))
        except (OSError, ValueError):
            receipts = ()
    except (UnknownDestinationError, OSError, ValueError) as exc:
        print(f"  reconciliation could not read its inputs ({type(exc).__name__})", file=sys.stderr)
        return 2
    ref = a.ref
    if ref is None:
        ref = "refs/remotes/origin/reconcile-ledger"
        fetched = _git(a.work, "fetch", "-q", "--depth", "1", "origin",
                       f"+refs/heads/{profile.ledger_branch}:{ref}")
        if fetched.returncode != 0:
            print("  reconciliation could not fetch the ledger branch", file=sys.stderr)
            return 2
    try:
        on_ledger = ledger_keys(a.work, ref, profile, a.kind)
    except RuntimeError as exc:
        print(f"  reconciliation could not read the ledger ({exc})", file=sys.stderr)
        return 2
    result = classify(payload_keys(payload, a.kind), on_ledger, receipts)
    c = result["counts"]
    print(f"  reconciliation by identity ({a.sport} {a.kind}, {result['payload_rows']} eligible row(s)): "
          f"on ledger {c[ON_LEDGER]}, proposed not merged {c[PROPOSED]}, refused {c[REFUSED]} "
          f"{result['refused_by_verdict'] or ''}, UNACCOUNTED {c[UNACCOUNTED]}")
    return 1 if c[UNACCOUNTED] else 0


if __name__ == "__main__":
    raise SystemExit(main())
