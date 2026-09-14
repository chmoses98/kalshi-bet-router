# The logical wager — Phase E

What one "bet" *is*, when the exchange only ever reports fills, orders and
settlements. Answered against the MLB ledger's existing schema rather than
invented, because that ledger already holds the owner's real manual bets and the
router must produce records compatible with them.

## The answer: a wager is a position EPISODE, not an order

An **episode** is flat → flat on one `(subaccount, ticker)` axis: the position
leaves zero, moves however it moves, and returns to zero (by offsetting trades,
or by settlement).

Three independent lines of evidence say the episode is the right unit.

**1. The MLB ledger already defines a wager that way.** `lib/edgelab/bets.py`
describes a wager as *"ONE user-placed economic position (one stake, one payout,
one result)"*. One stake, one payout, one result is exactly flat-to-flat. A
record carries a single `entryPrice`, a single `stake` and a single result — a
shape an order cannot fill and an episode fits exactly.

**2. An order is not an atomic economic unit.** Live data, 200 fills:

```
orders observed: 164        orders with partial fills (>1 execution): 25
position transitions: 200   opened: 155   increased: 45
```

25 of 164 orders arrived as several fills, so an order is already a *grouping*
of executions, not a primitive. And 45 increases means several orders routinely
build one economic position. Treating each order as a wager would split one
position into several records that share a payout and a result — and would
report 200 wagers where the owner placed at most 155.

**3. Settlement resolves an episode, not an order.** A settlement row is keyed by
ticker and carries `yes_count_fp` / `no_count_fp`, per-leg cost basis, `revenue`
and `fee_cost` — the economics of the whole position, with no reference to which
order created it. There is no order-level payout to report, because the exchange
does not compute one.

So: **fills aggregate into orders, orders aggregate into episodes, and the
episode is the wager.** The order layer is kept because it is real and useful
for execution quality, not because it is the unit of betting.

## Where the router's output goes

`scripts/edgelab/import_bet_batch.py` already has the slot. Each row may carry an
`executionEconomics` dict, expanded by `lib.edgelab.bets._EXECUTION_ECONOMICS_FIELDS`:

```
contractCost  averageFillPrice  entryFees  exitFees  totalFees
actualCashConsumed  unusedAllocatedCash  grossCashReturned
grossSettlementPayout  exitSaleProceeds  realizedROI
executionStatus  feeStatus  feeType  feeMultiplier  feeSource
feeScheduleVersion  feeEffectiveDate  economicsSource  economicsConfidence
```

An episode maps onto it directly, and **every mapped value is exchange-reported
rather than reconstructed**:

| field | from |
|---|---|
| `averageFillPrice` | episode weighted-average entry, on the traded leg |
| `contractCost` | Σ quantity × leg price over opening fills |
| `entryFees` / `exitFees` | Σ `fee_cost` over opening / closing fills |
| `totalFees` | entry + exit + the settlement's own `fee_cost` |
| `exitSaleProceeds` | Σ proceeds over closing fills |
| `grossSettlementPayout` | the settlement's `revenue` |
| `economicsSource` | `EXACT_API_EXECUTION` |

That last row is not a new convention. `lib/edgelab/execution_economics.py`
already defines `STAKE_EVIDENCE_EXACT_API_EXECUTION = "EXACT_API_EXECUTION"`,
described as *"real Kalshi order/fill data identifying total cash debited"*, and
maps it to confidence `HIGH`. The slot for this router was designed before the
router existed.

## The rule the router must not break

The same module defines a **stake-evidence priority ladder**, and the router is
**priority 2, not priority 1**:

```
1. user_confirmed_stake   -- authoritative
2. exact_api_stake        -- real Kalshi order/fill data   <- the router
3. exact_receipt_stake
4. fee-aware whole-dollar reconstruction
5. otherwise AMBIGUOUS -- stake is None, never guessed
```

and it states plainly that a later-supplied screenshot *"never overwrites"* a
user-confirmed stake.

So **exchange truth does not outrank the owner's own confirmed stake.** The
router may supply `executionEconomics` and may fill a stake the owner never
stated, but it must never overwrite one the owner did. That is a downstream
policy this system inherits, not one it gets to redesign — and it is the
concrete form of "do not duplicate or overwrite a canonical wager".

## Identity

MLB identity is `hash(importBatchId, sourceBetKey, marketTicker, side)` when a
row has no `entryTimestamp`. The router therefore needs a **stable
`sourceBetKey`** — stable meaning the same episode yields the same key on every
re-run, so a repeated import is a `DUPLICATE_NOOP` rather than a second wager.

The obvious key — the opening fill's id — is stable only if the opening fill is
stable. It is not, in one specific case: when a **backfill** supplies older
history, a position believed to have opened at fill *B* may turn out to have
opened at earlier fill *A*, and the episode's identity would change underneath an
already-imported wager.

This is why the accounting layer already distinguishes `StableIdentity` from
`ProvisionalIdentity`, and why the audit reports:

```
position episodes observed: 155
  provable from supplied history: 0
  with an importable identity: 0
```

Zero, correctly: a bounded 200-fill window cannot prove any episode began inside
it. **An episode with a provisional identity is never importable.** Promoting it
requires proving the history before it is complete, which is Phase C's job —
which is precisely why Phase E cannot ship a router ahead of Phase C, no matter
how well-specified this contract is.

### Settling an episode does not make it importable

Settlement replay closes episodes, and it would be easy to read a closed episode
as a finished, safe-to-import wager. It is not.

**A settlement proves where an episode ENDED. Identity is keyed on where it
BEGAN.** Back-filling older fills can still merge a settled episode into an
older one and change its opening fill, so under a bounded window a settled
episode is `closed` and *still* `ProvisionalIdentity`. An importer that treated
"settled" as "safe to import" would create a wager whose identity later moves
underneath it — the exact instability this split exists to prevent.

Tests pin all three halves of that: settled-but-provisional under a bounded
window, settled-and-stable under complete history, and that replaying
settlements never upgrades the replay's own completeness claim.

### Two provability flags, and an importer must read both

An episode carries two independent claims, and they answer different questions:

| Flag | Question | Cleared by |
|---|---|---|
| `provable` | Is the OPENING boundary established? | a bounded fill window |
| `outcome_provable` | Is the OUTCOME established? | settlement evidence that does not reach this episode, or a settlement that could not be applied |

`provable` gates **identity** — an unprovable opening means a
`ProvisionalIdentity` and nothing may be imported. `outcome_provable` gates
**resolution**: identity is fine, cost basis is fine, but whether the position
still exists is unknown.

An episode with `outcome_provable = False` is **not an open position**. It is a
market the evidence cannot follow to its end — most often because a settlement
that would have closed it predates the settlements route's reach, sometimes
because a settlement exists but disagreed with the replay on size. Either way:

* it must never be written as live inventory, or as an unsettled wager awaiting
  a result;
* it must never be settled from a reconstructed outcome — the whole point is
  that the outcome is unknown;
* its cost basis and identity remain valid, so it may be recorded as a wager of
  **unknown resolution**, if the downstream schema has a state for that. If it
  does not, the episode is not importable and the missing state is the thing to
  build.

See `docs/HISTORY.md`, "Settlement coverage is a second completeness dimension",
for how the boundary is measured and why it fails closed.

## Open questions this contract does not yet answer

* **A reduction that is not a close.** MLB's record has one stake and one payout.
  A partial exit mid-episode has neither yet. Options are to keep the episode
  open and report realized P&L only at close, or to emit a separate record per
  exit — the second duplicates a wager and is rejected on that basis, but the
  first means an open episode's record is provisional until it closes.
* **A reversal.** Crossing zero closes one episode and opens an opposite one from
  a single fill. That fill's fee belongs to both, and the audit already counts
  `episodes with ambiguous fee allocation (reversal)`. Live count is 0 so far, so
  no allocation rule has had to be chosen yet — and none should be chosen until
  one is observed.
* **Multi-leg.** MLB supports `MULTI_LEG` for one economic position spanning
  several contracts. A router that emits one wager per ticker cannot produce one,
  and must not silently flatten a deliberate multi-leg bet into unrelated singles.
* **A state for unknown resolution.** `outcome_provable = False` describes a
  wager that is neither pending nor settled. MLB's schema has no such state
  today, and inventing one downstream is a ledger decision, not a router one.
  Until it exists, these episodes stay out of any import.
