"""Command line entry point.

Default behaviour is aggregate-only and safe for a public log.  Per-fill detail
requires ``--show-sensitive-details`` *and* a non-CI environment; see
:mod:`kalshi_router.safety`.

Exit codes
----------
0   success, including a successful audit of an account with no fills
2   configuration or credential problem (nothing was sent to Kalshi)
3   Kalshi API or schema failure
4   sensitive output was requested somewhere it is not allowed
"""

from __future__ import annotations

import argparse
import json
import time
from decimal import Decimal
import sys

from .audit import AuditResult, run_audit
from .auth import KalshiSigner
from .client import KalshiReadOnlyClient
from .config import (
    DEFAULT_MAX_FILLS,
    DEFAULT_PAGE_LIMIT,
    AuditConfig,
    base_url_from_env,
    read_credentials,
)
from .errors import ConfigurationError, KalshiRouterError, SensitiveOutputRefused
from .safety import assert_sensitive_output_allowed

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_API = 3
EXIT_SENSITIVE_REFUSED = 4

#: A recovery run that imports PRE-CUTOVER history must say so in words.
#:
#: A boolean flag is too easy to set by accident -- in a workflow input, in a
#: copied command line, in a retry. The destination ledgers already hold
#: manually entered wagers for that period, so admitting history is a decision
#: about someone's financial record, and this makes stating it deliberate.
PRE_CUTOVER_ACKNOWLEDGEMENT = "I have decided to import pre-cutover history"

SENSITIVE_BANNER = """
!!  SENSITIVE LOCAL DIAGNOSTICS  !!
The block below identifies individual markets you traded. It is private betting
activity. Do not paste it into an issue, a pull request, a chat, or any log.
""".strip()


def _add_deliver_parser(sub) -> None:
    deliver = sub.add_parser(
        "deliver",
        help="write the importer payload for every eligible wager (counts to stdout, rows to files)",
    )
    deliver.add_argument(
        "--out-dir",
        required=True,
        help=(
            "Directory to write one payload per destination sport. The payload "
            "carries market, side, stake, contracts, price and fees, so it goes "
            "to a file and never to stdout."
        ),
    )
    deliver.add_argument(
        "--page-limit",
        type=int,
        default=DEFAULT_PAGE_LIMIT,
        help=f"rows per API page (default {DEFAULT_PAGE_LIMIT})",
    )
    # The deliver path never renders a wager, so the sensitive-details mode is
    # not merely unset here -- it does not exist. Defaulted so the shared
    # credential setup in main() can read it uniformly.
    deliver.set_defaults(show_sensitive_details=False, max_fills=None)
    deliver.add_argument(
        "--include-pre-cutover",
        default=None,
        metavar="ACKNOWLEDGEMENT",
        help=(
            "RECOVERY ONLY. Admit orders submitted at or before the production "
            "cutover. Requires the exact phrase "
            f"{PRE_CUTOVER_ACKNOWLEDGEMENT!r}. Off unless stated: the "
            "destination ledgers already hold manually entered wagers for that "
            "period."
        ),
    )
    deliver.add_argument(
        "--allow-stabilization",
        action="store_true",
        help=(
            "Treat an order with no new fill for the stabilization window as "
            "final, even while its market is open. Supported by measurement: "
            "every order in this account's history filled within one second."
        ),
    )


def config_from_args(args) -> AuditConfig:
    """Build the client's request shape from any subcommand's parsed arguments.

    This is a named function rather than three lines inside ``main()`` because
    it is the one piece of setup EVERY subcommand goes through, and a
    subcommand that omits an option it never uses must not be able to break it.
    The first release of ``deliver`` did exactly that -- it declared no
    ``max_fills``, and ``AuditConfig`` compared ``1 <= None`` and crashed before
    a single request was made. The test suite now parses every subparser and
    passes it through here.

    A subcommand that does not offer a sampling budget leaves these unset.
    ``AuditConfig`` is the CLIENT's request shape: it still needs a page size
    and a default ceiling even when the subcommand will override both.
    ``deliver`` walks to exhaustion, which sets the budget to infinity inside
    the client, so the ceiling is genuinely inert for it -- but ``None`` was
    not inert, it was a crash.
    """
    return AuditConfig(
        base_url=base_url_from_env(),
        max_fills=(
            DEFAULT_MAX_FILLS
            if getattr(args, "max_fills", None) is None
            else args.max_fills
        ),
        page_limit=(
            DEFAULT_PAGE_LIMIT
            if getattr(args, "page_limit", None) is None
            else args.page_limit
        ),
    )


def _add_series_probe_parser(sub) -> None:
    probe = sub.add_parser(
        "series-probe",
        help="ask Kalshi to corroborate candidate series tickers (adds nothing)",
    )
    probe.set_defaults(show_sensitive_details=False, max_fills=None, page_limit=None)


def _add_backfill_parser(sub) -> None:
    backfill = sub.add_parser(
        "backfill",
        help="inspect a bounded PRE-CUTOVER window and reconcile it (writes nothing)",
    )
    backfill.add_argument(
        "--since",
        required=True,
        metavar="RFC3339",
        help=(
            "Start of the window. The END is always the production cutover and "
            "is not settable, so this cannot be pointed at production's range."
        ),
    )
    backfill.add_argument(
        "--ledger",
        action="append",
        default=[],
        metavar="SPORT=PATH",
        help=(
            "A destination ledger to reconcile against, e.g. MLB=/path/bets.jsonl. "
            "Repeatable. A sport with no ledger given is reported as "
            "UNSUPPORTED_DESTINATION rather than assumed empty -- 'nothing to "
            "compare against' and 'nothing there' are different answers."
        ),
    )
    backfill.add_argument(
        "--out-dir",
        default=None,
        metavar="DIR",
        help=(
            "Write one importer payload per destination for the MISSING_IMPORTABLE "
            "wagers only. Omitted, this command still writes nothing at all -- "
            "inspection stays the default, and producing a payload stays an "
            "explicit request. The payload is written to FILES and never printed."
        ),
    )
    backfill.set_defaults(show_sensitive_details=False, max_fills=None, page_limit=None)


def _add_settle_parser(sub) -> None:
    settle = sub.add_parser(
        "settle",
        help="attribute exchange settlements to the gap window's wagers (writes payloads only)",
    )
    settle.add_argument(
        "--since",
        required=True,
        metavar="RFC3339",
        help=(
            "Start of the window. The END is always the production cutover and "
            "is not settable, exactly as it is for `backfill`."
        ),
    )
    settle.add_argument(
        "--destination",
        action="append",
        default=[],
        metavar="SPORT",
        help=(
            "A destination to emit settlements for. REPEATABLE and REQUIRED: "
            "nothing is implied. MLB is deliberately not a default -- that "
            "repository has its own canonical settlement driver, and a second "
            "one would be a second authority on the same fact."
        ),
    )
    settle.add_argument(
        "--out-dir",
        default=None,
        metavar="DIR",
        help=(
            "Write one settlement payload per destination. Omitted, this "
            "command reports counts and writes nothing."
        ),
    )
    settle.set_defaults(show_sensitive_details=False, max_fills=None, page_limit=None)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kalshi-router",
        description=(
            "Phase 0 read-only Kalshi fill audit. Prints aggregate counts only "
            "unless sensitive local diagnostics are explicitly requested."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    audit = sub.add_parser("audit", help="fetch recent fills and report aggregate counts")
    audit.add_argument(
        "--max-fills",
        type=int,
        default=DEFAULT_MAX_FILLS,
        help=f"bounded recent sample size (default {DEFAULT_MAX_FILLS})",
    )
    audit.add_argument(
        "--page-limit",
        type=int,
        default=DEFAULT_PAGE_LIMIT,
        help=f"fills requested per page (default {DEFAULT_PAGE_LIMIT})",
    )
    audit.add_argument(
        "--json",
        action="store_true",
        help="emit the aggregate report as JSON (still counts only)",
    )
    audit.add_argument(
        "--max-classify-markets",
        type=int,
        default=None,
        help=(
            "classify at most this many markets. Accounting still replays every "
            "fill; only the metadata sweep is bounded, because it costs several "
            "requests per market. Unclassified markets are reported separately "
            "and are never counted as unresolved."
        ),
    )
    audit.add_argument(
        "--production",
        action="store_true",
        help=(
            "Apply the production filter: which post-cutover orders would be "
            "delivered, and why the rest would not. Reports counts; delivers "
            "nothing."
        ),
    )
    audit.add_argument(
        "--shadow-wagers",
        action="store_true",
        help=(
            "Build the canonical wager rows a router WOULD send and report the "
            "counts. Nothing is sent, written or persisted; the refusals are "
            "the point."
        ),
    )
    audit.add_argument(
        "--compare-ledger",
        default=None,
        metavar="PATH",
        help=(
            "PHASE 6 VALIDATION, NOT BACKFILL. Compare the shadow wagers "
            "against an existing destination ledger (JSONL) and report how "
            "they agree. Requires --shadow-wagers. Nothing is written, sent or "
            "proposed; the comparison happens in memory and only counts are "
            "printed."
        ),
    )
    audit.add_argument(
        "--compare-ledger-sport",
        default="MLB",
        help="which sport's rows to read from the compared ledger (default MLB)",
    )
    audit.add_argument(
        "--full-history",
        action="store_true",
        help=(
            "walk both fill routes to EXHAUSTION so the replay can claim a "
            "COMPLETE history. Ignores --max-fills, because a budget and a "
            "completeness claim are mutually exclusive. Off by default."
        ),
    )
    audit.add_argument(
        "--reconcile",
        action="store_true",
        help=(
            "also compare the replay against the exchange's own positions and "
            "settlements (counts only). Off by default: it walks two more "
            "paginated collections."
        ),
    )
    audit.add_argument(
        "--show-sensitive-details",
        action="store_true",
        help=(
            "LOCAL ONLY: also print per-market classification detail. Refuses to "
            "run in CI. Never use this where the output could be captured."
        ),
    )
    _add_deliver_parser(sub)
    _add_series_probe_parser(sub)
    _add_backfill_parser(sub)
    _add_settle_parser(sub)
    return parser


def render_sensitive(result: AuditResult) -> str:
    """Render per-market detail for a private terminal."""
    lines = [SENSITIVE_BANNER, ""]
    if not result.details:
        lines.append("(no markets to detail)")
        return "\n".join(lines)
    lines.append(
        f"{'SPORT':<11} {'FILLS':>5}  {'MARKET':<38} {'COMPETITION':<24} REASON"
    )
    for detail in result.details:
        lines.append(
            f"{detail.sport.value:<11} {detail.fill_count:>5}  "
            f"{detail.market_ticker:<38} {(detail.competition or '-'):<24} {detail.reason}"
        )
        lines.append(
            f"{'':<11} {'':>5}    series={detail.series_ticker or '-'} "
            f"event={detail.event_ticker or '-'} "
            f"scope={detail.competition_scope or '-'} "
            f"resolved_by={detail.resolved_by.value if detail.resolved_by else '-'}"
        )
        for item in detail.evidence:
            lines.append(f"{'':<11} {'':>5}    evidence: {item}")
    return "\n".join(lines)


def _run_deliver(args, client, out, err) -> int:
    """Build the payload for every eligible wager and write it to disk.

    Prints COUNTS. The rows go to files, because the rows are the sensitive
    thing this whole system handles and a public Actions log is not where they
    belong -- the destination ledger the owner chose to publish is.
    """
    from .destination import ROUTER_IMPORT_BATCH_ID, write_payloads

    try:
        result = run_audit(
            client,
            max_fills=None,
            full_history=True,
            production=True,
            now=Decimal(int(time.time())),
            allow_stabilization=args.allow_stabilization,
            include_pre_cutover=(
                args.include_pre_cutover == PRE_CUTOVER_ACKNOWLEDGEMENT
            ),
        )
    except KalshiRouterError as exc:
        print(f"delivery failed: {type(exc).__name__}: {exc}", file=err)
        return EXIT_API

    print(result.production.render(), file=out)
    counts = write_payloads(result.production_wagers, args.out_dir)
    print("", file=out)
    print("payloads written (rows per destination; rows are NOT printed):", file=out)
    if not counts:
        print("  none -- nothing eligible", file=out)
    for sport, rows in sorted(counts.items()):
        print(f"  {sport}: {rows}", file=out)
    print(f"import batch id: {ROUTER_IMPORT_BATCH_ID}", file=out)

    print("", file=out)
    print(result.transport.render(), file=out)

    # A machine-readable health line, so a scheduled run can annotate itself
    # without a human reading the report. One token, on its own line, never a
    # count and never a market.
    print(f"HEALTH={result.production.health.value}", file=out)
    return EXIT_OK


def _run_backfill(args, client, out, err) -> int:
    """Inspect the gap window and reconcile it against the destinations.

    Writes NOTHING. This command exists to answer "what did we miss", and the
    answer has to be trustworthy before anything acts on it.
    """
    from .backfill import BACKFILL_IMPORT_BATCH_ID, BackfillWindow, reconcile
    from .ledger_compare import read_ledger

    try:
        window = BackfillWindow(args.since)
    except ValueError as exc:
        print(f"backfill window: {exc}", file=err)
        return EXIT_CONFIG

    ledgers: dict[str, list] = {}
    for spec in args.ledger:
        sport, _, path = spec.partition("=")
        if not sport or not path:
            print(f"--ledger expects SPORT=PATH; got {spec!r}", file=err)
            return EXIT_CONFIG
        ledgers[sport.strip().upper()] = read_ledger(path)

    try:
        result = run_audit(
            client,
            max_fills=None,
            full_history=True,
            backfill_window=window,
            now=Decimal(int(time.time())),
        )
    except KalshiRouterError as exc:
        print(f"backfill inspection failed: {type(exc).__name__}: {exc}", file=err)
        return EXIT_API

    print(f"backfill window: {window.start_iso}  ->  {window.end_iso} (the cutover)", file=out)
    print(f"import batch id: {BACKFILL_IMPORT_BATCH_ID}", file=out)
    print("", file=out)
    print(result.production.render(), file=out)

    wagers = result.production_wagers
    by_sport: dict[str, int] = {}
    for wager in wagers:
        by_sport[wager.sport] = by_sport.get(wager.sport, 0) + 1
    print("", file=out)
    print("reconstructed wagers by sport:", file=out)
    for sport in sorted(by_sport) or []:
        print(f"  {sport}: {by_sport[sport]}", file=out)
    if not by_sport:
        print("  none", file=out)

    print("", file=out)
    print(f"destination ledgers supplied: {sorted(ledgers) or 'none'}", file=out)
    # orders_considered on the production diagnostics IS the in-window order
    # count: evaluate_window hands evaluate_production exactly the candidates
    # the window selected.
    importable, diagnostics = reconcile(
        wagers, ledgers, frozenset(ledgers),
        orders_in_window=result.production.orders_considered,
    )
    print("", file=out)
    print(diagnostics.render(), file=out)

    if args.out_dir:
        from .destination import write_backfill_payloads

        # ONLY the importable wagers. `reconcile` already filtered to
        # MISSING_IMPORTABLE so a caller cannot get this wrong by forgetting,
        # and the destinations are exactly the ones a ledger was supplied for:
        # writing to a destination whose existing rows were never read is how a
        # backfill duplicates a ledger.
        counts = write_backfill_payloads(
            importable, args.out_dir, BACKFILL_IMPORT_BATCH_ID, frozenset(ledgers)
        )
        print("", file=out)
        print("payloads written (rows per destination; rows are NOT printed):", file=out)
        if not counts:
            print("  none -- nothing was missing and importable", file=out)
        for sport, rows in sorted(counts.items()):
            print(f"  {sport}: {rows}", file=out)

    print("", file=out)
    print(result.transport.render(), file=out)
    return EXIT_OK


def _walk_settlements(client) -> list:
    """Every settlement the exchange will hand over, normalized. Nothing else.

    A row this router cannot read is SKIPPED rather than raising: one
    uninterpretable settlement out of thousands should cost that settlement,
    not the whole pass -- and a wager whose settlement was skipped simply stays
    unsettled, which is a state the destinations already model.
    """
    from .models import SchemaError, normalize_settlement

    settlements = []
    for raw in client.iter_settlements():
        try:
            settlements.append(normalize_settlement(raw))
        except SchemaError:
            continue
    return settlements


def _run_settle(args, client, out, err) -> int:
    """Attribute the exchange's settlements to the gap window's wagers.

    Writes payload FILES for the destinations named, and prints counts. A
    settlement carries a ticker and a payout, so it is handled exactly as the
    wagers are: it reaches a destination importer, never this log.

    THE DESTINATIONS ARE NAMED, NOT INFERRED. MLB has its own canonical
    settlement driver -- `settle_markets.py` re-derives every outcome from the
    MLB Stats API -- and emitting settlements there would create a second
    authority on the same fact. So this command emits for exactly the
    destinations asked for and refuses to run with none.
    """
    from .backfill import BackfillWindow
    from .destination import write_settlement_payloads
    from .settlement import settle_batch

    destinations = {d.strip().upper() for d in args.destination if d.strip()}
    if not destinations:
        print(
            "--destination is required and repeatable; name every destination "
            "that should receive settlements. Nothing is implied, and MLB is "
            "deliberately not a default: it has its own settlement driver.",
            file=err,
        )
        return EXIT_CONFIG

    try:
        window = BackfillWindow(args.since)
    except ValueError as exc:
        print(f"settlement window: {exc}", file=err)
        return EXIT_CONFIG

    # THE SETTLEMENTS ARE WALKED DIRECTLY, NOT THROUGH reconcile=True.
    #
    # That flag is the accounting engine's switch, and it buys four things:
    # an unbounded settlements walk, two archive-reach probes, and the whole
    # position view -- because the REPLAY needs all of them to decide whether a
    # market ever closed. This command reads none of it. It needs the
    # settlements and nothing else, and it already knows which wagers it is
    # attributing them to.
    #
    # MEASURED, from the run timestamps:
    #
    #   backfill-settle  run 1, step 7   21:06:26 -> 21:15:18   8m52s, CANCELLED
    #   backfill-deliver run 2, step 8   20:11:33 -> 20:15:24   3m51s, completed
    #   backfill-deliver run 3, step 8   20:22:48 -> 20:27:30   4m42s, completed
    #   backfill-deliver run 4, step 8   20:54:43 -> 21:01:43   7m00s, completed
    #
    # The settlement step had run 8m52s WITHOUT FINISHING when it was cancelled
    # by hand -- it did not reach the job timeout. The wager pass over the same
    # window finishes in under seven. So the cost is real and the exact excess
    # is unknown, because the run never completed to be measured.
    #
    # Two walks here instead of two walks plus a replay, two probes and a
    # position fetch.
    try:
        result = run_audit(
            client,
            max_fills=None,
            full_history=True,
            backfill_window=window,
            now=Decimal(int(time.time())),
        )
        settlements_walked = _walk_settlements(client)
    except KalshiRouterError as exc:
        print(f"settlement pass failed: {type(exc).__name__}: {exc}", file=err)
        return EXIT_API

    print(f"settlement window: {window.start_iso}  ->  {window.end_iso} (the cutover)", file=out)
    print(f"destinations: {sorted(destinations)}", file=out)
    print("", file=out)

    wagers = [w for w in result.production_wagers if w.sport in destinations]
    # Latest settlement per ticker. A market settles once, so a second row for
    # one ticker is a re-observation rather than a second event; taking the
    # last keeps the most recently reported state without merging two.
    by_ticker = {s.ticker: s for s in settlements_walked}

    settlements = settle_batch(wagers, by_ticker)

    established = sum(1 for s in settlements if s.is_established)
    settled = sum(1 for s in settlements if s.settlement_status == "SETTLED")
    reasons: dict[str, int] = {}
    for one in settlements:
        for reason in one.refusals:
            reasons[reason] = reasons.get(reason, 0) + 1

    print("settlement attribution (counts only; payouts are NOT printed):", file=out)
    print(f"  wagers in the window for these destinations: {len(wagers)}", file=out)
    print(f"  settled: {settled}", file=out)
    print(f"  not settled: {len(wagers) - settled}", file=out)
    print(f"  profit and loss established: {established}", file=out)
    print(f"  profit and loss UNESTABLISHED: {len(wagers) - established}", file=out)
    for reason in sorted(reasons):
        print(f"    {reason}: {reasons[reason]}", file=out)

    if args.out_dir:
        # Only SETTLED rows cross. An unsettled wager is recorded downstream by
        # having no settlement row at all, so sending a PENDING one would be
        # sending a row the destination is built to refuse.
        deliverable = [s for s in settlements if s.settlement_status == "SETTLED"]
        sports = {w.source_key: w.sport for w in wagers}
        counts = write_settlement_payloads(deliverable, args.out_dir, sports)
        print("", file=out)
        print("settlement payloads written (rows per destination; rows are NOT printed):", file=out)
        if not counts:
            print("  none -- nothing in the window has settled", file=out)
        for sport, rows in sorted(counts.items()):
            print(f"  {sport}: {rows}", file=out)

    print("", file=out)
    print(result.transport.render(), file=out)
    return EXIT_OK


def _run_series_probe(client, out, err) -> int:
    """Corroborate candidate series tickers against Kalshi's own catalogue.

    Promotes nothing. The registry is edited by a human reading this output,
    because a table the classifier treats as authoritative should not be
    written by the same run that decided it wanted more entries.
    """
    from .classify import (
        competitions_under_our_sports,
        describe_competition_collisions,
        measure_competition_collisions,
        render_collision_details,
        render_competitions_under_our_sports,
    )
    from .series_probe import MLB_LEDGER_CANDIDATES, probe_series
    from .sports import Sport
    from .taxonomy import parse_filters_by_sport

    candidates = {t: Sport.MLB for t in MLB_LEDGER_CANDIDATES}
    try:
        report = probe_series(client, candidates)
    except KalshiRouterError as exc:
        print(f"series probe failed: {type(exc).__name__}: {exc}", file=err)
        return EXIT_API

    print(report.render(), file=out)

    # The OTHER lever on these markets, measured in the same run because it
    # costs one request. The series registry and the taxonomy ambiguity gate
    # are the only two things standing between these wagers and a
    # classification, and a decision about either needs both numbers.
    #
    # CollisionStructure has been built for a while and never actually run
    # against Kalshi's live taxonomy, so "the collisions are real" has been an
    # assumption rather than a measurement.
    try:
        taxonomy = parse_filters_by_sport(client.get_filters_by_sport())
    except KalshiRouterError as exc:
        print("", file=out)
        print(f"taxonomy unavailable: {type(exc).__name__}", file=out)
        return EXIT_OK

    print("", file=out)
    print(f"taxonomy: {taxonomy.sport_count} sports, "
          f"{taxonomy.competition_count} unambiguous competitions, "
          f"{taxonomy.collision_count} collisions", file=out)
    print("", file=out)
    print(measure_competition_collisions(taxonomy).render(), file=out)
    # The counts say what SHAPE the collisions are; this says which they are.
    # Without it "one nominal collision" is a claim nobody can check, and a
    # reader looking for one sport can read it as being about that sport.
    print("", file=out)
    print(render_collision_details(describe_competition_collisions(taxonomy)), file=out)
    # Whether "Pro Baseball" is MLB-specific is decided by what ELSE the
    # catalogue files under Baseball, not by our own mapping table.
    print("", file=out)
    print(render_competitions_under_our_sports(
        competitions_under_our_sports(taxonomy)), file=out)
    return EXIT_OK


def main(argv: list[str] | None = None, stdout=None, stderr=None) -> int:
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    args = build_parser().parse_args(argv)

    if getattr(args, "include_pre_cutover", None) is not None:
        if args.include_pre_cutover != PRE_CUTOVER_ACKNOWLEDGEMENT:
            # Refuse rather than fall back to the safe behaviour: someone who
            # typed the flag intends to import history, and silently doing the
            # normal thing would look like it worked.
            print(
                "--include-pre-cutover requires the exact acknowledgement "
                f"{PRE_CUTOVER_ACKNOWLEDGEMENT!r}.",
                file=err,
            )
            return EXIT_CONFIG

    if getattr(args, "compare_ledger", None) and not args.shadow_wagers:
        # Failing here rather than silently comparing an empty set: a run that
        # printed "0 matched, 457 ledger only" because no wagers were built
        # would read as a catastrophic disagreement instead of as a missing
        # flag, and someone would act on it.
        print(
            "--compare-ledger requires --shadow-wagers: there is nothing to "
            "compare the ledger against otherwise.",
            file=err,
        )
        return EXIT_CONFIG

    if args.show_sensitive_details:
        try:
            assert_sensitive_output_allowed()
        except SensitiveOutputRefused as exc:
            print(str(exc), file=err)
            return EXIT_SENSITIVE_REFUSED

    try:
        key_id, private_key = read_credentials()
        config = config_from_args(args)
        signer = KalshiSigner(key_id, private_key)
    except ConfigurationError as exc:
        print(f"configuration error: {exc}", file=err)
        return EXIT_CONFIG

    client = KalshiReadOnlyClient(signer=signer, config=config)

    if args.command == "deliver":
        return _run_deliver(args, client, out, err)

    if args.command == "series-probe":
        return _run_series_probe(client, out, err)

    if args.command == "backfill":
        return _run_backfill(args, client, out, err)

    if args.command == "settle":
        return _run_settle(args, client, out, err)

    try:
        result = run_audit(
            client,
            max_fills=args.max_fills,
            collect_details=args.show_sensitive_details,
            reconcile=args.reconcile,
            full_history=args.full_history,
            shadow_wagers=args.shadow_wagers,
            compare_ledger_path=args.compare_ledger,
            compare_ledger_sport=args.compare_ledger_sport,
            production=args.production,
            now=Decimal(int(time.time())) if args.production else None,
            max_classify_markets=args.max_classify_markets,
        )
    except KalshiRouterError as exc:
        # Message text is constructed to be non-secret; see errors module.
        print(f"audit failed: {type(exc).__name__}: {exc}", file=err)
        return EXIT_API

    if args.json:
        payload = dict(result.report.as_dict())
        payload.update(
            {f"accounting_{k}": v for k, v in result.accounting.as_dict().items()}
        )
        payload.update({f"schema_{k}": v for k, v in result.coverage.as_dict().items()})
        payload.update({f"history_{k}": v for k, v in result.history.as_dict().items()})
        payload.update({f"wager_{k}": v for k, v in result.wagers.as_dict().items()})
        payload.update(
            {f"collision_{k}": v for k, v in result.collisions.as_dict().items()}
        )
        payload.update(
            {f"transport_{k}": v for k, v in result.transport.as_dict().items()}
        )
        if result.ledger_comparison is not None:
            payload.update(
                {f"ledger_{k}": v for k, v in result.ledger_comparison.as_dict().items()}
            )
        payload.update({f"finality_{k}": v for k, v in result.finality.as_dict().items()})
        payload.update(
            {f"production_{k}": v for k, v in result.production.as_dict().items()}
        )
        payload.update(
            {
                f"settlement_coverage_{k}": v
                for k, v in result.settlement_coverage.as_dict().items()
            }
        )
        if result.reconciliation is not None:
            payload.update(
                {f"reconcile_{k}": v for k, v in result.reconciliation.as_dict().items()}
            )
        print(json.dumps(payload, indent=2, sort_keys=True), file=out)
    else:
        print(result.report.render(), file=out)
        print("", file=out)
        print(result.coverage.render(), file=out)
        print("", file=out)
        print(result.accounting.render(), file=out)
        print("", file=out)
        print(result.history.render(), file=out)
        print("", file=out)
        print(result.finality.render(), file=out)
        print("", file=out)
        print(result.collisions.render(), file=out)
        print("", file=out)
        print(result.transport.render(), file=out)
        if args.production:
            print("", file=out)
            print(result.production.render(), file=out)
        if args.shadow_wagers:
            print("", file=out)
            print(result.wagers.render(), file=out)
        if result.ledger_comparison is not None:
            print("", file=out)
            print(result.ledger_comparison.render(), file=out)
        if result.reconciliation is not None:
            print("", file=out)
            print(result.settlement_coverage.render(), file=out)
            print("", file=out)
            print(result.reconciliation.render(), file=out)

    if args.show_sensitive_details:
        print("", file=out)
        print(render_sensitive(result), file=out)

    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
