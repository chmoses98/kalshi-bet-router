# Downstream ledger repositories — Phase F audit

What each sport repository can actually accept today, established by reading the
repositories rather than by assuming they are symmetric. They are not.

Audited at:

| Repo | Sport | Commit audited |
|---|---|---|
| `chmoses98/edge-finder-api` | MLB | working copy |
| `chmoses98/nfl-edge-finder` | NFL | `b0368f35364020a9610d5d559ffb1de0c008f35e` |
| `chmoses98/cfb-edge-finder` | CFB | `5b687b800fafbf2d006ef442c93dd92ea6703770` |
| `chmoses98/Tennis-Edge-Finder` | Tennis | `625bc557567a856414ae5b960c81a373a377b1e4` |

## Headline: only one of four has an importer

| Repo | Canonical wager importer | Wager ledger |
|---|---|---|
| MLB | **yes** — `scripts/edgelab/import_bet_batch.py` | `data/edgelab/bets/bets.jsonl` |
| NFL | **none** | none found |
| CFB | **none** | none — `src/cfb_edge_finder/betting/` is an explicit stub |
| Tennis | **none** | `tennis_edge/ledger/` is predictions/CLV, not wagers |

A repo-wide search for `sourceBetKey`, `source_bet_key`, `importBatchId`,
`import_bet`, `write_placed_bet` and `bets.jsonl` returns nothing in NFL and
Tennis. In CFB it matches exactly one file — `docs/MLB_ARCHITECTURE_AUDIT.md`,
which describes MLB's contract as a pattern to adopt later, not as code that
exists. CFB's `betting/__init__.py` says so directly: *"Not implemented in this
foundation phase."*

**Consequence for routing.** Three of the four sports have nowhere to route to.
Writing wagers into them means *designing* their ledger contract first, which is
a deliberate act requiring the owner's intent — not something to infer from an
arbitrary JSON file that happens to exist. Until then, only MLB is a candidate
destination, and the router must refuse the others rather than improvise.

## MLB — the contract to mirror

`scripts/edgelab/import_bet_batch.py` is the reference, and it already solves
most of what a router needs:

* payload is `{"importBatchId": ..., "rows": [...]}`;
* `importBatchId` plus a per-row `sourceBetKey` are **required** when a row omits
  `entryTimestamp`;
* identity is `hash(importBatchId, sourceBetKey, marketTicker, side)`;
* **all** writes go through `lib.edgelab.bets.write_placed_bet`;
* re-running an identical batch is a pure no-op (`DUPLICATE_NOOP`);
* an ambiguous ticker leaves the row **UNRESOLVED** with its candidates, rather
  than picking one.

That last two properties are what make a router safe to re-run, and they are the
properties the other three repos do not have yet.

## Two cross-repo inconsistencies worth settling before Phase J

**1. Settlement is not always binary.** CFB models it as binary and in floating
point:

```python
# src/cfb_edge_finder/research/attribution.py
settlement_value = 1.0 if won else 0.0
```

Tennis contradicts this from live exchange data, in `tennis_edge/ledger/truth.py`:

> *"Verified on 2026-09-11 discovery data: 1,836 of ~61k finalized live-tier
> tennis markets settled as `result="scalar"` with `settlement_value_dollars`
> strictly between 0 and 1 (walkovers / cancellations / incomplete scopes
> resolved to a 'fair price' by the exchange)."*

So a `0.0`/`1.0` assumption will silently misprice every walkover and
cancellation. Tennis also keeps `SportsTruth` (what happened on court) separate
from `ExchangeTruth` (what Kalshi paid), which is the right shape: a retirement
has a sporting result and a scalar settlement, and they are not the same fact.

**2. Money types differ.** The router is Decimal-only by rule. CFB carries
prices and settlement values as `float` in its schemas. Any value crossing that
boundary needs an explicit exact conversion, not an implicit one.

## Tennis classification — evidence exists, and it is not the endpoint we use

The live audit has classified **0** tennis fills so far, so tennis remains
unproven *for this account*. But the Tennis repo has already done the discovery
work against the exchange, and it used a different endpoint than this router's
L2 layer:

* `GET /series?include_product_metadata=true` returns every series with `tags`
  and `category` — verified against a **13,959-series** catalogue snapshot
  (2026-09-10), of which 143 are tennis-tagged.
* Classification is by the literal tag `"Tennis"` first, with title/ticker
  regex only as a secondary net, and an untagged match surfaced as a coverage
  failure rather than silently accepted.
* The exchange's own `"Tennis"` tag also covers **pickleball**, which that repo
  flags explicitly instead of pricing it as tennis.
* `KXWTAX` ("Wealth tax") is called out as a trap that a naive `KXWTA*` prefix
  rule would wrongly claim — a direct warning about ticker-prefix heuristics of
  the kind this router's unverified L5 registry relies on.

This is the strongest available lead for repairing L2, and it is evidence, not a
guess: it names an endpoint and a field that were checked against the full
catalogue. It is recorded here rather than implemented, because adopting it is a
change to the evidence hierarchy and belongs with the L2 repair.
