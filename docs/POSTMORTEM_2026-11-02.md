# Betting postmortem 2026-11-02 — status (issues #116 / cfb #248 / nfl #231 / nba #85)

Status values: `DONE` (needs evidence), `PARTIAL` / `BLOCKED` / `NEEDS_OWNER_DECISION` (need a blocker),
`NOT_STARTED`. Machine form: `kalshi_router.postmortem_report` (`postmortem_report.v1`), which refuses a `DONE`
without evidence. **Statuses below are placeholders to be filled at the end of the mission.**

| # | Deliverable | Router | CFB | NFL | NBA |
|---|---|---|---|---|---|
| 1 | Immutable per-contract reconciliation | _router module shipped: `contract_reconciliation`, `combo_decode`_ | _fill_ | _fill_ | n/a |
| 2 | Stale/kickoff guards, price provenance, fee-inclusive break-even, calibrated fair value, bet-up-to | n/a | _fill_ | _fill_ | n/a |
| 3 | Correlation aggregation (>5U/game, >15U/day flags) | n/a | _fill_ | _fill_ | n/a |
| 4 | Key-number distributions, opponent-adjusted pace/EPA | n/a | _fill_ | _fill_ | n/a |
| 5 | PIT backtest vs price-only baseline and CLV | n/a | _fill_ | _fill_ | n/a |
| 6 | NBA PR #48 linkage | _clone-retry fix_ | n/a | n/a | _owner merges PR #48_ |
| 7 | Tests, report, dashboard, PRs | _this PR_ | _fill_ | _fill_ | _fill_ |

## What the router work establishes

* **Money identity.** `stake` already contains the entry fee; `net = gross − stake`. Fees are reported, never
  subtracted again. `contract_reconciliation.economics_check` flags (never repairs) a net that disagrees.
* **Combos.** `combo_decode.decode_combo` returns the legs the exchange stated and each leg's sport from its own
  metadata. Cross-category combos are `DECODED_CROSS_CATEGORY` with `sport = None` — reported separately, never
  defaulted into one ledger. No stated legs / unresolvable leg ⇒ `UNDECODED_COMBO`.
* **NBA linkage.** NBA is an observation destination (`auto_merge=False`). PR #48 (47 wagers) is pending *by
  design*; its settlements are `WAITING_FOR_PARENT_WAGER` (withheld, re-offered each run, not red) while the
  proposal is an open, non-draft PR whose head is the commit read. Merging PR #48 is the owner's decision.
* **Workflow resilience.** Run 41494783124 died on a transient `curl 56` while cloning MLB; under `set -e` that
  skipped every later destination (NBA, NFL, …). Clones in `deliver-wagers.yml` and `settle-wagers.yml` now retry
  with backoff and, on exhaustion, fail only that sport.
