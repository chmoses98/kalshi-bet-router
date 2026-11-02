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

## Evidence capture: order ids, fill ids, timestamps, closing prices (added after owner review of PR #117)

**Built (additive, inert):** `kalshi_router/execution_evidence.py` produces a *sidecar* row per production wager, keyed
by the same `source_bet_key` the wager row carries (`kalshi:v1:` + digest of subaccount, ticker, order id):
`order_id_digest`, `fill_id_digests`, `fill_count`, `first_fill_ts`, `last_fill_ts`, `execution_action` (BUY/SELL when the
exchange still reports the deprecated verb), `all_taker`, and `flags` (`FILL_IDS_MISSING`, `ACTION_NOT_ESTABLISHED`,
`TAKER_NOT_ESTABLISHED`). Ids are **domain-separated SHA-256 digests**, never raw: the README's security boundary lists order
and fill ids as private and every downstream ledger is public. The owner can recompute `sha256("kalshi-router.execution-evidence.v1|order|<id>")[:32]`
from their own Kalshi account to match a row. Nothing here changes a wager row, an identity or a delivery.

**Not landed in any ledger yet, and exactly why:**
1. `contract/edge_finder_contract/routed_ledger.py::WAGER_INPUT_FIELDS` is a closed list and the importer raises
   `RowRefused: unknown field(s)`. Each destination vendors that file, so emitting new keys in a wager row would make
   every destination refuse the row until it re-vendors. **Required destination change:** add optional
   `order_id_digest`, `fill_id_digests`, `first_fill_ts`, `last_fill_ts`, `all_taker` to the contract (versioned, additive,
   not part of `WAGER_CANONICAL_FIELDS` so identity and the DUPLICATE_NOOP comparison are unchanged), re-vendor into
   cfb/nfl/nba/nhl/soccer/tennis, *then* have the row builders emit them. Until then the sidecar is the carrier.
2. **Recommendation id and model timestamps cannot come from the router, by design.** `PROVENANCE_FIELDS` forbids
   `recommendation_id`, `model_*`, `projection_id` and friends in a routed row ("recording is not endorsing"). The link is
   made on the edge-finder side by market + side + time window using these digests' `first_fill_ts`; the router never asserts it.
3. **Closing price is not capturable with the current read-only client.** It exposes `get_market`/`get_event`/`get_series`
   only. A settled market's `last_price` is the 0/1 resolution, not a pre-close quote; a true close needs a candlestick or
   order-book-history endpoint that the client's allow-list does not include. Adding one is a deliberate widening of the
   read-only surface and needs owner approval. Closing prices for the window must come from the edge-finders' own captures.

## Post-merge NBA ingestion (PR #48 merged 2026-11-02T21:04:21Z)

* `nba-edge-finder@accounting-data` tip `63a868e` `wagers.jsonl`: **47 rows, 47 unique `source_bet_key`, 47 unique `wager_id`,
  byte-identical to PR #48's head file**; `settlements.jsonl` is 0 bytes (settlements are on an open PR, below).
* Router settle run [41523343208](https://github.com/chmoses98/kalshi-bet-router/actions/runs/41523343208) (main; its delivery
  step ran 21:16:28-21:17:07Z, after the merge): NBA `settlement parents: 47 settled rows, canonical 47, WAITING_FOR_PARENT_WAGER 0,
  no valid parent 0`; importer `NEW: 47, failed 0`; destination validator `wagers=47 settlements=47 problems=0`;
  second identical import changed nothing; branch `kalshi-router/settle-NBA` pushed and nba-edge-finder PR #86 opened; merge gate
  verdict `MERGE` but `HELD FOR OBSERVATION` (`auto_merge=False`), so it is **not** merged; reconciliation `on ledger 0, proposed
  not merged 47, UNACCOUNTED 0`. Same run: CFB 290, NFL 228, NHL 35, SOCCER 9 all `DUPLICATE_NOOP`; `destinations that failed: 0`.
