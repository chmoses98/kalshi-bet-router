"""Append-only EXECUTION-EVIDENCE ledger: order/fill digests, closing prices, and recommendation links.

SINGLE FILE, ZERO DEPENDENCIES, VENDORED VERBATIM into every destination repository (``scripts/accounting/``). The
copies are pinned by sha256 in the router (``tests/test_execution_evidence_ledger.py``), so a drifted copy fails CI.

WHY A SEPARATE LEDGER, NOT NEW WAGER FIELDS
-------------------------------------------
Each destination's wager importer REFUSES an unknown field (``routed_ledger.WAGER_INPUT_FIELDS`` is closed) and a
wager row is never rewritten. Adding fields to wager rows would make every not-yet-updated destination refuse the
row. This ledger is a different file with its own importer, keyed by the wager's ``source_bet_key``, so:

* a wager importer can never refuse a wager because of evidence, old or new;
* evidence for wagers already on the ledger can be added without rewriting them;
* ``DUPLICATE_NOOP`` / ``CONFLICT`` semantics of the wager ledger are untouched.

PRIVACY (every destination repository is PUBLIC)
------------------------------------------------
Raw order ids, fill ids and trade ids are PRIVATE. This module accepts only domain-separated SHA-256 digests (32 hex
characters) and REFUSES any field named like a raw id, and any string value shaped like a UUID (Kalshi order and
fill ids are UUIDs). The owner recomputes a digest from an id in their own account to match a row.

THREE KINDS, THREE PRODUCERS
----------------------------
``EXECUTION``  producer ``router``       order digest, fill digests, first/last fill time, buy/sell verb, taker flag
``CLOSE``      producer ``router``|``edge_finder``   the market's closing quote with an explicit QUALITY LABEL
``LINK``       producer ``edge_finder``  recommendation_ref / model_run_ref / model_as_of, written only by the sport
               repository that owns the decision records. The router never asserts a recommendation link.

A CLOSE is labelled ``TRUE_CLOSE`` (the final quote at/just before the scheduled start), ``EARLIER_QUOTE_PROXY``
(the last quote we could find, but too early to call a close) or ``MISSING``. A proxy is NEVER labelled a close.

Keys (append-only; an existing row is never rewritten):
    EXECUTION  (source_bet_key)            CLOSE  (source_bet_key, quality)       LINK  (source_bet_key, status, ref)
Identical row again -> DUPLICATE_NOOP. Same key, different content -> CONFLICT (kept out, flagged).

Stdout carries counts only. This file is committed to public repositories.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "edge_finder.execution_evidence.v1"
NEW, DUPLICATE_NOOP, CONFLICT, REFUSED = "NEW", "DUPLICATE_NOOP", "CONFLICT", "REFUSED"

KIND_EXECUTION, KIND_CLOSE, KIND_LINK = "EXECUTION", "CLOSE", "LINK"
PRODUCER_ROUTER, PRODUCER_EDGE_FINDER = "router", "edge_finder"
QUALITY_TRUE_CLOSE = "TRUE_CLOSE"
QUALITY_PROXY = "EARLIER_QUOTE_PROXY"
QUALITY_MISSING = "MISSING"
CLOSE_QUALITIES = (QUALITY_TRUE_CLOSE, QUALITY_PROXY, QUALITY_MISSING)
LINK_LINKED, LINK_AMBIGUOUS, LINK_MISSING = "LINKED", "AMBIGUOUS", "MISSING"
LINK_STATUSES = (LINK_LINKED, LINK_AMBIGUOUS, LINK_MISSING)

DIGEST_RE = re.compile(r"^[0-9a-f]{32}$")
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_RAW_ID_NAME_RE = re.compile(
    r"(^|_)(order|fill|trade|client_order|exec(ution)?)_?ids?$|^(fills?|orders?)$", re.IGNORECASE
)
_SOURCE_KEY_RE = re.compile(r"^kalshi:v1:[0-9a-f]{64}$")

_COMMON = {"schema_version", "kind", "producer", "source_bet_key", "recorded_at"}
_EXECUTION = {
    "order_id_digest",
    "fill_id_digests",
    "fill_count",
    "first_fill_ts",
    "last_fill_ts",
    "execution_action",
    "all_taker",
    "flags",
}
_CLOSE = {"market_ticker", "side", "close"}
_CLOSE_BODY = {
    "quality",
    "yes_bid",
    "yes_ask",
    "yes_mid",
    "last_trade",
    "as_of_ts",
    "cutoff_ts",
    "period_minutes",
    "minutes_before_cutoff",
    "source_endpoint",
    "flags",
    "reason",
}
_LINK = {"status", "recommendation_ref", "model_run_ref", "model_as_of", "link_basis"}
_ALLOWED = {
    KIND_EXECUTION: _COMMON | _EXECUTION,
    KIND_CLOSE: _COMMON | _CLOSE,
    KIND_LINK: _COMMON | _LINK,
}
_PRODUCERS = {
    KIND_EXECUTION: {PRODUCER_ROUTER},
    KIND_CLOSE: {PRODUCER_ROUTER, PRODUCER_EDGE_FINDER},
    KIND_LINK: {PRODUCER_EDGE_FINDER},
}


class RowRefused(ValueError):
    """This row cannot be filed, and filing it anyway would be worse."""


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v))


def _aware_ts(v: Any) -> bool:
    if not isinstance(v, str) or "T" not in v:
        return False
    try:
        return datetime.fromisoformat(v).tzinfo is not None
    except ValueError:
        return False


def _walk_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield str(k)
            yield from _walk_strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _walk_strings(v)


def _walk_keys(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        for k, v in value.items():
            yield str(k)
            yield from _walk_keys(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _walk_keys(v)


def _price(v: Any) -> bool:
    return _is_num(v) and 0.0 < float(v) < 1.0


def privacy_problems(row: Any) -> list[str]:
    """Raw-identifier shapes. Checked on EVERY row before anything else."""
    problems = []
    for key in _walk_keys(row):
        if _RAW_ID_NAME_RE.search(key):
            problems.append(
                f"field name {key!r} looks like a raw exchange id; only *_digest fields are accepted"
            )
    for s in _walk_strings(row):
        if _UUID_RE.search(s):
            problems.append(
                "a string value is shaped like a UUID (a raw order/fill/trade id); refused"
            )
            break
    return problems


def validate_row(row: Any) -> list[str]:
    if not isinstance(row, dict):
        return ["row is not an object"]
    p = privacy_problems(row)
    if p:
        return p  # do not echo anything else about a row that may carry a raw id
    kind = row.get("kind")
    if kind not in _ALLOWED:
        return [f"kind must be one of {sorted(_ALLOWED)}"]
    out: list[str] = []
    unknown = sorted(set(row) - _ALLOWED[kind])
    if unknown:
        out.append(f"unknown field(s) {unknown}")
    if row.get("schema_version") != SCHEMA_VERSION:
        out.append(f"schema_version must be {SCHEMA_VERSION}")
    if row.get("producer") not in _PRODUCERS[kind]:
        out.append(f"{kind} rows may only be produced by {sorted(_PRODUCERS[kind])}")
    if not (
        isinstance(row.get("source_bet_key"), str) and _SOURCE_KEY_RE.match(row["source_bet_key"])
    ):
        out.append("source_bet_key must be kalshi:v1:<64 hex>")
    if "recorded_at" in row and not _aware_ts(row["recorded_at"]):
        out.append("recorded_at must be an RFC 3339 timestamp with a zone")
    if kind == KIND_EXECUTION:
        out += _validate_execution(row)
    elif kind == KIND_CLOSE:
        out += _validate_close(row)
    else:
        out += _validate_link(row)
    return out


def _validate_execution(row: dict) -> list[str]:
    out = []
    if not (
        isinstance(row.get("order_id_digest"), str) and DIGEST_RE.match(row["order_id_digest"])
    ):
        out.append("order_id_digest must be 32 lowercase hex characters")
    fills = row.get("fill_id_digests")
    if not isinstance(fills, list) or not all(
        isinstance(d, str) and DIGEST_RE.match(d) for d in fills
    ):
        out.append("fill_id_digests must be a list of 32-hex digests (possibly empty)")
    elif len(set(fills)) != len(fills):
        out.append("fill_id_digests contains a duplicate")
    fc = row.get("fill_count")
    if not isinstance(fc, int) or isinstance(fc, bool) or fc < 0:
        out.append("fill_count must be a non-negative integer")
    for name in ("first_fill_ts", "last_fill_ts"):
        if not _is_num(row.get(name)) or row[name] <= 0:
            out.append(f"{name} must be a positive epoch time")
    first, last = row.get("first_fill_ts"), row.get("last_fill_ts")
    if _is_num(first) and _is_num(last) and last < first:
        out.append("last_fill_ts precedes first_fill_ts")
    if row.get("execution_action") not in (None, "BUY", "SELL"):
        out.append("execution_action must be BUY, SELL or null (null = not established)")
    if row.get("all_taker") not in (None, True, False):
        out.append("all_taker must be true, false or null (null = not established)")
    if not isinstance(row.get("flags"), list) or not all(isinstance(f, str) for f in row["flags"]):
        out.append("flags must be a list of strings")
    return out


def _validate_close(row: dict) -> list[str]:
    out = []
    if not (isinstance(row.get("market_ticker"), str) and row["market_ticker"].strip()):
        out.append("market_ticker is required")
    if row.get("side") not in ("YES", "NO"):
        out.append("side must be YES or NO")
    c = row.get("close")
    if not isinstance(c, dict):
        return out + ["close must be an object"]
    unknown = sorted(set(c) - _CLOSE_BODY)
    if unknown:
        out.append(f"unknown close field(s) {unknown}")
    q = c.get("quality")
    if q not in CLOSE_QUALITIES:
        return out + [f"close.quality must be one of {list(CLOSE_QUALITIES)}"]
    prices = [n for n in ("yes_bid", "yes_ask", "yes_mid", "last_trade") if c.get(n) is not None]
    for n in prices:
        if not _price(c[n]):
            out.append(f"close.{n} must be a number strictly between 0 and 1 (dollars)")
    if q == QUALITY_MISSING:
        if prices:
            out.append("a MISSING close carries no price")
        if not (isinstance(c.get("reason"), str) and c["reason"]):
            out.append("a MISSING close must state its reason")
        return out
    if not prices:
        out.append(f"a {q} close must carry at least one price")
    if (c.get("yes_bid") is None) != (c.get("yes_ask") is None):
        out.append("yes_bid and yes_ask are recorded together or not at all")
    if _price(c.get("yes_bid")) and _price(c.get("yes_ask")) and c["yes_ask"] < c["yes_bid"]:
        out.append("crossed quote: yes_ask below yes_bid")
    for n in ("as_of_ts", "cutoff_ts"):
        if not _is_num(c.get(n)) or c[n] <= 0:
            out.append(f"close.{n} must be a positive epoch time")
    if (
        _is_num(c.get("as_of_ts"))
        and _is_num(c.get("cutoff_ts"))
        and c["as_of_ts"] > c["cutoff_ts"]
    ):
        out.append("a close observed AFTER its cutoff is not a pre-start close")
    if not (isinstance(c.get("source_endpoint"), str) and c["source_endpoint"]):
        out.append("close.source_endpoint is required")
    pm = c.get("period_minutes")
    if not isinstance(pm, int) or isinstance(pm, bool) or pm <= 0:
        out.append("close.period_minutes must be a positive integer")
    if q == QUALITY_TRUE_CLOSE and isinstance(pm, int) and pm > 60:
        out.append("a TRUE_CLOSE cannot come from a candle longer than 60 minutes")
    return out


def _validate_link(row: dict) -> list[str]:
    out = []
    status = row.get("status")
    if status not in LINK_STATUSES:
        return ["status must be one of " + str(list(LINK_STATUSES))]
    refs = [n for n in ("recommendation_ref", "model_run_ref") if row.get(n) is not None]
    for n in refs:
        if not (isinstance(row[n], str) and DIGEST_RE.match(row[n])):
            out.append(f"{n} must be an opaque 32-hex reference (digest), not a private identifier")
    if status == LINK_LINKED:
        if "recommendation_ref" not in refs:
            out.append("a LINKED row must carry recommendation_ref")
        if row.get("model_as_of") is None or not _aware_ts(row.get("model_as_of")):
            out.append("a LINKED row must carry model_as_of as an RFC 3339 timestamp with a zone")
    elif refs or row.get("model_as_of") is not None:
        out.append(f"a {status} row carries no reference and no model_as_of")
    if not isinstance(row.get("link_basis"), dict) or not row["link_basis"]:
        out.append("link_basis (how the match was made) is required")
    return out


def row_key(row: dict) -> tuple:
    kind = row["kind"]
    if kind == KIND_EXECUTION:
        return (kind, row["source_bet_key"])
    if kind == KIND_CLOSE:
        return (kind, row["source_bet_key"], row["close"]["quality"])
    return (kind, row["source_bet_key"], row["status"], row.get("recommendation_ref"))


_VOLATILE = {"recorded_at"}


def _identity(row: dict) -> dict:
    return {k: v for k, v in row.items() if k not in _VOLATILE}


def read_jsonl(path: Path) -> list[dict]:
    path = Path(path)
    if not path.exists():
        return []
    rows = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise RowRefused(f"{path.name} line {n} is not JSON") from exc
    return rows


@dataclass
class ImportResult:
    receipts: list[dict] = field(default_factory=list)
    written: int = 0
    duplicate: int = 0
    refused: int = 0

    def counts(self) -> dict:
        return {"written": self.written, "alreadyPresent": self.duplicate, "refused": self.refused}


def import_rows(ledger: Path, rows: list) -> ImportResult:
    """Append every valid, new row; never rewrite. One receipt per input row, in order."""
    ledger = Path(ledger)
    on_file = {row_key(r): r for r in read_jsonl(ledger)}
    res = ImportResult()
    new: list[dict] = []
    for i, row in enumerate(rows):
        problems = validate_row(row)
        if problems:
            res.refused += 1
            res.receipts.append(
                {"row": i, "status": REFUSED, "success": False, "reason": "; ".join(problems)}
            )
            continue
        key = row_key(row)
        prior = on_file.get(key)
        if prior is not None:
            if _identity(prior) == _identity(row):
                res.duplicate += 1
                res.receipts.append({"row": i, "status": DUPLICATE_NOOP, "success": True})
            else:
                res.refused += 1
                res.receipts.append(
                    {
                        "row": i,
                        "status": CONFLICT,
                        "success": False,
                        "reason": "same key already on the ledger with different content; "
                        "an existing row is never rewritten",
                    }
                )
            continue
        on_file[key] = row
        new.append(row)
        res.written += 1
        res.receipts.append({"row": i, "status": NEW, "success": True})
    if new:
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with ledger.open("a", encoding="utf-8") as fh:
            for r in new:
                fh.write(json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n")
    return res


def validate_ledger(ledger: Path, wager_keys: set[str] | None = None) -> list[str]:
    """Problems in an existing ledger file. With ``wager_keys`` every row must belong to a known wager."""
    problems: list[str] = []
    seen: set[tuple] = set()
    try:
        rows = read_jsonl(ledger)
    except RowRefused as exc:
        return [str(exc)]
    for n, row in enumerate(rows, 1):
        bad = validate_row(row)
        if bad:
            problems.append(f"row {n}: {'; '.join(bad)}")
            continue
        k = row_key(row)
        if k in seen:
            problems.append(f"row {n}: duplicate key")
        seen.add(k)
        if wager_keys is not None and row["source_bet_key"] not in wager_keys:
            problems.append(f"row {n}: no wager with this source_bet_key (orphan evidence)")
    return problems


# --------------------------------------------------------------------------- recommendation join


def link_recommendation(
    execution: dict, candidates: list[dict], *, window_seconds: float, market_ticker: str, side: str
) -> dict:
    """The LINK row for one EXECUTION row. Exact, unique matches only -- never a guess.

    ``candidates`` come from the sport repository's own decision records. Each must carry ``market_ticker``,
    ``side``, ``created_at_ts`` (epoch), ``recommendation_ref`` and ``model_run_ref`` (32-hex digests of the private
    ids) and ``model_as_of`` (RFC 3339). A candidate matches when market and side are EXACTLY equal and it was created
    BEFORE the first fill and no more than ``window_seconds`` before it. Zero matches -> MISSING. One distinct
    recommendation -> LINKED. Two or more distinct recommendations -> AMBIGUOUS: it is NOT resolved by picking the
    nearest, because choosing between two recommendations for one fill is exactly the guess this ledger must not make.
    """
    if window_seconds <= 0:
        raise ValueError(
            "window_seconds must be positive: there is no default window to hide a guess in"
        )
    first = execution["first_fill_ts"]
    hits = [
        c
        for c in candidates
        if c.get("market_ticker") == market_ticker
        and c.get("side") == side
        and _is_num(c.get("created_at_ts"))
        and 0 <= first - c["created_at_ts"] <= window_seconds
    ]
    refs = sorted({c.get("recommendation_ref") for c in hits})
    basis = {
        "match": "market+side exact; created before first fill",
        "window_seconds": window_seconds,
        "candidates_considered": len(hits),
    }
    base = {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND_LINK,
        "producer": PRODUCER_EDGE_FINDER,
        "source_bet_key": execution["source_bet_key"],
        "link_basis": basis,
    }
    if not hits:
        return {**base, "status": LINK_MISSING}
    if len(refs) > 1:
        return {**base, "status": LINK_AMBIGUOUS}
    pick = max(hits, key=lambda c: c["created_at_ts"])
    return {
        **base,
        "status": LINK_LINKED,
        "recommendation_ref": pick["recommendation_ref"],
        "model_run_ref": pick.get("model_run_ref"),
        "model_as_of": pick["model_as_of"],
    }


# --------------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    imp = sub.add_parser("import", help='append evidence rows from a payload {"evidence": [...]}')
    imp.add_argument("--payload", required=True)
    imp.add_argument(
        "--ledger",
        required=True,
        help="path of the evidence JSONL on the destination's ledger branch",
    )
    imp.add_argument("--receipts-out")
    val = sub.add_parser("validate", help="check an existing evidence ledger")
    val.add_argument("--ledger", required=True)
    val.add_argument(
        "--wagers",
        help="optional wagers JSONL: every evidence row must belong to one of its wagers",
    )
    val.add_argument("--result-out")
    args = ap.parse_args(argv)
    if args.cmd == "import":
        payload = json.loads(Path(args.payload).read_text(encoding="utf-8"))
        rows = payload.get("evidence") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            print('payload must be {"evidence": [...]}', file=sys.stderr)
            return 2
        res = import_rows(Path(args.ledger), rows)
        if args.receipts_out:
            Path(args.receipts_out).write_text(
                json.dumps(
                    {"kind": "execution_evidence", **res.counts(), "rows": res.receipts}, indent=1
                )
                + "\n",
                encoding="utf-8",
            )
        c = res.counts()
        print(
            f"execution evidence: written={c['written']} alreadyPresent={c['alreadyPresent']} refused={c['refused']}"
        )
        return 1 if res.refused else 0
    keys = None
    if args.wagers:
        keys = {r.get("source_bet_key") for r in read_jsonl(Path(args.wagers))}
    problems = validate_ledger(Path(args.ledger), keys)
    if args.result_out:
        Path(args.result_out).write_text(
            json.dumps({"problems": len(problems)}) + "\n", encoding="utf-8"
        )
    print(f"execution evidence ledger: problems={len(problems)}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
