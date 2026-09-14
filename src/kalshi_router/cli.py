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

SENSITIVE_BANNER = """
!!  SENSITIVE LOCAL DIAGNOSTICS  !!
The block below identifies individual markets you traded. It is private betting
activity. Do not paste it into an issue, a pull request, a chat, or any log.
""".strip()


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


def main(argv: list[str] | None = None, stdout=None, stderr=None) -> int:
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    args = build_parser().parse_args(argv)

    if args.show_sensitive_details:
        try:
            assert_sensitive_output_allowed()
        except SensitiveOutputRefused as exc:
            print(str(exc), file=err)
            return EXIT_SENSITIVE_REFUSED

    try:
        key_id, private_key = read_credentials()
        config = AuditConfig(
            base_url=base_url_from_env(),
            max_fills=args.max_fills,
            page_limit=args.page_limit,
        )
        signer = KalshiSigner(key_id, private_key)
    except ConfigurationError as exc:
        print(f"configuration error: {exc}", file=err)
        return EXIT_CONFIG

    client = KalshiReadOnlyClient(signer=signer, config=config)

    try:
        result = run_audit(
            client,
            max_fills=args.max_fills,
            collect_details=args.show_sensitive_details,
            reconcile=args.reconcile,
            full_history=args.full_history,
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
