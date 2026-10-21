# NHL routed-wager recording gaps (findings from the SIFT NHL model-improvement research)

**DOCS ONLY — no router code, destination, ledger schema, delivery or merge behaviour is changed by this file.** Findings come from a read-only audit of the 34 NHL wagers
recorded through 2026-10-20 (see `chmoses98/NHL-edge-finder`, branch `claude/nhl-model-improvement-f6kn0k`, `docs/research/nhl-model-improvement/ACTUAL_WAGER_AUDIT.md`).
Line references are to this repository at `4b7aaa2`.

## What the NHL import row cannot say

`to_nhl_import_row` (`production.py:979`) emits 13 fields plus `wager_id` and `schema_version`. Not carried: the exchange **order id** (only the one-way `source_bet_key`), fill ids, order
status, order-created / last-fill / ledger-ingest times, maker-taker flag, and the **buy/sell action**. `side` is the contract traded, not the exposure: `EXPOSURE_SIDE_DESTINATIONS` is empty
(`production.py:590`), so a sell of YES is filed like a purchase of YES, and `ProductionRefusal.REDUCTION_NOT_REPRESENTABLE` (`production.py:275`) is defined but never returned.
Consequence: of 34 NHL wagers, entries vs exits cannot be proven from any record (31 are first orders on their ticker and so entries *by inference*; 1 exit, 1 partial close and 1 unknown were
identified only by offsetting size). `executed_at` is the exchange `created_time` of the order's first fill; no ledger-observed time exists. There is no recommendation/provenance field,
by design (the importer refuses them), so a wager cannot be tied to a SIFT recommendation except heuristically by ticker/side/time.

## What is verified correct

The client is read-only (`client.py:121-156`, GET only, allow-listed paths); fees recorded on the 34 NHL fills equal `0.07·c·p·(1−p)` within $0.0004; settlement matches an independent resolver on 34/34.

## Proposal (needs a decision; not implemented here)

1. Add `exchange_action` (`buy`/`sell`), `order_id`, `order_created_at`, `last_fill_at`, `ledger_ingested_at` and `taker` to the *raw* router record, and either refuse or represent reductions instead of filing them as purchases.
2. Allow an optional opaque `recommendation_ref` (card hash + ticker + side), or an explicit `MANUAL` marker, supplied by the owner at entry — never inferred.
3. Reconcile the docs with the code on NHL auto-merge: commit `fce1690` graduates NHL; the research reviewed an earlier state in which docs and code disagreed.
