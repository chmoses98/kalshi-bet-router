"""Verify candidate series tickers against Kalshi's own series catalogue.

WHY THIS EXISTS
---------------
The classifier's series registry is EXACT MATCH ONLY, and it knew exactly one
MLB series: ``KXMLBGAME``. Measured against the owner's canonical MLB ledger,
that covers 82 of 452 recorded wagers. The other 370 are derivative markets --
first five innings, team totals, strikeouts, run-in-first-inning and so on --
and the registry had never heard of any of them.

That, and not the taxonomy ambiguity gate, is why a live shadow run classified
ZERO episodes as MLB. Proven by classifying the same market twice under the
same ambiguous taxonomy: ``KXMLBGAME`` resolves through the registry,
``KXMLBF5`` is refused, and the only difference between them is the table.

WHY THE FIX IS NOT A WEAKENING
------------------------------
Adding an entry does not lower any evidentiary bar. The registry is an
exact-match table the classifier ALREADY treats as authoritative for
``KXMLBGAME``; an entry can only ever let a ticker match, never make a weaker
kind of evidence count for more. The taxonomy ambiguity gate is untouched, and
a ticker absent from the table still produces UNRESOLVED rather than a guess.

WHY CANDIDATES ARE PROBED RATHER THAN ASSUMED
---------------------------------------------
``KXMLBF5`` is obviously MLB to a human reading it. That is precisely the kind
of reasoning this project does not accept: the registry's docstring is explicit
that entries should be confirmed "from real data", and a prefix that merely
looks right is a naming convention, not evidence.

So each candidate is fetched from ``GET /series/{ticker}`` and corroborated
against the series' OWN category, tags and title. A candidate Kalshi does not
return, or returns without sport-corroborating metadata, is reported and NOT
promoted.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .competitions import normalize
from .sports import Sport

#: Series tickers observed in the owner's canonical MLB ledger that the
#: registry did not know, with the row count that evidences each.
#:
#: This list is the QUESTION, not the answer. Nothing here is added to the
#: registry until Kalshi's own series metadata corroborates it.
MLB_LEDGER_CANDIDATES: dict[str, int] = {
    "KXMLBF5": 172,
    "KXMLBTEAMTOTAL": 94,
    "KXMLBKS": 27,
    "KXMLBTOTAL": 15,
    "KXMLBRFI": 14,
    "KXMLBF5TOTAL": 10,
    "KXMLBOUTS": 8,
    "KXMLBF3": 6,
    "KXMLBF7": 6,
    "KXMLBSPREAD": 2,
    "KXMLBHIT": 2,
    "KXMLBF5SPREAD": 1,
}

#: Tokens in a series' own metadata that corroborate each sport. Deliberately
#: the league name and nothing looser: "baseball" alone would also match a
#: college or international series, which is the mistake the ambiguity gate
#: exists to prevent.
_CORROBORATING_TOKENS: dict[Sport, tuple[str, ...]] = {
    Sport.MLB: ("mlb", "major league baseball"),
    Sport.NFL: ("nfl", "national football league"),
    Sport.CFB: ("ncaaf", "ncaa football", "college football", "cfb"),
    Sport.TENNIS: ("atp", "wta", "tennis"),
}


@dataclass
class SeriesProbeResult:
    """One candidate's outcome. Series tickers are PUBLIC catalogue names."""

    ticker: str
    expected: Sport
    http_status: int | None = None
    exists: bool = False
    corroborated: bool = False
    #: Which metadata field carried the corroborating token, for the record.
    evidence_field: str = ""
    #: Set when the series exists but its metadata names a DIFFERENT sport.
    contradicted: bool = False
    note: str = ""

    @property
    def promotable(self) -> bool:
        return self.exists and self.corroborated and not self.contradicted


@dataclass
class SeriesProbeReport:
    """Counts only, plus the public catalogue names that were confirmed."""

    probed: int = 0
    confirmed: int = 0
    not_found: int = 0
    no_corroborating_metadata: int = 0
    contradicted: int = 0
    lookup_failed: int = 0
    results: list = field(default_factory=list)

    def as_dict(self) -> dict[str, int]:
        return {k: v for k, v in vars(self).items() if isinstance(v, int)}

    def render(self) -> str:
        lines = [
            "series registry candidates (public catalogue names only):",
            f"  probed: {self.probed}",
            f"    CONFIRMED by Kalshi's own metadata: {self.confirmed}",
            f"    series not found: {self.not_found}",
            f"    exists but no corroborating metadata: {self.no_corroborating_metadata}",
            f"    exists and CONTRADICTS the expected sport: {self.contradicted}",
            f"    lookup failed: {self.lookup_failed}",
            "",
        ]
        for r in self.results:
            state = (
                "CONFIRMED" if r.promotable
                else "CONTRADICTED" if r.contradicted
                else "not found" if not r.exists
                else "no corroboration"
            )
            detail = f" via {r.evidence_field}" if r.evidence_field else ""
            note = f"  ({r.note})" if r.note else ""
            lines.append(f"  {r.ticker:<18} {r.expected.value:<7} {state}{detail}{note}")
        return "\n".join(lines)


def _metadata_tokens(series: dict) -> list[tuple[str, str]]:
    """(field name, text) pairs from a series object, flattened."""
    out: list[tuple[str, str]] = []
    for key in ("category", "title", "sub_title", "subtitle"):
        value = series.get(key)
        if isinstance(value, str) and value.strip():
            out.append((f"series.{key}", value))
    for key in ("tags", "categories"):
        value = series.get(key)
        if isinstance(value, (list, tuple)):
            for item in value:
                if isinstance(item, str) and item.strip():
                    out.append((f"series.{key}", item))
    return out


def probe_series(client, candidates: dict[str, Sport]) -> SeriesProbeReport:
    """Ask Kalshi about each candidate. Promotes nothing on its own."""
    from .errors import HttpStatusError, KalshiRouterError

    report = SeriesProbeReport()
    for ticker in sorted(candidates):
        expected = candidates[ticker]
        result = SeriesProbeResult(ticker=ticker, expected=expected)
        report.probed += 1
        try:
            series = client.get_series(ticker)
        except HttpStatusError as exc:
            result.http_status = getattr(exc, "status", None)
            result.note = f"http {result.http_status}"
            report.not_found += 1
            report.results.append(result)
            continue
        except KalshiRouterError as exc:
            result.note = type(exc).__name__
            report.lookup_failed += 1
            report.results.append(result)
            continue

        result.exists = True
        result.http_status = 200

        # Corroboration is checked against EVERY sport, not just the expected
        # one, so a series whose metadata names a different sport is reported
        # as a contradiction rather than merely failing to corroborate.
        matched: dict[Sport, str] = {}
        for field_name, text in _metadata_tokens(series):
            key = normalize(text)
            for sport, tokens in _CORROBORATING_TOKENS.items():
                if any(token in key for token in tokens):
                    matched.setdefault(sport, field_name)

        if expected in matched:
            other = [s for s in matched if s is not expected]
            if other:
                result.contradicted = True
                result.note = "metadata also names " + ",".join(s.value for s in other)
                report.contradicted += 1
            else:
                result.corroborated = True
                result.evidence_field = matched[expected]
                report.confirmed += 1
        elif matched:
            result.contradicted = True
            result.note = "metadata names " + ",".join(s.value for s in matched)
            report.contradicted += 1
        else:
            report.no_corroborating_metadata += 1
        report.results.append(result)
    return report
