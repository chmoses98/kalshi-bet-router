"""Everything the delivery path needs to know about one destination, in one place.

*** WHY THIS EXISTS ***
Production routing used to know a destination in five places. The repository
name lived in ``destination.DESTINATION_REPOS``; the importer command and the
ledger branch lived inline in ``deliver-wagers.yml`` as MLB literals; the
committable path prefix lived in ``delivery_branch.COMMITTABLE_PREFIX``; the
mergeable paths lived in ``automerge.MERGEABLE_PATHS``. Every one of those was
correct for MLB and silently wrong for anything else, and adding a second sport
meant remembering all five.

It also meant the production workflow could not route a sport whose ledger is
on a DATA BRANCH rather than on ``main``. CFB's is: ``accounting-data``, with
the importer on ``main``. The one-time backfill workflow already knew that --
it carried a ``case`` naming the repository, the branch, the importer script
and the allowed path prefix for all three destinations -- and production had no
way to learn it.

So a destination is now ONE OBJECT, and every consumer reads it.

*** WHAT ACTIVATING A SPORT ACTUALLY REQUIRES ***
Adding an entry to :data:`PROFILES` turns production routing on for that sport.
It is deliberately not a one-line change: the profile cannot be written without
naming the repository, the branch its ledger lives on, the branch its IMPORTER
lives on, the exact command that runs it, what that command may touch, and what
may be merged without a human. A sport whose answers nobody knows cannot be
added by accident.

*** WHAT IT DOES NOT DO ***
It holds no credential, runs no command and makes no network call. It is a
table, and `tests/test_destination_profiles.py` reads it the same way the
workflow does.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .sports import Sport


@dataclass(frozen=True)
class DestinationProfile:
    """One downstream ledger, and the whole contract for writing to it."""

    sport: Sport
    repo: str
    ledger_branch: str
    """The branch the canonical ledger lives on.

    `main` for MLB, `accounting-data` for CFB. This is the branch the delivery
    clone checks out and the branch a pull request targets -- NOT necessarily
    the branch the importer's source is on."""

    code_branch: str | None
    """The branch the importer's SOURCE lives on, when it differs.

    None means "the same checkout as the ledger". CFB keeps its importer on
    `main` while its ledger is on `accounting-data`, so the delivery needs two
    checkouts; conflating them is how a delivery runs an importer that does not
    exist on the branch it is writing."""

    wager_importer: tuple[str, ...]
    """The importer command, as argv with placeholders.

    Placeholders, substituted by the caller:
      {payload}   the payload file
      {work}      the ledger checkout
      {code}      the code checkout (equals {work} when code_branch is None)
      {receipts}  where to write the run's receipts
      {season}    the CFB season, for a ledger that is one file per season
    """

    settlement_importer: tuple[str, ...] | None
    """The same, for settlements. None means this destination settles its own.

    MLB is None on purpose: `edge-finder-api` re-derives every outcome from the
    MLB Stats API in its own postgame job, and a settlement pushed there by
    this router would be a SECOND authority on one fact. The two would disagree
    the first time either was wrong."""

    committable_prefixes: tuple[str, ...]
    """Path prefixes a router-authored COMMIT may touch. A containment rule."""

    mergeable_paths: frozenset[str]
    """Exact paths that may be MERGED to the ledger branch without a human.

    Deliberately narrower than `committable_prefixes` and deliberately exact:
    the importer is allowed to write anywhere under its prefix, and landing
    that unread is a separate, smaller permission. A file the importer starts
    writing later shows up as a refusal and somebody looks once, rather than
    being waved through forever by a prefix match."""

    ledger_branch_runs_ci: bool

    """Whether a pull request into the LEDGER BRANCH gets a check run at all.

    *** THE FACT THAT WOULD OTHERWISE STALL EVERY DELIVERY ***
    GitHub runs a `pull_request` workflow only if that workflow file exists on
    the pull request's BASE branch. MLB's base is `main`, which carries the
    repository's CI, so a delivery there is checked. CFB's base is
    `accounting-data`, an ORPHAN branch holding `wagers/`, `settlements/` and a
    README -- no `.github/` at all. Measured rather than assumed: the two CFB
    backfill pull requests of 2026-09-15 have ZERO workflow runs between them.

    So no check ever reports there, the auto-merge gate's green-CI condition
    waits for a signal that cannot arrive, and every automatic CFB delivery
    would sit open forever. `ledger_validator` is the answer."""

    ledger_validator: tuple[str, ...] | None
    """The destination's OWN validator, run by the delivery after the import.

    Not a substitute for the destination's judgement -- it IS the
    destination's judgement, the same way the importer is: its script, its
    schema, its rules, run against the tree its importer just produced. The
    router only reports the verdict.

    None means the ledger branch's own CI covers it."""

    row_identity_field: str
    """The key each destination's receipt uses for the router's own source key.

    MLB says `sourceBetKey`; the snake_case ledgers say `source_bet_key`. The
    gate normalises through `receipts.normalise`, and this is the field it
    reads."""

    requires_season: bool = False
    #: Whether a batch that PASSES every gate condition may merge itself.
    #:
    #: False is an OBSERVATION PERIOD, not a defect and not a weaker gate: the
    #: gate still runs and still prints its verdict, the rows are still
    #: delivered to the ledger branch and still open a pull request, and a
    #: REFUSAL is still red. The only thing withheld is the final merge, so a
    #: person can read the first few real deliveries before the path closes
    #: over itself. Flip it to True once those look right.
    auto_merge: bool = True
    """Whether the importer needs `--season`. True for CFB, whose ledger is one
    file per season and which refuses to infer one from a game date -- a
    college season spans two calendar years."""

    notes: tuple[str, ...] = field(default_factory=tuple)

    record_layout: str = "jsonl"
    """How the canonical ledger stores rows.

    "jsonl": one file holds many rows, one per line (MLB, CFB). The gate reads
    added and removed LINES of the exact `mergeable_paths`.

    "json_per_file": every record is its own JSON file (NFL's handicap-data,
    `data/imported_wagers/<season>/week_<NN>/<id>.json`). A line diff of a
    pretty-printed JSON file is not a list of rows, so the gate reads ADDED
    FILES as rows, and any modified, deleted or renamed ledger file as a
    rewrite -- the same append-only property, in that ledger's own shape."""

    settlement_economics: str = "router-settlement-economics.v1"
    """Which settlement-economics contract this destination receives (`kalshi_router.settlement`).

    v1 subtracts the position's trading fee a second time (see `settlement.ECONOMICS_V1`); it stays only where
    the destination cannot yet accept the v2 row or reconcile its already-filed v1 settlements. Moving a
    destination to v2 requires it to accept `economics_version` and to answer a v1-vs-v2 difference with an
    append-only amendment rather than a conflict or a rewrite."""

    mergeable_patterns: tuple[str, ...] = ()
    """Full-match regular expressions for ledgers whose paths are not a fixed set.

    A per-record ledger cannot list its paths in advance, so it names their
    SHAPE instead -- as narrowly as the importer's own id minting allows (a
    fixed prefix, a fixed-length hex digest, one directory per season and
    week). This is the same permission `mergeable_paths` grants, stated as a
    pattern: a file of any other shape still refuses."""

    records_combo_wagers: bool = False
    """Whether this ledger can RECORD a wager on a multivariate COMBO (parlay) market, end to end.

    A combo's sport is proven by its legs (`classify.classify_with_legs`), but proving the sport does not make
    a ledger able to hold the wager. True only where the whole lifecycle was read and holds for a ticker
    that names no single game: the importer and the destination's validator treat `market_ticker` as an
    opaque string, and the SETTLEMENT is the exchange's own settlement of that combo market, delivered by
    this router (a `settlement_importer` exists). False by default, and False for MLB, which settles its own
    wagers from the contract it parses out of the ticker and explicitly defers multi-market combos
    (`edge-finder-api lib/wager_settlement_semantics.py: SIDE_DEFERRED_MULTI_MARKET_COMBO`) -- a combo filed
    there would never settle. A combo for such a destination is refused COMBO_NOT_RECORDABLE (BLOCKED)."""


#: Every destination production routing is willing to push to.
#:
#: *** MLB ***
#: The original. Ledger and importer both on `main`.
#:
#: *** CFB ***
#: Activated after end-to-end verification against the destination's own
#: contract: `Sport.CFB` exists in the enum, `production.to_cfb_import_row`
#: already emits `cfb_accounted_wager.v1`, `scripts/import_routed_wagers.py`
#: validates and deduplicates on `source_bet_key`, and
#: `accounting/store.append_wagers` is append-only and keyed on that same
#: field. Eighteen CFB wagers were already delivered through exactly this path
#: by the one-time gap backfill in September 2026; what was missing was
#: PRODUCTION activation, which is this entry plus the branch and importer
#: facts the scheduled workflow could not previously express.
#:
#: A sport with no profile is REFUSED, not defaulted.
PROFILES: dict[Sport, DestinationProfile] = {
    Sport.MLB: DestinationProfile(
        sport=Sport.MLB,
        repo="chmoses98/edge-finder-api",
        ledger_branch="main",
        # THE IMPORTER IS CODE, AND CODE COMES FROM THE CODE BRANCH'S HEAD.
        #
        # This was None ("the importer lives beside the ledger, run it from
        # the work tree"). But the work tree is not always main: when the
        # router's proposal branch already sits on the current ledger, the
        # importer runs ON TOP OF THAT BRANCH (delivery_branch.seed), whose
        # tree is the ledger as of the proposal PLUS the importer as of the
        # proposal. So a fix merged to the destination's main was invisible
        # until main's LEDGER moved: edge-finder-api #250 (merged 15:05Z on
        # 2026-09-28) ended the marketObservationLinkage CONFLICT, and
        # deliver run 36440425803 at 15:17Z still refused the same row,
        # because it executed kalshi-router/MLB's copy of import_bet_batch.py
        # from 2026-09-24. A separate checkout of main is what CFB and NFL
        # already do; the ledger stays in {work} (the importer's data paths
        # are relative to its working directory) and only the code moves.
        code_branch="main",
        wager_importer=(
            "python",
            "{code}/scripts/edgelab/import_bet_batch.py",
            "--file",
            "{payload}",
            "--receipts-out",
            "{receipts}",
        ),
        settlement_importer=None,
        committable_prefixes=("data/",),
        mergeable_paths=frozenset({"data/edgelab/bets/bets.jsonl"}),
        ledger_branch_runs_ci=True,
        # Proven in production over many deliveries.
        auto_merge=True,
        ledger_validator=None,
        row_identity_field="sourceBetKey",
        notes=(
            "settles its own bets from the MLB Stats API; the router must not push "
            "settlements here",
        ),
    ),
    Sport.CFB: DestinationProfile(
        sport=Sport.CFB,
        repo="chmoses98/cfb-edge-finder",
        ledger_branch="accounting-data",
        code_branch="main",
        wager_importer=(
            "python",
            "{code}/scripts/import_routed_wagers.py",
            "--payload",
            "{payload}",
            "--base-dir",
            "{work}",
            "--season",
            "{season}",
            "--receipts-out",
            "{receipts}",
        ),
        settlement_importer=(
            "python",
            "{code}/scripts/import_routed_settlements.py",
            "--payload",
            "{payload}",
            "--base-dir",
            "{work}",
            "--season",
            "{season}",
            "--receipts-out",
            "{receipts}",
        ),
        ledger_branch_runs_ci=False,
        # Observation period closed 2026-09-28. Real CFB batches were read by
        # a person before this loop closed: cfb-edge-finder #57 (34
        # settlements), #58 (27 wagers), and the 2026-09-26 full postmortem
        # (32 wagers, 0 unaccounted). The gate itself is unchanged; the
        # amendment path it now also carries is driven to MERGE and REFUSE in
        # tests/test_cfb_amendment_gate.py.
        auto_merge=True,
        ledger_validator=(
            "python",
            "{code}/scripts/validate_accounting_ledger.py",
            "--base-dir",
            "{work}",
            "--result-out",
            "{receipts}",
        ),
        # v2 from 2026-09-28: net = gross - stake once the exchange's fee_cost proves no further fee.
        # cfb-edge-finder answers a v1 settlement already on file with an append-only AMENDMENT row in
        # `settlement_amendments/<season>.jsonl` (never a rewrite of the v1 row), identified
        # deterministically from the wager's key and the contract, and receipts it CORRECTED; a repeat
        # is DUPLICATE_NOOP and a disagreeing correction is REFUSED. The 89 reconciled historical rows
        # were amended by the destination's own backfill before this switched; the router's first v2
        # run lands on the same amendment ids as no-ops and establishes the four shared-position rows
        # v1 had refused.
        settlement_economics="router-settlement-economics.v2",
        committable_prefixes=("wagers/", "settlements/", "settlement_amendments/"),
        mergeable_paths=frozenset(
            {f"wagers/{year}.jsonl" for year in range(2024, 2036)}
            | {f"settlements/{year}.jsonl" for year in range(2024, 2036)}
            | {f"settlement_amendments/{year}.jsonl" for year in range(2024, 2036)}
        ),
        row_identity_field="source_bet_key",
        requires_season=True,
        # Read 2026-10-08: import_routed_wagers / validate_accounting_ledger treat market_ticker as an opaque
        # string; decision attribution matches it EXACTLY (a combo matches no recommendation and stays
        # unattributed, which is true); settlement comes from this router.
        records_combo_wagers=True,
        notes=(
            "ledger on accounting-data, importer on main: two checkouts",
            "one file per season, and the season is named rather than inferred",
            "accounting-data is an orphan branch with no .github/, so a pull request into "
            "it gets no check runs; the destination's own validator supplies the verdict",
        ),
    ),
    #: *** NFL ***
    #: Activated 2026-09-24, after the week-2 wagers were found never to have
    #: been delivered: with no profile here, every post-cutover NFL order was
    #: refused as NO_DESTINATION_IMPORTER, which health counted as BY DESIGN,
    #: so nothing ever turned red. Week 1 had reached the ledger only through
    #: the one-time gap backfill, whose window ends at the cutover.
    #:
    #: What made the scheduled path verifiable (the reason this entry was
    #: withheld): the importer resolves the week from the REAL nflverse
    #: schedule (`--allow-schedule-download`, the same schedule the backfill
    #: proved on 24 wagers) and refuses a date matching no single week; it
    #: returns one receipt per row in the shared vocabulary; and the
    #: destination ships its own whole-ledger validator, because handicap-data
    #: -- like CFB's accounting-data -- carries no `.github/` and gets no
    #: check runs.
    Sport.NFL: DestinationProfile(
        sport=Sport.NFL,
        repo="chmoses98/nfl-edge-finder",
        ledger_branch="handicap-data",
        code_branch="main",
        wager_importer=(
            "python",
            "{code}/scripts/handicap/import_routed_wagers.py",
            "--payload",
            "{payload}",
            "--handicap-root",
            "{work}",
            "--allow-schedule-download",
            "--receipts-out",
            "{receipts}",
        ),
        settlement_importer=(
            "python",
            "{code}/scripts/handicap/import_routed_settlements.py",
            "--payload",
            "{payload}",
            "--handicap-root",
            "{work}",
            "--receipts-out",
            "{receipts}",
        ),
        committable_prefixes=("data/imported_wagers/", "data/wager_settlements/",
                              "data/wager_settlement_amendments/"),
        mergeable_paths=frozenset(),
        record_layout="json_per_file",
        mergeable_patterns=(
            r"data/imported_wagers/20\d{2}/week_\d{2}/routed-[0-9a-f]{24}\.json",
            r"data/wager_settlements/20\d{2}/week_\d{2}/stl-[0-9a-f]{24}\.json",
            r"data/wager_settlement_amendments/20\d{2}/week_\d{2}/amd-[0-9a-f]{24}\.json",
        ),
        # v2: net = gross - stake once the exchange's fee_cost proves no further fee. nfl-edge-finder answers a
        # v1 settlement already on file with an append-only AMENDMENT record (never a rewrite), which it
        # re-derives itself before accepting.
        settlement_economics="router-settlement-economics.v2",
        ledger_branch_runs_ci=False,
        ledger_validator=(
            "python",
            "{code}/scripts/handicap/validate_routed_ledger.py",
            "--handicap-root",
            "{work}",
            "--result-out",
            "{receipts}",
        ),
        row_identity_field="source_bet_key",
        # The gate still decides. The importer path it runs is the one that
        # landed 24 week-1 wagers and 24 settlements in production (PRs #24,
        # #26), and the owner asked for delivery to be hands-off; the first
        # scheduled batch was watched as it landed.
        auto_merge=True,
        # Read 2026-10-08: imported_wager.v1 requires market_ticker as a non-empty string and nothing more; the
        # week comes from game_date (one date for a combo, by construction); settlement comes from this router.
        records_combo_wagers=True,
        notes=(
            "ledger on handicap-data, importer on main: two checkouts",
            "one JSON file per record; the week is resolved by the destination from the real schedule",
            "handicap-data has no .github/, so the destination's own validator supplies the verdict",
        ),
    ),
    #: *** NHL *** (2026-09-29)
    #: ACCOUNTING ONLY: wagers the owner places by hand on Kalshi, recorded in NHL-edge-finder's routed-wager
    #: ledger. The NHL research models (DATA_ONLY_V1/V2) have no part in it and remain RESEARCH_ONLY.
    #:
    #: Same shape as CFB -- a JSONL ledger on an ORPHAN `accounting-data` branch (README + the two ledger files,
    #: deliberately none of the repository's model/research data), importer and validator on `main`, two
    #: checkouts -- with two differences: the ledger is NOT season-partitioned (an NHL season spans two calendar
    #: years and nothing should have to guess one), and it speaks router-settlement-economics.v2 from its first
    #: row (no v1 history exists, so no amendment path is needed).
    Sport.NHL: DestinationProfile(
        sport=Sport.NHL,
        repo="chmoses98/NHL-edge-finder",
        ledger_branch="accounting-data",
        code_branch="main",
        wager_importer=(
            "python",
            "{code}/scripts/accounting/import_routed_wagers.py",
            "--payload",
            "{payload}",
            "--base-dir",
            "{work}",
            "--receipts-out",
            "{receipts}",
        ),
        settlement_importer=(
            "python",
            "{code}/scripts/accounting/import_routed_settlements.py",
            "--payload",
            "{payload}",
            "--base-dir",
            "{work}",
            "--receipts-out",
            "{receipts}",
        ),
        committable_prefixes=("data/accounting/",),
        mergeable_paths=frozenset({
            "data/accounting/wagers.jsonl",
            "data/accounting/settlements.jsonl",
        }),
        # accounting-data carries no .github/, so no check run ever reports on a pull request into it; the
        # destination's own validator supplies the verdict, exactly as for CFB and NFL.
        ledger_branch_runs_ci=False,
        ledger_validator=(
            "python",
            "{code}/scripts/accounting/validate_routed_ledger.py",
            "--base-dir",
            "{work}",
            "--result-out",
            "{receipts}",
        ),
        row_identity_field="source_bet_key",
        requires_season=False,
        settlement_economics="router-settlement-economics.v2",
        # OBSERVATION PERIOD until the full path is proven end to end against the live destination (classifier on
        # real NHL markets, import, identical re-import DUPLICATE_NOOP, settlement, orphan refusal, validator,
        # containment, reconciliation, a production dry run). The gate still runs and still prints its verdict.
        auto_merge=False,
        # The NHL routed ledger reads market_ticker only to pair a settlement with its wager (same ticker and
        # side); settlement comes from this router.
        records_combo_wagers=True,
        notes=(
            "ACCOUNTING ONLY: manually placed Kalshi NHL wagers; no NHL model is involved and none has authority",
            "ledger on accounting-data (orphan: README + data/accounting/ only), importer and validator on main",
            "not season-partitioned: data/accounting/wagers.jsonl and data/accounting/settlements.jsonl",
            "accounting-data has no .github/, so the destination's own validator supplies the verdict",
        ),
    ),
}


def _shared_ledger_profile(sport: Sport, repo: str, notes: tuple[str, ...]) -> DestinationProfile:
    """A destination that imports through the contract's shared routed ledger.

    NBA, SOCCER and TENNIS (2026-10-02, app-readiness pass) are ACCOUNTING ONLY destinations with the NHL shape:
    an ORPHAN `accounting-data` branch holding `data/accounting/wagers.jsonl` and `settlements.jsonl` and nothing
    else; importer and validator on `main` under `scripts/accounting/`, thin wrappers over the vendored
    `contract/edge_finder_contract/routed_ledger.py` (zero dependencies, so the router's runner needs nothing
    installed); not season-partitioned; settlement economics v2 from the first row. Identity is minted by the
    destination: `<prefix>w-` / `<prefix>s-` + sha256(source_bet_key)[:24].

    auto_merge is False: an OBSERVATION PERIOD, as for NHL, until a real delivery has been watched through the
    gate on each destination. The branch is still pushed and the pull request still opened; only the final merge
    waits for a person.
    """
    return DestinationProfile(
        sport=sport,
        repo=repo,
        ledger_branch="accounting-data",
        code_branch="main",
        wager_importer=(
            "python", "{code}/scripts/accounting/import_routed_wagers.py",
            "--payload", "{payload}", "--base-dir", "{work}", "--receipts-out", "{receipts}",
        ),
        settlement_importer=(
            "python", "{code}/scripts/accounting/import_routed_settlements.py",
            "--payload", "{payload}", "--base-dir", "{work}", "--receipts-out", "{receipts}",
        ),
        committable_prefixes=("data/accounting/",),
        mergeable_paths=frozenset({"data/accounting/wagers.jsonl", "data/accounting/settlements.jsonl"}),
        ledger_branch_runs_ci=False,
        ledger_validator=(
            "python", "{code}/scripts/accounting/validate_routed_ledger.py",
            "--base-dir", "{work}", "--result-out", "{receipts}",
        ),
        row_identity_field="source_bet_key",
        requires_season=False,
        settlement_economics="router-settlement-economics.v2",
        auto_merge=False,
        # contract/edge_finder_contract/routed_ledger.py: market_ticker is an opaque required string, compared
        # only to pair a settlement with its wager; settlement comes from this router.
        records_combo_wagers=True,
        notes=(
            "ACCOUNTING ONLY: manually placed Kalshi wagers; no model is involved and none has authority",
            "ledger on accounting-data (orphan: README + data/accounting/ only), importer and validator on main",
            "imports through the shared edge_finder_contract.routed_ledger (vendored from kalshi-bet-router/contract)",
            "accounting-data has no .github/, so the destination's own validator supplies the verdict",
        ) + notes,
    )


PROFILES[Sport.NBA] = _shared_ledger_profile(Sport.NBA, "chmoses98/nba-edge-finder", (
    "identity minted by the destination: nbaw-/nbas- + sha256(source_bet_key)[:24]",
))
PROFILES[Sport.SOCCER] = _shared_ledger_profile(Sport.SOCCER, "chmoses98/soccer-edge-finder", (
    "identity minted by the destination: socw-/socs- + sha256(source_bet_key)[:24]",
    "soccer_edge.router (PositionV1) remains the repository's own translation layer; delivery uses the shared ledger",
))
PROFILES[Sport.TENNIS] = _shared_ledger_profile(Sport.TENNIS, "chmoses98/Tennis-Edge-Finder", (
    "identity minted by the destination: tenw-/tens- + sha256(source_bet_key)[:24]",
    "tennis settlements can be SCALAR on the exchange (walkovers); a scalar result arrives with result absent and "
    "the money fields as the exchange states them",
))


class UnknownDestinationError(KeyError):
    """No profile for this sport. A refusal, never a default."""


def profile_for(sport_name: str) -> DestinationProfile:
    for sport, profile in PROFILES.items():
        if sport.value == sport_name:
            return profile
    raise UnknownDestinationError(
        f"{sport_name} has no destination profile; production routing refuses rather "
        f"than defaulting somewhere plausible. Known: {sorted(s.value for s in PROFILES)}"
    )


def has_profile(sport_name: str) -> bool:
    return any(sport.value == sport_name for sport in PROFILES)


def routable_sport_names() -> frozenset[str]:
    return frozenset(sport.value for sport in PROFILES)


def render_command(
    template: tuple[str, ...],
    *,
    payload: str,
    work: str,
    code: str,
    receipts: str,
    season: str | int | None = None,
) -> list[str]:
    """Fill a command template. Refuses an unfilled placeholder.

    A `{season}` that reached a real command line as the literal string
    "{season}" would be handed to the CFB importer's `type=int`, which would
    reject it -- eventually, after a clone and an import. Refusing here, before
    anything is fetched, names the missing value instead.
    """
    values = {
        "payload": payload,
        "work": work,
        "code": code,
        "receipts": receipts,
        "season": "" if season is None else str(season),
    }
    out: list[str] = []
    for part in template:
        try:
            rendered = part.format(**values)
        except KeyError as exc:  # pragma: no cover - a template typo
            raise UnknownDestinationError(
                f"the importer template names an unknown placeholder {exc}"
            ) from exc
        if not rendered and part:
            raise UnknownDestinationError(
                f"the importer template needs a value for {part!r} and none was supplied"
            )
        out.append(rendered)
    return out


def combo_destination_names() -> frozenset[str]:
    """Sports whose destination can record a combo wager. A destination that settles its own wagers is never
    one, whatever its flag says: its settlement would have to grade a ticker that names no single game."""
    return frozenset(
        sport.value for sport, profile in PROFILES.items()
        if profile.records_combo_wagers and profile.settlement_importer is not None
    )


def describe(sport_name: str) -> dict[str, Any]:
    """The profile as plain JSON, for the delivery workflow's shell to read.

    Safe to print: a repository name, a branch name and a command template are
    already public facts about this system. Nothing here is a wager.
    """
    profile = profile_for(sport_name)
    return {
        "sport": profile.sport.value,
        "repo": profile.repo,
        "ledger_branch": profile.ledger_branch,
        "code_branch": profile.code_branch or profile.ledger_branch,
        "needs_separate_code_checkout": profile.code_branch is not None,
        "wager_importer": list(profile.wager_importer),
        "settlement_importer": (
            list(profile.settlement_importer) if profile.settlement_importer else None
        ),
        "settles_its_own": profile.settlement_importer is None,
        "ledger_branch_runs_ci": profile.ledger_branch_runs_ci,
        "ledger_validator": (
            list(profile.ledger_validator) if profile.ledger_validator else None
        ),
        "has_ledger_validator": profile.ledger_validator is not None,
        "committable_prefixes": list(profile.committable_prefixes),
        "mergeable_paths": sorted(profile.mergeable_paths),
        "row_identity_field": profile.row_identity_field,
        "requires_season": profile.requires_season,
        "record_layout": profile.record_layout,
        "settlement_economics": profile.settlement_economics,
        "mergeable_patterns": list(profile.mergeable_patterns),
        "records_combo_wagers": profile.records_combo_wagers,
        "notes": list(profile.notes),
    }
