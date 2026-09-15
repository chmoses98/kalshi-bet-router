# Mission closeout — production routing and the historical gap

**Status: CLOSED, 2026-09-15.**

Every number in this document is from a live run against the real account or
from a merged commit in a destination repository, and each names its source.
Nothing here is an estimate. Where evidence does not cover something, this
document says so rather than filling the gap.

---

## 1. The production cutover

```
src/kalshi_router/production.py
PRODUCTION_CUTOVER_ISO = "2026-09-15T00:00:00Z"
```

It has never moved. `git log --all -S'PRODUCTION_CUTOVER_ISO = '` over the whole
history returns only the commit that introduced the constant and its squash.

It is **structurally** fixed, not merely unchanged:

* `BackfillWindow.end_iso` is a property returning the constant. It takes no
  argument and has no setter, so no caller can widen the window.
* `tests/test_backfill.py` pins the literal in both modules that name it.
* `test_backfill_has_no_flag_for_the_window_end` and
  `test_settle_has_no_flag_for_the_window_end` forbid an end flag on either CLI.
* `test_the_catch_up_cannot_widen_its_own_window` checks the workflow's own
  invocation passes only `--since`, and forbids `--until`, `--end`, `--cutover`
  and `--include-pre-cutover` on it.

Membership is decided on an order's **first execution**, the same instant the
production filter keys on: a late fill on an early order does not move a wager
into the window.

## 2. The first natural production wager

The first real post-cutover wager arrived on a scheduled run and the delivery
**failed**. Root cause, reproduced exactly rather than guessed:

```
git rev-parse <missing-ref>
```

A bare `git rev-parse` **echoes its argument to stdout** before failing, so the
`--force-with-lease` expectation became the literal string
`refs/remotes/origin/kalshi-router/MLB` and the push died with "cannot parse
expected object name" — *after* the destination importer had already returned a
canonical bet id. The `|| echo ""` never helped: `rev-parse` had written to
stdout before it ran.

The fix is `--verify --quiet`, which prints nothing and exits 1 on a missing
ref. `deliver-wagers.yml` and `recover-wagers.yml` carried the identical defect
and both were fixed.

The old failed run was never re-run as acceptance. A fresh delivery proved the
whole chain: real Kalshi wager → payload → MLB classification → finality →
the destination's own canonical importer → canonical bet id → branch → pull
request → merge.

## 3. The canonical MLB bet id

```
9670568c90dbd3d37dab8cd68a459e2e9b830d85
importBatchId  kalshi-router-v1
entryMethod    IMPORTED_RECEIPT
```

Present once in `data/edgelab/bets/bets.jsonl` on `main`. It later settled as a
WIN with `netProfitLoss 12.26`.

Fixing the execution-economics plumbing did **not** change it, which is the
property that matters: the destination derives identity from
`hash(importBatchId, sourceBetKey, marketTicker, side)` and **no economics
field participates**. Economics are not identity.

## 4. Production idempotency

A second delivery against the merged ledger:

```
no ledger change -- every row was already imported (no-op).
destinations that failed: 0
```

No second pull request, no second wager row. One `sourceBetKey`, one `betId`.

## 5. The historical gap, by sport

Window `2026-09-11T00:00:00Z → 2026-09-15T00:00:00Z`.

```
orders in the window: 76
refused before reconciliation: 0
reconciled: 74
  EXACT_EXISTING (owner already recorded it)   0
  DUPLICATE_NOOP (this backfill already wrote) 0
  MISSING_IMPORTABLE (written)                74
  AMBIGUOUS (refused)                          0
  CONFLICT (refused, never overwritten)        0
  UNSUPPORTED_DESTINATION                      0

MLB    32      NFL    24      CFB    18      Tennis  0
```

`EXACT_EXISTING: 0` is the verdict a broken matcher would also produce, so it
was checked against the ledgers rather than trusted: the latest `gameDate`
among the MLB ledger's hand-entered and legacy rows is **2026-09-10**, one day
before the window opens. The owner's accounting genuinely stopped there.

Destination commits: MLB `823513da`, NFL `c4d93c72`, CFB `fd8aef27`.

Tennis: **zero wagers classified, nothing written**, and its research-authority
rules were not weakened to accommodate one. That is not the claim that no
Tennis bets exist — see §11.

## 6. Destination canonical importer architecture

The router **never edits a ledger file** and never pushes to a destination's
own branch. It hands a payload to that destination's own importer, commits only
what the importer produced onto a branch in its own namespace, and opens a pull
request. Bypassing the importer would bypass its duplicate detection, its
ticker resolution and its validation — the properties that make a re-run safe.

The three destinations agree on nothing, and each row is built against its
own schema:

| | ledger | branch | importer lives on | price field |
|---|---|---|---|---|
| MLB | one JSONL | `main` | `main` | `entryPrice` (camelCase) |
| NFL | one JSON file per record | `handicap-data` | `main` | `actual_price` |
| CFB | one JSONL per season | `accounting-data` | `main` | `execution_price` |

Assuming they agreed is precisely how the MLB economics once became null. NFL
and CFB keep their importers on `main` while their ledgers live on a data
branch, so each delivery takes **two checkouts** — conflating them runs an
importer that does not exist on the branch it is writing.

**Reading is the same split.** `LEDGER_VOCABULARIES` states how to read each
destination's existing rows, and a sport with no known vocabulary raises rather
than being read in another's words. This is not cosmetic: read in MLB's
camelCase, every lookup against the NFL and CFB ledgers returns `None`, nothing
raises, and every already-written wager comes back `MISSING_IMPORTABLE` — the
second run would have duplicated both ledgers and reported success.
`set(ROW_BUILDERS) == set(LEDGER_VOCABULARIES)` is asserted by test.

## 7. Destination-owned identity

The router does not name records in other people's ledgers. Each destination
mints its own, **deterministically from `source_bet_key` alone** — not the
stake, the price, the fee or the import batch:

```
MLB   betId              hash(importBatchId, sourceBetKey, marketTicker, side)
NFL   imported_wager_id  routed-<sha256(source_bet_key)[:24]>
CFB   wager_id           routed-<sha256(source_bet_key)[:24]>
      settlement ids     stl-<sha256(source_bet_key)[:24]>
```

So a corrected fee lands on the **same** record rather than beside it, and the
same order noticed by a second backfill is the same wager.

`sourceBetKey` is `kalshi:v1:<sha256 of the order>` — 74 distinct keys, one per
order, derived from the venue's own order identity and stable across runs.

Dedup is **economic** against rows the owner entered by hand (`(marketTicker,
side)` within stake and price tolerances) and **key-based** against rows this
backfill wrote. That split is what makes `EXACT_EXISTING` and `DUPLICATE_NOOP`
mean different things.

`importBatchId` for the whole catch-up:

```
kalshi-gap-backfill-2026-09-12-through-production-cutover-v1
```

One constant on every row of every destination. A timestamp would give the same
wager a new identity each run; a content digest would re-identify the entire
batch whenever one wager was added.

## 8. Settlement counts

```
MLB   32 wagers -> 28 SETTLED, 4 PENDING     15 won, 13 lost
NFL   24 wagers -> 24 SETTLED, 0 PENDING     15 won,  9 lost
CFB   18 wagers -> 18 SETTLED, 0 PENDING     14 won,  4 lost
```

MLB settled through **edge-finder-api's own** driver
(`reconcile_settled_bets_from_archive.py`), which performs no fetch and runs the
same `settle_bets_for_ticker` / `bet_needs_settlement_update` /
`upsert_records` path `settle_markets.py` runs. That run settled 37 bets in
total: 29 on the gap window's dates and 8 already pending from 2026-09-07 and
2026-09-10. The tool has no date filter; the split is stated rather than folded
into one number.

NFL and CFB settled through the router's own attribution pass. MLB is
deliberately **not** a destination of that pass — it has its own settlement
driver, and a second one would be a second authority on the same fact. The CLI
requires `--destination` and has no default, so this cannot silently become
"everywhere".

## 9. Established versus unestablished P&L

```
MLB   -115.77    28 of 32 established
NFL    +89.09    22 of 24 established
CFB   +279.81    18 of 18 established
```

A per-wager return is **exact**, not apportioned: the exchange publishes
`value`, the market's settlement price for a YES contract, so

```
gross return = contracts × (value if YES else 1 − value)
```

is a published per-contract price applied to a quantity read from the order's
own fills. Two orders on one market each get an exact return, and their returns
sum to the position's revenue.

The MLB postmortems for the four gap dates total **−103.51**, which differs from
the gap window's −115.77 by exactly **12.26** — bet `9670568c9`, delivered by
*production* under `kalshi-router-v1`, which also fell on 2026-09-14. A
postmortem counts every bet on its date; the backfill's figure counts one import
batch. Two nearly-equal numbers measuring different things is how a
reconciliation quietly stops being one.

## 10. Why no cross-sport total is stated

The evidence does not cover all 74 wagers. MLB has 4 unsettled and NFL has 2
unestablished, so any sum would cover **66 of 74** while being printed as though
it covered all of them.

That is the same rule both accounting reports enforce within a sport: nothing is
printed until every wager's P&L is established, and until then the report states
how many are missing and says outright that no season total is being stated. It
fires on real data — the NFL report currently prints
`UNESTABLISHED for 2 of 24 wagers` and states no season total.

## 11. The two classification refusals

Two of the 76 in-window orders are refused, on two markets, with reason
`competition absent`: Kalshi's own event metadata carries no competition, so
nothing establishes a sport.

They **remain refused**. They were not guessed into a sport, and no gate was
weakened to recover them. `COMPETITION_AMBIGUOUS` or `COMPETITION_ABSENT` is
evidence that classification failed — never evidence that any particular sport
is the answer.

This is also why Tennis's zero is not the claim that the owner placed no Tennis
bets: these two orders could be any sport.

## 12. The four MLB pending settlements

```
4 wagers, 3 distinct markets, 45.74 of stake
KXMLBKS-26SEP111910KCBOS-BOSSGRAY54-6
KXMLBKS-26SEP112010CLEMIN-MINTBRADLEY26-6
KXMLBKS-26SEP121335NYMNYY-NYYGCOLE45-6      (carries two of the four)
```

All four are `marketFamily: pitcher_strikeouts`, and
`reconcile_settled_bets_from_archive._is_player_prop` returns `True` for every
one. They are skipped under `PLAYER_PROP_ISSUE_43`:

```python
# GitHub issue #43 -- these have no settlement implementation at all and
# must remain pending regardless of what any archive appears to say.
PLAYER_PROP_TICKER_PREFIXES = ("KXMLBKS-", "KXMLBOUTS-")
```

**They are not pending for want of archived evidence.** All three markets have
archived `SETTLED` records. The repository refuses to settle player props
*regardless of what the archive appears to say*, because the settlement
semantics are not implemented — a stronger guarantee than missing evidence, and
the reason is recorded here so nobody later "fixes" it by reading the archive
directly.

## 13. The two NFL shared-position-fee refusals

24 NFL wagers cover **23** distinct market/side pairs: one market carries two of
the owner's orders.

A Kalshi settlement is per **market and position**, not per order. A settlement
**fee** is charged on the position and is not a per-contract price, so
apportioning it between two orders would be splitting a lump sum — producing a
number indistinguishable from an exchange-stated one for a quantity the exchange
never stated per order.

So the **net** P&L is refused for those two, with reason `shared_position_fee`
recorded on the row, and the **gross** return is still stated — it rests on the
published settlement price and does not depend on the split. Refusing the part
that cannot be established is not a reason to discard the part that can.

The fee is not allocated to complete a total, and the total is not stated.

MLB carries the same shape (32 wagers, 31 pairs) but settles through its own
driver, which makes its own determination.

## 14. No model provenance was fabricated

No wager in any destination carries a recommendation id, evaluation id, model
fair probability, `model_supported`, projection id, rating, edge or expected
value. Verified on the merged branches:

* MLB — all five provenance keys exist in the 84-key schema and all five are
  `null` on all 32 rows.
* NFL and CFB — the fields are **absent from the dataclass**, so the schema
  cannot grow one, and `validate()` refuses a row that carries one. A router row
  containing `model_supported=False` fails the import rather than landing
  quietly: `False` still asserts the model had an opinion.

Every row is `entryMethod / entry_method: IMPORTED_RECEIPT`. A Kalshi execution
proves the owner placed the bet and proves nothing about what recommended it.
Settling a bet establishes what the exchange paid and establishes nothing about
what recommended it either — the settlement records refuse the same fields.

## 15. CFB accounting did not promote the CFB model

```
CFB MODEL STATUS = RESEARCH ONLY / DISABLED
CFB ACCOUNTING   = ENABLED
```

Unchanged. `cfb_edge_finder.accounting` imports no predictive package and no
predictive package imports it; `tests/test_accounting_isolation.py` asserts both
directions, and a token-matched scan asserts the package exposes no sizing,
ranking, recommendation or pricing surface. The package is on both
sizing-disconnection guard lists.

A won-lost record from that ledger is indistinguishable at a glance from a
backtest's, so the report carries `NOT_MODEL_EVIDENCE` as a **field** and prints
it before any number. A reader who sees only the output still sees it.

## 16. The wager ledgers were immutable during settlement

Settlement is a **separate append-only record**, never an edit:

```
NFL  data/wager_settlements/<season>/week_NN/<id>.json   (new record kind)
CFB  settlements/<season>.jsonl
```

Verified by diffing the merged settlement commits against the ledger state
before them:

```
CFB  984761523833df1a..HEAD   ->  settlements/2026.jsonl
NFL  51e67c48ee35b229..HEAD   ->  24 files, all under data/wager_settlements/
                                  imported_wagers touched: 0
```

There is **no PENDING record**. A market settles once, so a settlement is
written once and a wager with no settlement row is simply unsettled — which is
what the absence already means to every reader. A PENDING row would have to be
superseded later, in a store whose entire guarantee is that rows are not.

`wager_settlements` was added to NFL's `IMMUTABLE_KINDS` in the same change that
registered it: a settlement that can be edited after the fact is not evidence
either.

## 17. Second-run imports are no-ops

The full backfill re-run — same `--since`, same season, `dry_run=false`,
acknowledgement typed, against the **merged** ledgers:

```
orders in the window: 76
reconciled: 74
  DUPLICATE_NOOP (this backfill already wrote it): 74
  MISSING_IMPORTABLE (will be written):             0

payloads written: none -- nothing was missing and importable
no payload: the gap window holds nothing this backfill may write.
```

All 74 flipped `MISSING_IMPORTABLE → DUPLICATE_NOOP`. No payload was built, so
no branch was pushed and no pull request opened. The delivery step took zero
seconds.

## 18. Production workflow status

| Workflow | Trigger | Credentials | Writes downstream |
|---|---|---|---|
| `ci.yml` | pull request | none | no |
| `phase0-readonly-audit.yml` | dispatch, `main` only | Kalshi | no |
| `historical-shadow-compare.yml` | dispatch, `main` only | Kalshi | no |
| `series-probe.yml` | dispatch, `main` only | Kalshi | no |
| `backfill-inspect.yml` | dispatch, `main` only | Kalshi | **no — holds no write credential** |
| `downstream-credential-probe.yml` | dispatch, `main` only | Kalshi + downstream | no — reads repository metadata |
| `deliver-wagers.yml` | **every 15 minutes**, and dispatch | Kalshi + downstream | yes |
| `recover-wagers.yml` | dispatch only | Kalshi + downstream | yes |
| `backfill-deliver.yml` | dispatch only, **never scheduled** | Kalshi + downstream | yes |
| `backfill-settle.yml` | dispatch only, **never scheduled** | Kalshi + downstream | yes |

`deliver-wagers.yml` remains the live production path on its 15-minute cadence.

The two `backfill-*` writing workflows are the one-time catch-up and are
**complete**. They are dispatch-only and default to a dry run; pushing requires
the acknowledgement phrase typed out, because writing the owner's betting
history into three public repositories is not something they can undo. A
one-time catch-up on a timer would stop being one-time.

Adding a workflow that names `DOWNSTREAM_REPO_TOKEN` fails an allowlist test
until it is added deliberately. That test went red twice during this mission,
which is exactly its job.

**Kalshi remains READ-ONLY.** No code here creates, modifies or cancels an
order, and `test_no_trading_route_is_reachable` fails if such a route becomes
reachable.

## 19. Known limitations

1. **Two orders remain unclassified** (§11) and will stay refused unless new
   authoritative Kalshi metadata appears. Recovering them by lowering the
   classification standard is not an option.
2. **Four MLB wagers remain pending** (§12) and cannot settle until player-prop
   settlement is implemented in edge-finder-api (issue #43). Reading the archive
   directly to settle them would defeat a deliberate refusal.
3. **Two NFL net P&Ls remain unestablished** (§13). This is permanent for those
   two wagers: the exchange never stated a per-order share of a position fee,
   and no later evidence will change that. Their gross returns are stated.
4. **No cross-sport realized total exists** (§10), and none should be
   manufactured while 1–3 hold.
5. **The settlement pass takes about 13½ minutes**, measured at 13m29s and
   13m22s over two runs. It independently reconstructs the window's wagers from
   Kalshi rather than reading the destination ledgers. **This is deliberate and
   is not to be optimised away:** Kalshi is the authoritative execution source,
   and independent reconstruction is what can expose a wager that was missed or
   malformed downstream. Making the destination ledgers the sole source would
   let an earlier import omission disappear from settlement visibility. If
   runtime later becomes operationally important, a ledger-indexed fast path may
   be *evaluated* in a separate research task — but Kalshi must remain the
   authoritative evidence source and equivalence must be proven before
   activation.
6. **`contracts` can be fractional.** The Q1-2026 fixed-point migration states
   that fractional contracts are representable, and the gap window contains
   them (CFB totals 946.84 contracts). They are recorded as the exchange
   reported them. Rounding would be fabrication.
7. **GitHub's scheduler runs late on this account** — measured at 126–326
   minutes. The 15-minute cadence is correct as configured; what a wager waits
   for is the next run GitHub decides to start.

## 20. Mission closed

The production router and the one-time historical catch-up are **complete**.

* 74 of 76 in-window orders are recorded canonically across three ledgers.
* 70 of 74 are settled; 66 of 74 carry an established realized P&L.
* Every refusal that remains is a refusal this system chose on purpose, and
  each one is written down above with the evidence that produced it.

Nothing in this document should be revised except to record new authoritative
evidence. Settled records are not to be rewritten, missing settlement evidence
is not to be manufactured, and a shared position fee is not to be allocated
merely to complete a total.
