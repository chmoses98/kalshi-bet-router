# Phase 1A — shadow fill → order → position accounting

**Shadow only.** Nothing in this document is routed, persisted, or emitted to a
downstream repository. This layer answers one question — *given the authoritative
Kalshi fill stream, what positions did the account actually establish, increase,
reduce, close or reverse?* — and stops there.

## 1. Raw fill model

A fill is **immutable execution evidence**, and the layer above it never replaces
it. `NormalizedFill` retains, in memory only:

| Field | Notes |
|---|---|
| `fill_id` | exchange identity |
| `order_id` | the submission this execution belongs to |
| `trade_id` | the match event (shared with the counterparty — *not* a grouping key for your order) |
| `ticker` | market |
| `action` / `side` | `buy`/`sell` × `yes`/`no`, rejected rather than defaulted if unrecognized |
| `count` | `Decimal`, from `count_fp` |
| `price_dollars` | `Decimal`, from `yes_price_dollars`/`no_price_dollars` |
| `fee_dollars` | `Decimal`, exchange-reported; `None` if absent |
| `created_time` / `ts` | execution time |
| `is_taker` | liquidity role |

All quantities and prices are exact `Decimal`. **Binary floating point is never
used for a financial quantity.**

## 2. Execution-order model

One submitted order can execute against several resting orders, producing several
fills. The live audit measured this: **164 orders produced 200 fills, and 25
orders (15%) filled in more than one execution.**

`OrderExecution` summarizes the fills sharing an `order_id`: total quantity,
**quantity-weighted** average price, first/last execution time, fill count,
underlying fill ids, and the fee total.

Arithmetic averaging is wrong and is not used. For `40 @ 0.57, 65 @ 0.58,
71 @ 0.59` the arithmetic mean is exactly `0.58`; the weighted average is
`0.58085227…`. On unevenly filled orders — most of them — the arithmetic mean
misstates cost basis.

Aggregation **fails closed** if one `order_id` spans more than one ticker, action
or side: that would mean `order_id` does not identify a single submission, and
everything built on it would be unsound.

## 3. Position model — the YES/NO question, answered from Kalshi

This was not guessed. Kalshi's own `GET /portfolio/positions` returns, per market
ticker, **a single signed quantity** (`position_fp`) alongside
`market_exposure_dollars`, `realized_pnl_dollars` and `fees_paid_dollars`. There
is no separate YES inventory and NO inventory — there is one number per market.
It matches the order book, where a YES bid at *X* is a NO ask at *$1 − X*.

So YES and NO are modelled as **binary complements on one signed axis**, keyed by
market ticker:

- positive → long YES · negative → long NO · zero → flat

Each fill is projected onto that axis:

| Fill | Signed quantity | YES-equivalent price |
|---|---|---|
| `buy` / `yes` | `+count` | `price` |
| `sell` / `yes` | `−count` | `price` |
| `buy` / `no` | `−count` | `1 − price` |
| `sell` / `no` | `+count` | `1 − price` |

Buying NO at \$0.43 *is* selling YES at \$0.57. The projection is exact, not an
approximation.

## 4. Logical-wager terminology

Deliberately **not** decided here. The layers are kept separate so the downstream
importer can choose explicitly:

| Term | Meaning |
|---|---|
| **Fill** | one execution |
| **Order execution group** | the fills of one submission — the only authoritative "this was one decision" boundary the exchange gives us |
| **Position transition** | the signed effect of one fill (open / increase / reduce / close / reverse) |
| **Position episode** | one flat-to-flat span on one market |

`BUY 100 YES` then later `SELL 40 YES` is **one episode** with a reduction — not
two wagers. `BUY 40` + `BUY 60` is one episode either way, but whether it was one
decision is answerable only from `order_id`: same order → one submission;
different orders → two.

**No time-window grouping is invented.** If order boundaries are the only
authoritative grouping, that is what is preserved.

## 5. Deterministic identity scheme

Identity must be stable so a future import can be retried safely.

| Object | Source key | Basis |
|---|---|---|
| Fill | `kalshi:fill:{fill_id}` | exchange id |
| Order group | `kalshi:order:{order_id}` | exchange id |
| Position episode | `kalshi:episode:{ticker}:{opening_fill_id}` | immutable exchange evidence |

Each also exposes a `sha256` digest for fixed-width use. **No identifier derives
from wall-clock time, iteration order, or a fresh UUID.** Keying an episode on its
opening fill rather than a sequence number means back-filling older history cannot
renumber existing episodes.

Source keys embed tickers and fill ids, so they are **private** and never
rendered.

## 6. Fill ordering

Position accounting is **path dependent**, so ordering is part of correctness.

1. **Execution time** — `created_time` (RFC3339), falling back to `ts` (Unix
   seconds). Offset-naive values are read as UTC, never local time, so ordering
   cannot vary by machine.
2. **`fill_id` ascending** as tie-breaker.

API response order is *not* used: the endpoint is newest-first and cursor
paginated, so arrival order depends on when the audit ran. A fill with no usable
timestamp **fails closed** rather than being appended arbitrarily.

## 7. Cost-basis policy

**Weighted average** on the open inventory, in YES-equivalent dollars.

- **Increase:** `new_avg = (avg·|before| + price·|Δ|) / |after|`
- **Reduce / close:** basis unchanged; realized P&L recognized on the closed part.

If any fill lacks a price, the episode is marked `cost_basis_complete = False` and
the average becomes `None`. **No basis is ever estimated** — a guessed basis
propagates silently into realized P&L.

## 8. Reduction / close / reverse semantics

| Transition | Condition |
|---|---|
| `OPEN` | position was flat |
| `INCREASE` | same sign, magnitude grows |
| `REDUCE` | same sign, magnitude shrinks, not to zero |
| `CLOSE` | reaches exactly zero — episode ends |
| `REVERSE` | crosses zero — old episode closes, new one opens on the other side |

Realized P&L on a reduction is `(exit − entry) × quantity × sign(position)`, so a
long-NO position realizes with the opposite sign automatically.

**Reversal is legal, not an error.** Selling more YES than held carries the
position through zero into long-NO, which Kalshi's signed representation permits.
The invariant "closed cannot exceed inventory" therefore holds *within an
episode*, which is what the tests assert — not across a reversal.

## 9. Fee treatment

Fees are taken **only** from what the exchange reported (`fee_cost_dollars`,
`fee_dollars`, `fees_paid_dollars`, or legacy integer-cent `fee_cost`).

Kalshi's published schedule is `ceil(0.07 · P · (1−P) · contracts)` for takers and
roughly a quarter of that for makers. **That formula is deliberately not
implemented.** A reconstructed fee is an estimate wearing the costume of a fact,
and the rate varies by market category.

If *any* fill in an order or episode lacks a fee, the total is `None` /
`fee_complete = False` rather than a partial sum that could pass for complete.

On a reversal the execution's fee is charged **once**, to the outgoing episode.

## 10. History requirements — what a complete replay actually needs

Position state is only meaningful if the replay started from a known position.
Kalshi's history is split:

- `GET /portfolio/fills` — recent fills.
- `GET /historical/fills` — fills older than the historical cutoff.
- `GET /historical/cutoff` — the timestamps that say which to use.

**Fills before the cutoff exist only in the historical endpoint**, so a complete
history requires querying both and merging. Phase 1A implements neither: it
performs full replay over supplied history and refuses to claim state otherwise.

Two viable strategies for Phase 1B, to be chosen deliberately:

1. **Full replay** — merge live + historical fills into a complete stream.
2. **Checkpoint + delta** — take `GET /portfolio/positions` as an authoritative
   opening state (it already carries `position_fp`, `realized_pnl_dollars` and
   `fees_paid_dollars`) and replay only fills after it.

Strategy 2 is cheaper and self-correcting, and it supplies the authoritative P&L
that fills alone cannot (see §11).

## 11. What a bounded 200-fill audit can and cannot prove

**Can prove:** that fills parse, that orders aggregate, that multi-fill orders are
real and common, that the fixed-point contract matches, that fee fields are
present or absent, and what happened *inside the window*.

**Cannot prove:**

- **Position state.** The first observed fill on a market may be reducing a
  position opened before the window, making computed inventory wrong by an
  unknown offset that no internal consistency check can reveal. The engine
  therefore marks every episode unprovable and sets
  `claims_complete_position_state = False`. This is fail-closed by design.
- **Complete realized P&L.** *Settlement is not a fill.* A market that resolves
  pays out without producing any fill, so the fill stream can never produce
  complete realized economics. The authoritative figure is
  `realized_pnl_dollars` from `GET /portfolio/positions`. Everything this engine
  computes is an explicitly-labelled **shadow estimate**, unreconciled against
  the exchange.

## 12. Privacy boundary

Unchanged from Phase 0, extended to accounting. Tickers, fill ids, order ids,
episode identifiers, quantities, prices, fees and P&L exist **only in memory**.

`AccountingDiagnostics` has no field capable of holding a string — every field is
an `int` or a `bool`, asserted structurally by a test, exactly as `AuditReport`
is. Nothing is persisted, uploaded as an artifact, or cached.

## 13. Future downstream importer contract

Open questions the importer must answer, which this layer deliberately leaves
open:

1. **What is a wager?** Order execution group, or position episode? An episode
   spanning several orders is one exposure but several decisions.
2. **How are reductions represented downstream?** A modification of the original
   wager, or a linked offsetting record? A reduction is not a new bet.
3. **Is a reversal one event or two?** This engine says two episodes; a ledger may
   disagree.
4. **Where does settlement come from?** Not from fills. The importer needs market
   resolution and `realized_pnl_dollars` separately.
5. **Idempotency key.** The episode `source_id` is stable and safe to use, *but
   only if the history feeding it is complete* — otherwise the opening fill, and
   hence the identity, can change once older history arrives.
6. **Which classifications are routable?** Per the Phase 0.1 verdict: MLB, NFL and
   CFB **only when resolved at L1 event competition**. Tennis is not authorized.
   `OTHER` and `UNRESOLVED` are never routed.
