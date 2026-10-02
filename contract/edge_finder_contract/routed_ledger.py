"""The shared destination accounting ledger for routed Kalshi wagers and their settlements.

This is the generalisation of NHL-edge-finder's ``nhl_edge.accounting.ledger`` (the pattern the
router already delivers to in production), parameterised by sport so NBA, TENNIS and any later
sport get an importer with the same contract rather than a seventh hand-written one:

    payload   {"importBatchId": str, "rows": [<router row>]}   /   {"settlements": [<router row>]}
    identity  wager_id = "<prefix>w-" + sha256(source_bet_key)[:24]; settlement_id likewise with "s"
    verdicts  NEW | DUPLICATE_NOOP | CONFLICT | REFUSED, one receipt per row, in the vocabulary
              ``kalshi_router.receipts.normalise`` reads
    store     append-only JSONL under ``data/accounting/`` on the destination's ledger branch
    privacy   stdout carries counts and reasons; never a ticker, price, stake or key

A wager row says the owner placed a bet. It carries no model provenance, and a row that tries to
(``recommendation_id``, ``model_probability`` ...) is refused rather than stored.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

ECONOMICS_V2 = "router-settlement-economics.v2"
ENTRY_METHOD = "IMPORTED_RECEIPT"
VENUE = "kalshi"
NEW, DUPLICATE_NOOP, CONFLICT, REFUSED = "NEW", "DUPLICATE_NOOP", "CONFLICT", "REFUSED"

#: The snake_case router dialect (``to_cfb_import_row`` / ``to_nhl_import_row``), plus the two
#: optional evidence fields the NFL builder adds. ``actual_price`` and ``execution_price`` name
#: the same quantity-weighted price; a row carries exactly one.
WAGER_INPUT_FIELDS = ("source_bet_key", "import_batch_id", "entry_method", "game_date", "market_ticker", "side",
                      "executed_at", "contracts", "execution_price", "actual_price", "stake", "fees_paid",
                      "fees_are_estimated", "venue", "fee_state", "execution_action")
WAGER_REQUIRED = ("source_bet_key", "import_batch_id", "entry_method", "game_date", "market_ticker", "side",
                  "executed_at", "contracts", "stake", "fees_paid", "fees_are_estimated", "venue")
WAGER_CANONICAL_FIELDS = ("entry_method", "game_date", "market_ticker", "side", "executed_at", "contracts",
                          "execution_price", "stake", "fees_paid", "fees_are_estimated", "venue")
SETTLEMENT_INPUT_FIELDS = ("source_bet_key", "market_ticker", "side", "settlement_status", "settled_at", "result",
                           "gross_return", "net_profit_loss", "refusals", "venue", "economics_version")
SETTLEMENT_CANONICAL_FIELDS = tuple(f for f in SETTLEMENT_INPUT_FIELDS if f != "source_bet_key")
PROVENANCE_FIELDS = frozenset({
    "recommendation_id", "recommendation", "model_version", "model_evaluation_id", "model_fair_probability",
    "model_probability", "model_supported", "p_model", "fair_probability", "fair_prob", "edge", "expected_value",
    "ev", "projection_id", "prediction_id", "prediction_record_id", "thesis_id", "authority", "confidence",
    "kelly", "stake_recommendation", "gate", "qualification", "readiness", "decision_id",
})
_PROVENANCE_RE = re.compile(r"^(model_|fair_|edge_|probabilit|recommendation|prediction_|thesis|authority|confidence)")
MONEY_TOLERANCE = 1e-9
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass(frozen=True)
class LedgerSpec:
    sport: str
    id_prefix: str
    """e.g. ``nba``: wagers mint ``nbaw-<24hex>``, settlements ``nbas-<24hex>``."""
    wager_schema: str
    settlement_schema: str
    ledger_dir: str = "data/accounting"
    wagers_file: str = "wagers.jsonl"
    settlements_file: str = "settlements.jsonl"

    def wagers_path(self, base: Path) -> Path:
        return Path(base) / self.ledger_dir / self.wagers_file

    def settlements_path(self, base: Path) -> Path:
        return Path(base) / self.ledger_dir / self.settlements_file

    def mint_wager_id(self, source_bet_key: Any) -> str:
        return _mint(f"{self.id_prefix}w", source_bet_key)

    def mint_settlement_id(self, source_bet_key: Any) -> str:
        return _mint(f"{self.id_prefix}s", source_bet_key)


class RowRefused(ValueError):
    """This row cannot be filed, and filing it anyway would be worse."""


def _mint(prefix: str, source_bet_key: Any) -> str:
    if not isinstance(source_bet_key, str) or not source_bet_key.strip():
        raise RowRefused("source_bet_key is required")
    return f"{prefix}-{hashlib.sha256(source_bet_key.encode('utf-8')).hexdigest()[:24]}"


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v))


def _aware_ts(v: Any) -> bool:
    if not isinstance(v, str) or "T" not in v:
        return False
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return False
    return dt.tzinfo is not None


def _nonempty(v: Any) -> bool:
    return isinstance(v, str) and bool(v.strip())


def provenance_fields(row: dict) -> list[str]:
    return sorted(k for k in row if k in PROVENANCE_FIELDS or _PROVENANCE_RE.match(str(k)))


def validate_wager(spec: LedgerSpec, rec: dict) -> list[str]:
    p: list[str] = []
    bad = provenance_fields(rec)
    if bad:
        p.append(f"model/recommendation provenance field(s) {bad} are not accounting facts")
    for name in ("wager_id", "source_bet_key", "import_batch_id", "market_ticker", "game_date", "executed_at"):
        if not _nonempty(rec.get(name)):
            p.append(f"{name} is required")
    if rec.get("schema_version") != spec.wager_schema:
        p.append(f"schema_version must be {spec.wager_schema}")
    if _nonempty(rec.get("source_bet_key")) and rec.get("wager_id") != spec.mint_wager_id(rec["source_bet_key"]):
        p.append("wager_id is not the id this ledger mints from source_bet_key")
    if rec.get("entry_method") != ENTRY_METHOD:
        p.append(f"entry_method must be {ENTRY_METHOD}")
    if rec.get("side") not in ("YES", "NO"):
        p.append("side must be YES or NO")
    if _nonempty(rec.get("game_date")) and not _DATE.match(rec["game_date"]):
        p.append("game_date must be YYYY-MM-DD")
    if _nonempty(rec.get("executed_at")) and not _aware_ts(rec["executed_at"]):
        p.append("executed_at must be an RFC 3339 timestamp with a zone")
    c, px, st, fee = rec.get("contracts"), rec.get("execution_price"), rec.get("stake"), rec.get("fees_paid")
    if not _is_number(c) or c <= 0:
        p.append("contracts must be a positive number")
    if not _is_number(px) or not (0 < px < 1):
        p.append("execution_price must be a number strictly between 0 and 1 (dollars per contract)")
    if not _is_number(st) or st <= 0:
        p.append("stake must be a positive number")
    if not _is_number(fee) or fee < 0:
        p.append("fees_paid must be a non-negative number")
    if rec.get("fees_are_estimated") is not False:
        p.append("fees_are_estimated must be false: only the exchange's own reported fees are accepted")
    if rec.get("venue") != VENUE:
        p.append("venue must be kalshi")
    if rec.get("execution_action") not in (None, "BUY", "SELL"):
        p.append("execution_action must be BUY, SELL or absent")
    if all(_is_number(x) for x in (c, px, st, fee)) and c > 0:
        if abs(st - (c * px + fee)) > 1e-6 * max(1.0, st):
            p.append("stake does not equal contracts x execution_price + fees_paid")
    return p


def validate_settlement(spec: LedgerSpec, rec: dict) -> list[str]:
    p: list[str] = []
    bad = provenance_fields(rec)
    if bad:
        p.append(f"model/recommendation provenance field(s) {bad} are not accounting facts")
    for name in ("settlement_id", "source_bet_key", "market_ticker", "settled_at"):
        if not _nonempty(rec.get(name)):
            p.append(f"{name} is required")
    if rec.get("schema_version") != spec.settlement_schema:
        p.append(f"schema_version must be {spec.settlement_schema}")
    if _nonempty(rec.get("source_bet_key")) and rec.get("settlement_id") != spec.mint_settlement_id(rec["source_bet_key"]):
        p.append("settlement_id is not the id this ledger mints from source_bet_key")
    if rec.get("side") not in ("YES", "NO"):
        p.append("side must be YES or NO")
    if rec.get("settlement_status") != "SETTLED":
        p.append("settlement_status must be SETTLED (an unsettled wager simply has no settlement row)")
    if _nonempty(rec.get("settled_at")) and not _aware_ts(rec["settled_at"]):
        p.append("settled_at must be an RFC 3339 timestamp with a zone")
    if rec.get("result") not in ("WON", "LOST", None):
        p.append("result must be WON, LOST or absent")
    if rec.get("economics_version") != ECONOMICS_V2:
        p.append(f"economics_version must be {ECONOMICS_V2}")
    if rec.get("venue") != VENUE:
        p.append("venue must be kalshi")
    refusals = rec.get("refusals")
    if not isinstance(refusals, list) or not all(isinstance(r, str) for r in refusals):
        p.append("refusals must be a list of strings")
        refusals = []
    g, n = rec.get("gross_return"), rec.get("net_profit_loss")
    for name, v in (("gross_return", g), ("net_profit_loss", n)):
        if v is not None and not _is_number(v):
            p.append(f"{name} must be a number or absent")
    if g is not None and _is_number(g) and g < 0:
        p.append("gross_return may not be negative")
    established = g is not None and n is not None
    if established and refusals:
        p.append("a settlement with established money may not also carry refusals")
    if not established and not refusals:
        p.append("a settlement without established money must say why (refusals)")
    return p


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RowRefused(f"{path.name} line {i} is not JSON") from exc
        if not isinstance(row, dict):
            raise RowRefused(f"{path.name} line {i} is not an object")
        out.append(row)
    return out


def _line(rec: dict) -> str:
    return json.dumps(rec, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"


def _append(path: Path, recs: list[dict]) -> None:
    if not recs:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_bytes() if path.exists() else b""
    with path.open("a", encoding="utf-8") as fh:
        if existing and not existing.endswith(b"\n"):
            fh.write("\n")
        for r in recs:
            fh.write(_line(r))


def _differs(a: Any, b: Any) -> bool:
    if _is_number(a) and _is_number(b):
        return abs(float(a) - float(b)) > MONEY_TOLERANCE
    return a != b


def conflicting_fields(existing: dict, incoming: dict, fields: tuple[str, ...]) -> list[str]:
    return [f for f in fields if _differs(existing.get(f), incoming.get(f))]


def build_wager(spec: LedgerSpec, row: Any) -> dict:
    if not isinstance(row, dict):
        raise RowRefused("row is not an object")
    prov = provenance_fields(row)
    if prov:
        raise RowRefused(f"model/recommendation provenance field(s) {prov} are not accounting facts")
    unknown = sorted(set(row) - set(WAGER_INPUT_FIELDS))
    if unknown:
        raise RowRefused(f"unknown field(s) {unknown}")
    missing = [f for f in WAGER_REQUIRED if f not in row]
    if missing:
        raise RowRefused(f"missing field(s) {missing}")
    prices = [f for f in ("execution_price", "actual_price") if f in row]
    if len(prices) != 1:
        raise RowRefused("a row carries exactly one of execution_price / actual_price")
    rec = {k: row[k] for k in WAGER_INPUT_FIELDS if k in row and k != "actual_price"}
    rec["execution_price"] = row[prices[0]]
    rec["wager_id"] = spec.mint_wager_id(row.get("source_bet_key"))
    rec["schema_version"] = spec.wager_schema
    problems = validate_wager(spec, rec)
    if problems:
        raise RowRefused("; ".join(problems))
    return rec


@dataclass
class ImportResult:
    rows: list[dict] = field(default_factory=list)
    written: int = 0
    duplicate: int = 0
    refused: int = 0

    @property
    def conflicted(self) -> bool:
        return self.refused > 0

    def receipts(self, kind: str, import_batch_id: str | None = None) -> dict:
        out = {"kind": kind, "written": self.written, "alreadyPresent": self.duplicate, "refused": self.refused,
               "rows": self.rows}
        if import_batch_id is not None:
            out["importBatchId"] = import_batch_id
        return out


def _receipt(index: int, key: Any, id_field: str, ident: str | None, status: str, reason: str | None = None,
             fields: list[str] | None = None) -> dict:
    r = {"row": index, "source_bet_key": key if isinstance(key, str) else None, id_field: ident, "status": status,
         "duplicate_status": status, "success": status in (NEW, DUPLICATE_NOOP)}
    if reason:
        r["reason"] = reason
    if fields:
        r["conflicting_fields"] = list(fields)
    return r


def import_wagers(spec: LedgerSpec, base_dir: Path, rows: list, import_batch_id: str | None = None) -> ImportResult:
    path = spec.wagers_path(base_dir)
    on_file = {r.get("source_bet_key"): r for r in read_jsonl(path)}
    res = ImportResult()
    new: list[dict] = []
    for i, row in enumerate(rows):
        key = row.get("source_bet_key") if isinstance(row, dict) else None
        try:
            if import_batch_id is not None and isinstance(row, dict) and row.get("import_batch_id") != import_batch_id:
                raise RowRefused("row import_batch_id disagrees with the payload envelope")
            rec = build_wager(spec, row)
        except RowRefused as exc:
            res.refused += 1
            res.rows.append(_receipt(i, key, "wager_id", None, REFUSED, str(exc)))
            continue
        prior = on_file.get(rec["source_bet_key"])
        if prior is not None:
            diff = conflicting_fields(prior, rec, WAGER_CANONICAL_FIELDS)
            if diff:
                res.refused += 1
                res.rows.append(_receipt(i, key, "wager_id", rec["wager_id"], CONFLICT,
                                         "this order is already on the ledger with different economics; "
                                         "an existing row is never rewritten", diff))
            else:
                res.duplicate += 1
                res.rows.append(_receipt(i, key, "wager_id", rec["wager_id"], DUPLICATE_NOOP))
            continue
        on_file[rec["source_bet_key"]] = rec
        new.append(rec)
        res.written += 1
        res.rows.append(_receipt(i, key, "wager_id", rec["wager_id"], NEW))
    _append(path, new)
    return res


def build_settlement(spec: LedgerSpec, row: Any) -> dict:
    if not isinstance(row, dict):
        raise RowRefused("row is not an object")
    prov = provenance_fields(row)
    if prov:
        raise RowRefused(f"model/recommendation provenance field(s) {prov} are not accounting facts")
    unknown = sorted(set(row) - set(SETTLEMENT_INPUT_FIELDS))
    if unknown:
        raise RowRefused(f"unknown field(s) {unknown}")
    missing = [f for f in SETTLEMENT_INPUT_FIELDS if f not in row and f not in ("result", "gross_return", "net_profit_loss", "economics_version")]
    if missing:
        raise RowRefused(f"missing field(s) {missing}")
    rec = {k: row.get(k) for k in SETTLEMENT_INPUT_FIELDS}
    rec["refusals"] = list(rec["refusals"]) if isinstance(rec["refusals"], (list, tuple)) else rec["refusals"]
    rec["settlement_id"] = spec.mint_settlement_id(row.get("source_bet_key"))
    rec["schema_version"] = spec.settlement_schema
    problems = validate_settlement(spec, rec)
    if problems:
        raise RowRefused("; ".join(problems))
    return rec


def import_settlements(spec: LedgerSpec, base_dir: Path, rows: list) -> ImportResult:
    wagers = {r.get("source_bet_key"): r for r in read_jsonl(spec.wagers_path(base_dir))}
    path = spec.settlements_path(base_dir)
    on_file = {r.get("source_bet_key"): r for r in read_jsonl(path)}
    res = ImportResult()
    new: list[dict] = []
    for i, row in enumerate(rows):
        key = row.get("source_bet_key") if isinstance(row, dict) else None
        try:
            rec = build_settlement(spec, row)
            wager = wagers.get(rec["source_bet_key"])
            if wager is None:
                raise RowRefused(f"ORPHAN: no wager with this source_bet_key is on the {spec.sport} wager ledger")
            if wager.get("market_ticker") != rec["market_ticker"] or wager.get("side") != rec["side"]:
                raise RowRefused("settlement market/side disagree with the wager it settles")
        except RowRefused as exc:
            res.refused += 1
            res.rows.append(_receipt(i, key, "settlement_id", None, REFUSED, str(exc)))
            continue
        prior = on_file.get(rec["source_bet_key"])
        if prior is not None:
            diff = conflicting_fields(prior, rec, SETTLEMENT_CANONICAL_FIELDS)
            if diff:
                res.refused += 1
                res.rows.append(_receipt(i, key, "settlement_id", rec["settlement_id"], CONFLICT,
                                         "this wager already has a different settlement; a market settles once "
                                         "and an existing row is never rewritten", diff))
            else:
                res.duplicate += 1
                res.rows.append(_receipt(i, key, "settlement_id", rec["settlement_id"], DUPLICATE_NOOP))
            continue
        on_file[rec["source_bet_key"]] = rec
        new.append(rec)
        res.written += 1
        res.rows.append(_receipt(i, key, "settlement_id", rec["settlement_id"], NEW))
    _append(path, new)
    return res


def validate_ledger(spec: LedgerSpec, base_dir: Path, base_texts: dict[str, str] | None = None) -> dict:
    """Whole-ledger check: schema per row, unique keys, every settlement has its wager, and (when
    ``base_texts`` gives the ledger at a base ref) append-only. Reasons never quote values."""
    failures: list[str] = []
    counts = {"wagers": 0, "settlements": 0}
    rows_by: dict[str, list[dict]] = {}
    for label, path, validator, fields in (
        ("wagers", spec.wagers_path(base_dir), validate_wager, WAGER_INPUT_FIELDS + ("wager_id", "schema_version")),
        ("settlements", spec.settlements_path(base_dir), validate_settlement, SETTLEMENT_INPUT_FIELDS + ("settlement_id", "schema_version")),
    ):
        rel = path.relative_to(Path(base_dir)) if path.is_relative_to(Path(base_dir)) else path
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        rows: list[dict] = []
        keys: set[str] = set()
        for i, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                failures.append(f"{rel} line {i}: blank line")
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                failures.append(f"{rel} line {i}: not JSON")
                continue
            if not isinstance(row, dict):
                failures.append(f"{rel} line {i}: not an object")
                continue
            for reason in validator(spec, row):
                failures.append(f"{rel} line {i}: {reason}")
            unknown = sorted(set(row) - set(fields))
            if unknown:
                failures.append(f"{rel} line {i}: unknown field(s) {unknown}")
            key = row.get("source_bet_key")
            if key in keys:
                failures.append(f"{rel} line {i}: duplicate source_bet_key")
            keys.add(key)
            rows.append(row)
        rows_by[label] = rows
        counts[label] = len(rows)
        base = (base_texts or {}).get(str(rel).replace("\\", "/"))
        if base is not None and not text.startswith(base):
            failures.append(f"{rel}: not append-only against the base ref")
    wager_keys = {r.get("source_bet_key") for r in rows_by.get("wagers", [])}
    for i, s in enumerate(rows_by.get("settlements", []), 1):
        if s.get("source_bet_key") not in wager_keys:
            failures.append(f"settlements row {i}: ORPHAN settlement (no wager with its key)")
    return {"ok": not failures, "counts": counts, "failures": failures}


# ------------------------------------------------------------------- CLI glue (shared by every importer script)

EXIT_OK, EXIT_REFUSED, EXIT_BAD_INPUT = 0, 1, 2


def read_payload(path: str, kind: str) -> tuple[str | None, list]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("payload is not an object")
    if kind == "wagers":
        batch = payload.get("importBatchId")
        if not isinstance(batch, str) or not batch.strip():
            raise ValueError("payload carries no importBatchId")
        rows = payload.get("rows")
    else:
        batch = payload.get("importBatchId")
        rows = payload.get("settlements", payload.get("rows"))
    if not isinstance(rows, list):
        raise ValueError("payload carries no rows list")
    return batch, rows


def run_import_cli(spec: LedgerSpec, kind: str, argv: list[str] | None = None) -> int:
    """``kind`` is ``wagers`` or ``settlements``. Prints counts only."""
    import argparse
    import sys

    ap = argparse.ArgumentParser(description=f"import routed {kind} into the {spec.sport} accounting ledger (counts only)")
    ap.add_argument("--payload", required=True)
    ap.add_argument("--base-dir", required=True, help="a checkout of the ledger branch")
    ap.add_argument("--receipts-out", default=None)
    ap.add_argument("--season", default=None, help="accepted and ignored: this ledger is not season-partitioned")
    a = ap.parse_args(argv)
    try:
        batch, rows = read_payload(a.payload, kind)
        if kind == "wagers":
            result = import_wagers(spec, Path(a.base_dir), rows, import_batch_id=batch)
        else:
            result = import_settlements(spec, Path(a.base_dir), rows)
    except (OSError, ValueError, RowRefused) as exc:
        print(f"unreadable payload or ledger: {type(exc).__name__}", file=sys.stderr)
        return EXIT_BAD_INPUT
    print(f"rows in payload: {len(rows)}")
    print(f"  written:         {result.written}")
    print(f"  already present: {result.duplicate}")
    print(f"  refused:         {result.refused}")
    for r in result.rows:
        if not r["success"]:
            fields = f" fields={r['conflicting_fields']}" if r.get("conflicting_fields") else ""
            print(f"    row {r['row']}: {r['status']}: {r.get('reason')}{fields}")
    if a.receipts_out:
        Path(a.receipts_out).write_text(json.dumps(result.receipts(kind, batch), indent=2, sort_keys=True) + "\n")
    return EXIT_REFUSED if result.conflicted else EXIT_OK


def run_validate_cli(spec: LedgerSpec, argv: list[str] | None = None) -> int:
    import argparse
    import subprocess

    ap = argparse.ArgumentParser(description=f"validate the {spec.sport} accounting ledger (counts and reasons only)")
    ap.add_argument("--base-dir", required=True)
    ap.add_argument("--base-ref", default=None, help="git ref to prove append-only against")
    ap.add_argument("--result-out", default=None)
    a = ap.parse_args(argv)
    base_texts = None
    if a.base_ref:
        base_texts = {}
        for rel in (f"{spec.ledger_dir}/{spec.wagers_file}", f"{spec.ledger_dir}/{spec.settlements_file}"):
            proc = subprocess.run(["git", "-C", a.base_dir, "show", f"{a.base_ref}:{rel}"], capture_output=True, text=True)
            if proc.returncode == 0:
                base_texts[rel] = proc.stdout
    result = validate_ledger(spec, Path(a.base_dir), base_texts)
    print(f"{spec.sport} ledger: wagers={result['counts']['wagers']} settlements={result['counts']['settlements']} "
          f"problems={len(result['failures'])}")
    for f in result["failures"][:50]:
        print(f"  {f}")
    if a.result_out:
        Path(a.result_out).write_text(json.dumps({"passed": result["ok"], **result}, indent=2, sort_keys=True) + "\n")
    return EXIT_OK if result["ok"] else EXIT_REFUSED
