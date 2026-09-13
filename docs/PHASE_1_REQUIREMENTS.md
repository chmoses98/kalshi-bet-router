# Partial fills, position accounting, and what Phase 1 needs

Phase 0 deliberately stops short of building a wager-aggregation engine. What it
*does* do is inspect enough of the fill schema to answer the questions that engine
will turn on, and implement pure primitives that the engine can build from
(`src/kalshi_router/models.py`).

## Research answers

### 1. Can one logical wager create multiple fills?

**Yes.** A single submitted order executes against whatever resting liquidity is
available and produces one fill per match. A 100-contract order can come back as
five fills of 20. Any layer that treats one fill as one bet will overcount wagers
and undercount size.

Phase 0 measures this directly and reports `partial-order groups (orders with >1
fill)` in every audit.

### 2. How are buy vs sell represented?

The fill's `action` field: `"buy"` or `"sell"`. Phase 0 normalizes it to the
`Action` enum and **rejects** any other token rather than defaulting, because a
silent default would invert a position.

### 3. How are YES vs NO represented?

The fill's `side` field (also seen as `outcome_side`): `"yes"` or `"no"` — which
leg of the binary contract was transacted. This is orthogonal to `action`: all
four combinations are real and distinct.

A crucial modelling point for Phase 1: *buying NO* and *selling YES* express the
same directional view but are **not** the same position, and Kalshi reports them
differently. Collapsing them early will corrupt the ledger.

### 4. How are partial fills represented?

Not as a special field — as multiple fill records sharing one `order_id`, each
with its own `count` and its own execution price. There is no "partial" flag and
no "remaining quantity" on the fill; residual size lives on the order, which Phase
0 does not read (`/portfolio/orders` is outside the read-only allowlist).

### 5. Are order IDs sufficient to group executions from one submitted order?

**Yes, for grouping executions to a submission** — that is exactly what
`order_id` identifies, and `group_by_order()` implements it.

**No, as a wager identity.** Two caveats matter:

* A logical wager may span several orders (an initial entry plus a top-up), and
  `order_id` will not join them.
* Fills carry `trade_id` as well; a trade is the match event, and several members'
  fills share one. It is not a grouping key for *your* order.

So `order_id` is the right key for "these executions came from one submission",
and not the right key for "this is one bet".

### 6. Can a user later reduce or exit an existing position?

**Yes.** A `sell` fill on a side previously bought reduces or closes that exposure,
and a position can be flipped outright. Fills are therefore a **signed stream of
position deltas**, not an append-only list of bets. A Phase 1 layer that only ever
adds wagers will drift out of sync with reality the first time a position is
traded out of, and settlement will be wrong.

### 7. What will a future position-accounting layer need?

Confirmed as available from the fill schema:

* `fill_id` — idempotency key. Phase 0 already de-duplicates on it.
* `order_id` — groups executions from one submission.
* `ticker` — the market, and via metadata the event, series and sport.
* `action` + `side` — the signed direction of the delta.
* `count` — quantity (subject to the `count_fp` question below).
* price fields — entry price per execution, needed for a weighted average.
* `created_time` / `ts` — ordering. Position state is path-dependent, so the
  stream must be replayed in execution order.

Still to be resolved before Phase 1 does quantity arithmetic:

* **`count_fp` scale factor.** Unverified. Phase 0 flags such fills rather than
  converting them; see `docs/API_CONTRACT.md`. Any arithmetic on a guessed scale
  would be silently wrong by orders of magnitude.
* **Settlement data.** Fills describe entry and exit, not outcome. Settlement will
  need market resolution, which is a separate read.
* **Fees.** `fee_cost` appears in the documented fill schema and is not consumed by
  Phase 0. Net P&L requires it.
* **Subaccounts.** If the account uses them, positions must be scoped per
  subaccount.
* **Completeness window.** Phase 0 samples a bounded recent window. Position
  accounting needs a complete, gap-free history with a durable high-water mark,
  which is a different retrieval strategy (`min_ts`/`max_ts`, or the historical
  fills endpoint).

## What Phase 1 must add

1. **Verified API facts.** Run the Phase 0 workflow against the live account and
   close the open questions in `docs/API_CONTRACT.md` — especially the series-tag
   availability that classification coverage depends on, and the `count_fp` scale.
2. **A verified series registry.** Replace the `verified=False` entries with real
   tickers observed from live data, and prune the rest. Audits already report how
   many classifications leaned on an unverified entry.
3. **Complete, resumable retrieval.** A high-water mark and gap detection, so the
   system can prove it has seen every fill rather than a recent sample.
4. **A private data store.** Phase 0 persists nothing on purpose. Phase 1 needs
   somewhere to keep fills and positions that is **not this public repository** —
   a private repo, or encrypted storage. This is a decision to make explicitly,
   not to arrive at by adding a `data/` directory.
5. **Canonical wager construction.** Replay the signed stream per market to derive
   positions and weighted-average entry, then decide what constitutes "a wager" for
   each downstream ledger. Downstream repos likely have differing definitions;
   reconcile before routing.
6. **Routing, with a human gate first.** Only `MLB`, `NFL`, `CFB` and `TENNIS` are
   routable. `OTHER` and `UNRESOLVED` must never be routed, and `UNRESOLVED` should
   raise an alert for manual review — it means classification found a gap, which is
   exactly the signal worth acting on.
7. **Credentials for writing downstream.** Phase 0 deliberately creates no PAT and
   no GitHub App. Whatever Phase 1 uses must be scoped to the specific target
   repositories, and must never be exposed to a public workflow log.
