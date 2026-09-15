"""The delivery contract: what a batch is, and what makes re-sending it safe.

Phase H. This module assembles the payload a downstream ledger's importer
accepts. **There is deliberately no transport here** -- no HTTP client, no git
push, no dispatch. Two things must be settled by the owner before any code that
can write downstream should exist at all, and both are recorded in
``docs/DELIVERY.md``:

1. a credential scoped to exactly one repository, which only the owner can
   create;
2. whether these rows may be published at all -- every destination repository
   is PUBLIC, and a canonical wager row carries stake, entry price, contracts,
   fees and payout.

Building the payload is safe and useful without either: it is what lets the
contract be tested, and it is what makes the credential's required scope
precise rather than approximate.

The router never writes the ledger file
---------------------------------------
MLB's own rule is that **all** writes go through
``lib.edgelab.bets.write_placed_bet``. A cross-repo commit straight into
``data/edgelab/bets/bets.jsonl`` would bypass the importer's duplicate
detection, its ticker resolution and its validation -- exactly the properties
that make a re-run safe. So the unit of delivery is a BATCH PAYLOAD handed to
the destination's own importer, never a file write.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .sports import Sport
from .wager import ShadowWager

#: The batch label every router-produced row carries, forever.
#:
#: This is deliberately a CONSTANT, and the reasoning matters more than the
#: value. The destination derives a row's identity from
#: ``hash(importBatchId, sourceBetKey, marketTicker, side)``, so ``importBatchId``
#: is part of the primary key. Two tempting choices are both wrong:
#:
#: * a TIMESTAMP ("router-2026-09-14T19:00") gives the same wager a different
#:   identity on every run, so every re-run duplicates the entire ledger;
#: * a CONTENT DIGEST of the batch is worse in a subtler way -- it is stable
#:   while the batch is, and then adding one new wager changes the id and
#:   re-imports every existing row as new.
#:
#: A constant makes a row's identity depend only on the row, which is what
#: "re-running an identical batch is a no-op" actually requires. It is versioned
#: so a future deliberate re-identification is possible and explicit.
ROUTER_IMPORT_BATCH_ID = "kalshi-router-v1"

#: Where each sport's wagers would go. Only MLB has an importer (Phase F read
#: all four repositories), so it is the only entry -- and a sport absent from
#: this map is refused rather than defaulted somewhere plausible.
DESTINATION_REPOS: dict[Sport, str] = {
    Sport.MLB: "chmoses98/edge-finder-api",
}


@dataclass
class DeliveryPlan:
    """What WOULD be sent, in counts. Never the payload itself.

    SENSITIVE data does not reach this object: it holds totals and a repository
    name, both of which are already public facts about the system.
    """

    destination: str
    import_batch_id: str
    rows: int = 0

    def render(self) -> str:
        return "\n".join([
            f"  destination: {self.destination}",
            f"  import batch id: {self.import_batch_id}",
            f"  rows that would be delivered: {self.rows}",
        ])


def write_payloads(wagers, out_dir: str) -> dict[str, int]:
    """Write one PRODUCTION importer payload per destination. Counts only.

    Production routing is gated on :data:`DESTINATION_REPOS`, which is the set
    of sports the scheduled delivery workflow knows how to push to. A wager for
    any other sport should have been refused long before here.
    """
    return _write_payloads(
        wagers,
        out_dir,
        import_batch_id=ROUTER_IMPORT_BATCH_ID,
        allowed_sports=_sport_names(),
        why="delivery",
    )


def write_backfill_payloads(wagers, out_dir: str, import_batch_id: str, allowed_sports) -> dict[str, int]:
    """Write one HISTORICAL importer payload per destination. Counts only.

    Deliberately NOT gated on :data:`DESTINATION_REPOS`. That map is what the
    every-15-minutes production job is willing to push to, and widening it would
    change what production routes -- a much larger decision than catching up on
    a bounded window in the past. The gate here is the set of destinations the
    caller actually supplied a ledger for, which is the same set reconciliation
    formed its verdicts against. Writing to a destination whose existing rows
    were never read is how a backfill duplicates a ledger.
    """
    return _write_payloads(
        wagers,
        out_dir,
        import_batch_id=import_batch_id,
        allowed_sports=frozenset(allowed_sports),
        why="backfill delivery",
    )


def _write_payloads(wagers, out_dir: str, *, import_batch_id: str, allowed_sports, why: str) -> dict[str, int]:
    """One importer payload per destination, each in that destination's words.

    The payload is the sensitive artefact of this whole system -- it carries
    market, side, stake, contracts, price, fees. It goes to a FILE, never to
    stdout and never to a log, because the only place it is allowed to arrive is
    the destination ledger the owner chose to publish.

    Returns ``{sport: row count}``. That mapping is safe to print.
    """
    import json
    import os

    from .production import ROW_BUILDERS

    if not isinstance(import_batch_id, str) or not import_batch_id.strip():
        raise ValueError("a payload needs an import batch id; the destination's row identity depends on it")

    grouped: dict[str, list] = {}
    for wager in wagers:
        if wager.sport not in allowed_sports:
            raise ValueError(
                f"a wager reached {why} for a sport with no destination "
                "importer; it should have been refused earlier"
            )
        if wager.sport not in ROW_BUILDERS:
            # Reaching here means a sport was declared deliverable without
            # anyone writing its row shape. Sending it in another sport's
            # vocabulary would land a wager with no price.
            raise ValueError(
                f"{wager.sport} has no payload shape; a row cannot be built for it"
            )
        grouped.setdefault(wager.sport, []).append(wager)

    os.makedirs(out_dir, exist_ok=True)
    counts: dict[str, int] = {}
    for sport, sport_wagers in sorted(grouped.items()):
        # Sorted by the ROUTER's own source key rather than by a field of the
        # emitted row: every destination spells that field differently, and the
        # ordering must not depend on which one this is. The same set of wagers
        # therefore always produces a byte-identical payload, which is what lets
        # a human confirm that a second run really did propose nothing new.
        sport_wagers.sort(key=lambda wager: wager.source_key)
        rows = [ROW_BUILDERS[sport](wager, import_batch_id) for wager in sport_wagers]
        payload = {"importBatchId": import_batch_id, "rows": rows}
        path = os.path.join(out_dir, f"{sport}.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        counts[sport] = len(rows)
    return counts


def _sport_names() -> frozenset[str]:
    return frozenset(sport.value for sport in DESTINATION_REPOS)


def destination_repo_for(sport_name: str) -> str | None:
    """The repository a sport's wagers go to, by name."""
    for sport, repo in DESTINATION_REPOS.items():
        if sport.value == sport_name:
            return repo
    return None


def build_batch(wagers: list[ShadowWager]) -> dict[str, Any]:
    """Assemble one importer payload.

    Rows are ordered by ``sourceBetKey`` so the same set of wagers always
    produces a byte-identical payload. That is not cosmetic: a diffable,
    reproducible payload is what lets a human confirm that a second run really
    did propose nothing new.
    """
    rows = sorted(
        (wager.to_import_row() for wager in wagers),
        key=lambda row: row["sourceBetKey"],
    )
    return {"importBatchId": ROUTER_IMPORT_BATCH_ID, "rows": rows}


def plan_delivery(wagers: list[ShadowWager]) -> list[DeliveryPlan]:
    """Group wagers by destination and report what each batch would contain.

    A wager whose sport has no destination is not silently dropped -- it cannot
    reach this function, because :func:`~kalshi_router.wager.build_shadow_wager`
    already refused it with ``NO_DESTINATION_IMPORTER``. This asserts that
    invariant rather than trusting it.
    """
    by_sport: dict[Sport, int] = {}
    for wager in wagers:
        if wager.sport not in DESTINATION_REPOS:
            raise ValueError(
                "a wager reached delivery planning for a sport with no "
                "destination importer; it should have been refused earlier"
            )
        by_sport[wager.sport] = by_sport.get(wager.sport, 0) + 1

    return [
        DeliveryPlan(
            destination=DESTINATION_REPOS[sport],
            import_batch_id=ROUTER_IMPORT_BATCH_ID,
            rows=count,
        )
        for sport, count in sorted(by_sport.items(), key=lambda item: item[0].value)
    ]
