"""Exact-match registry of Kalshi sports series tickers.

This registry is deliberately **exact match only**.  It is not a prefix table and
it is not a pattern matcher: ``KXNFLGAME`` matches ``KXNFLGAME`` and nothing else.
That restriction is what makes ticker evidence deterministic enough to be treated
as authoritative, and it is why a ticker this registry does not know produces
``UNRESOLVED`` rather than a guess.

Verification status
-------------------
Entries marked ``verified=False`` were derived from Kalshi's published series
naming conventions but were **not** confirmed against a live ``GET /series/{ticker}``
response while this module was written.  An unverified entry can only ever fail
to match (costing a classification, never corrupting one), because a ticker that
does not exist is never returned by the API.  Audits report how many
classifications leaned on an unverified entry so the owner can confirm or prune
this table from real data.

Series metadata (tags/category/title) always outranks this table conceptually;
the classifier treats a disagreement between the two as unresolvable.
"""

from __future__ import annotations

from dataclasses import dataclass

from .sports import Sport


@dataclass(frozen=True)
class SeriesEntry:
    sport: Sport
    verified: bool


#: Exact Kalshi series tickers to sport.  Keys are compared case-insensitively
#: after stripping, but are otherwise matched literally.
SERIES_TICKER_REGISTRY: dict[str, SeriesEntry] = {
    # --- MLB ---------------------------------------------------------------
    "KXMLBGAME": SeriesEntry(Sport.MLB, verified=False),
    "KXMLBSERIES": SeriesEntry(Sport.MLB, verified=False),
    "KXWORLDSERIES": SeriesEntry(Sport.MLB, verified=False),
    "KXMLBWS": SeriesEntry(Sport.MLB, verified=False),
    # --- NFL ---------------------------------------------------------------
    "KXNFLGAME": SeriesEntry(Sport.NFL, verified=False),
    "KXNFLSPREAD": SeriesEntry(Sport.NFL, verified=False),
    "KXNFLTOTAL": SeriesEntry(Sport.NFL, verified=False),
    "KXSUPERBOWL": SeriesEntry(Sport.NFL, verified=False),
    # --- College football --------------------------------------------------
    "KXNCAAFGAME": SeriesEntry(Sport.CFB, verified=False),
    "KXNCAAFSPREAD": SeriesEntry(Sport.CFB, verified=False),
    "KXNCAAFCHAMP": SeriesEntry(Sport.CFB, verified=False),
    # --- NHL ---------------------------------------------------------------
    # VERIFIED: every one of these was returned by Kalshi's own series listing in NHL-edge-finder's live discovery
    # (run 36567059802, 2026-09-29: 75 NHL series; data/catalog/discovery_summary.json there); KXNHLGAME, KXNHLTOTAL
    # and KXNHLSPREAD were also read back market-by-market as settled 2025-26 history (KXNHLTEAMTOTAL has none yet). Still the weakest evidence level:
    # Kalshi's competition ("Pro Hockey") outranks this table and a disagreement is unresolvable, never an override.
    "KXNHLGAME": SeriesEntry(Sport.NHL, verified=True),
    "KXNHLSPREAD": SeriesEntry(Sport.NHL, verified=True),
    "KXNHLTOTAL": SeriesEntry(Sport.NHL, verified=True),
    "KXNHLTEAMTOTAL": SeriesEntry(Sport.NHL, verified=True),
    "KXNHLOT": SeriesEntry(Sport.NHL, verified=True),
    "KXNHLOVERTIME": SeriesEntry(Sport.NHL, verified=True),
    "KXNHL1P": SeriesEntry(Sport.NHL, verified=True),
    "KXNHL2P": SeriesEntry(Sport.NHL, verified=True),
    "KXNHL3P": SeriesEntry(Sport.NHL, verified=True),
    "KXNHL1PTOTAL": SeriesEntry(Sport.NHL, verified=True),
    "KXNHL2PTOTAL": SeriesEntry(Sport.NHL, verified=True),
    "KXNHL3PTOTAL": SeriesEntry(Sport.NHL, verified=True),
    "KXNHL1PSPREAD": SeriesEntry(Sport.NHL, verified=True),
    "KXNHL2PSPREAD": SeriesEntry(Sport.NHL, verified=True),
    "KXNHL3PSPREAD": SeriesEntry(Sport.NHL, verified=True),
    "KXNHLFIRSTGOAL": SeriesEntry(Sport.NHL, verified=True),
    "KXNHLGOAL": SeriesEntry(Sport.NHL, verified=True),
    "KXNHLPTS": SeriesEntry(Sport.NHL, verified=True),
    "KXNHLAST": SeriesEntry(Sport.NHL, verified=True),
    "KXNHLSAVES": SeriesEntry(Sport.NHL, verified=True),
    # --- Tennis ------------------------------------------------------------
    "KXATPMATCH": SeriesEntry(Sport.TENNIS, verified=False),
    "KXWTAMATCH": SeriesEntry(Sport.TENNIS, verified=False),
    "KXUSOPEN": SeriesEntry(Sport.TENNIS, verified=False),
    "KXWIMBLEDON": SeriesEntry(Sport.TENNIS, verified=False),
}


def lookup_series_ticker(series_ticker: str | None) -> SeriesEntry | None:
    """Return the registry entry for an exact series ticker, if any."""
    if not series_ticker:
        return None
    return SERIES_TICKER_REGISTRY.get(series_ticker.strip().upper())
