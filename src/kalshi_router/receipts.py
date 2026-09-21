"""One vocabulary for what a destination's importer said it did.

*** THE PROBLEM ***
Each destination's importer reports in its own words, and they genuinely
differ. MLB writes a LIST of per-row receipts, camelCase, with
``duplicateStatus`` in {NEW, DUPLICATE_NOOP, CORRECTED, CONFLICT} and a minted
``betId``. The CFB importer writes an OBJECT of counts with a ``rows`` list
beside it, snake_case, and mints ``wager_id`` deterministically from
``source_bet_key``.

The auto-merge gate has to answer the same four questions of both:

  * did the importer refuse anything?
  * did any row need a human's judgement?
  * did a second identical import change nothing?
  * did every row come back with a canonical identity, and the SAME one?

So the shapes are normalised here, once, and the gate reasons about
:class:`Receipt` alone. Teaching the gate two vocabularies would mean two
branches through the most safety-critical code in the repository.

*** NORMALISING IS NOT INTERPRETING ***
A verdict this module does not recognise stays as it is, and the gate treats an
unrecognised verdict as needing judgement. Mapping an unknown word onto the
nearest known one is how a refusal becomes a merge.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: Verdicts meaning "the destination accepted this row without anyone's help".
NO_JUDGEMENT_VERDICTS = frozenset({"NEW", "DUPLICATE_NOOP", "CORRECTED"})

#: What a SECOND, identical import must return for every row. This is the
#: idempotency proof: a `NEW` here would mean a row's identity is not a
#: function of the row.
IDEMPOTENT_RERUN_VERDICTS = frozenset({"DUPLICATE_NOOP"})


@dataclass(frozen=True)
class Receipt:
    """What one importer said about one row."""

    source_key: str | None
    identity: str | None
    """The destination's own canonical id for the row. Absent means the
    destination did not name it, which is a row nobody can refer to later."""
    verdict: str | None
    success: bool
    conflicting_fields: tuple[str, ...] = ()
    reason: str | None = None

    @property
    def needs_judgement(self) -> bool:
        return self.verdict not in NO_JUDGEMENT_VERDICTS

    def as_dict(self) -> dict[str, Any]:
        return {
            "sourceBetKey": self.source_key,
            "betId": self.identity,
            "duplicateStatus": self.verdict,
            "success": self.success,
            "conflictingFields": [{"field": f} for f in self.conflicting_fields],
            "reason": self.reason,
        }


def _first(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if row.get(key) is not None:
            return row[key]
    return None


def _from_row(row: dict[str, Any]) -> Receipt:
    conflicting = row.get("conflictingFields") or row.get("conflicting_fields") or []
    fields: list[str] = []
    for entry in conflicting:
        if isinstance(entry, dict):
            name = entry.get("field") or entry.get("name")
            if name:
                fields.append(str(name))
        elif entry:
            fields.append(str(entry))
    verdict = _first(row, "duplicateStatus", "duplicate_status", "status")
    return Receipt(
        source_key=_first(row, "sourceBetKey", "source_bet_key"),
        identity=_first(row, "betId", "bet_id", "wager_id", "wagerId", "settlement_id"),
        verdict=str(verdict) if verdict is not None else None,
        # `success` absent is read as TRUE only when a verdict is present: an
        # importer that named a verdict and no failure flag reported a written
        # row. A receipt with neither is not a success, it is an unreadable
        # receipt, and the gate must not merge on it.
        success=bool(row.get("success", verdict is not None)),
        conflicting_fields=tuple(sorted(set(fields))),
        reason=_first(row, "reason", "refusal", "error"),
    )


def normalise(payload: Any) -> tuple[Receipt, ...]:
    """Every destination's receipts, in one shape.

    Accepts:
      * a LIST of per-row receipts (MLB, and CFB's `rows`);
      * an OBJECT carrying `rows`;
      * an OBJECT of counts with `keysWritten` and `refusals` and no rows --
        the CFB importer's older shape, kept readable so the landing order of
        two repositories' pull requests cannot break delivery.

    Returns an empty tuple for anything it cannot read. The gate WAITS on empty
    receipts rather than merging, so an unreadable shape costs a cycle instead
    of a wrong merge.
    """
    if isinstance(payload, list):
        return tuple(_from_row(row) for row in payload if isinstance(row, dict))

    if not isinstance(payload, dict):
        return ()

    rows = payload.get("rows")
    if isinstance(rows, list):
        return tuple(_from_row(row) for row in rows if isinstance(row, dict))

    # The counts-only shape. Every key the importer WROTE is a NEW row; the
    # rest of the batch was already present, which is a DUPLICATE_NOOP. The
    # identity is unknown from this shape and is reported as unknown rather
    # than invented -- the gate's identity condition then fails closed, which
    # is the correct outcome for a destination that will not name its rows.
    written = payload.get("keysWritten")
    if isinstance(written, list):
        receipts = [
            Receipt(source_key=str(key), identity=None, verdict="NEW", success=True)
            for key in written
        ]
        already = int(payload.get("alreadyPresent") or 0)
        receipts.extend(
            Receipt(source_key=None, identity=None, verdict="DUPLICATE_NOOP", success=True)
            for _ in range(already)
        )
        for entry in payload.get("refusals") or []:
            reason = (
                entry.get("reason")
                if isinstance(entry, dict)
                else str(entry)
            )
            receipts.append(
                Receipt(
                    source_key=None,
                    identity=None,
                    verdict="REFUSED",
                    success=False,
                    reason=str(reason) if reason else None,
                )
            )
        return tuple(receipts)

    return ()


def verdict_counts(receipts: tuple[Receipt, ...]) -> dict[str, int]:
    """Counts only. Safe for a public log: no ticker, price, stake or key."""
    out: dict[str, int] = {}
    for receipt in receipts:
        out[str(receipt.verdict)] = out.get(str(receipt.verdict), 0) + 1
    return dict(sorted(out.items()))


def conflicting_field_names(receipts: tuple[Receipt, ...]) -> tuple[str, ...]:
    """FIELD NAMES ONLY, never their values.

    A refusal nobody can identify is a refusal nobody can resolve -- and every
    scheduled run will refuse the same row until somebody does. Names make that
    actionable; the receipt's `existing` and `incoming` VALUES stay out of a
    public log, exactly like the stake and the ticker."""
    return tuple(sorted({f for r in receipts for f in r.conflicting_fields}))
