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
#: NFL is deliberately absent. Its importer needs a real NFL week resolved from
#: a schedule capture on a third branch, and nobody has verified that path for
#: the scheduled job. A sport with no profile is REFUSED, not defaulted.
PROFILES: dict[Sport, DestinationProfile] = {
    Sport.MLB: DestinationProfile(
        sport=Sport.MLB,
        repo="chmoses98/edge-finder-api",
        ledger_branch="main",
        code_branch=None,
        wager_importer=(
            "python",
            "scripts/edgelab/import_bet_batch.py",
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
        # HELD FOR OBSERVATION. CFB has never completed a real delivery: the
        # end-to-end path is proven by a dry run and by tests driving the
        # committed bash, which is not the same as having watched it land.
        # The first few real batches should be read by a person before the
        # loop closes. Set to True once they have been.
        auto_merge=False,
        ledger_validator=(
            "python",
            "{code}/scripts/validate_accounting_ledger.py",
            "--base-dir",
            "{work}",
            "--result-out",
            "{receipts}",
        ),
        committable_prefixes=("wagers/", "settlements/"),
        mergeable_paths=frozenset(
            {f"wagers/{year}.jsonl" for year in range(2024, 2036)}
            | {f"settlements/{year}.jsonl" for year in range(2024, 2036)}
        ),
        row_identity_field="source_bet_key",
        requires_season=True,
        notes=(
            "ledger on accounting-data, importer on main: two checkouts",
            "one file per season, and the season is named rather than inferred",
            "accounting-data is an orphan branch with no .github/, so a pull request into "
            "it gets no check runs; the destination's own validator supplies the verdict",
        ),
    ),
}


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
        "notes": list(profile.notes),
    }
