# Authoritative history — Phase C

How the router decides what it *knows* about an account, and what it refuses to
claim. Phase 1A replayed a bounded recent window and correctly refused to call
the result a position state. This is what it takes to stop refusing.

## The two candidate strategies are not alternatives

**Option 1 — full replay.** Walk every fill (`/portfolio/fills` plus
`/historical/fills` for anything older than `/historical/cutoff`) and rebuild
position state from primitive events.

**Option 2 — checkpoint plus delta.** Take `/portfolio/positions` as a
checkpoint and apply only newer fills.

They look like competing answers to one question. They are answers to two
different questions, and each is unable to do the other's job:

| | full replay | positions checkpoint |
|---|---|---|
| net position per ticker | derived | **stated by the exchange** |
| how the position was built | **derived** | unavailable |
| per-episode cost basis | **derived** | unavailable |
| realized P&L per episode | **derived** | account-lifetime total only |
| fee attribution per wager | **derived** | account total only |
| proof that history is complete | impossible alone | **this is the proof** |

`/portfolio/positions` returns **one signed number per ticker**. A logical wager
is an *episode* — flat to flat — and no sequence of episodes can be recovered
from a net number. Two members holding +10 may have arrived by one purchase or
by nine trades across three weeks; the checkpoint cannot tell them apart, so it
can never produce a canonical wager.

The converse is equally true: a replay cannot prove its own completeness. A
missing page and a genuinely absent trade look identical from inside the replay.

**So the design uses both, for the jobs they can each actually do:**

> **Replay derives. The checkpoint proves. Disagreement emits nothing.**

This is why "compare Option 1 vs Option 2 and pick one" has no correct answer:
picking either alone gives up something the system needs. Option 1 alone can
silently emit wagers built on incomplete history. Option 2 alone cannot emit
wagers at all.

## Settlements are position-changing events, and they are not fills

This is the finding that decides whether reconciliation can ever pass.

A market that expires **settles**. The position goes to zero and cash moves, and
**no fill is generated** — the exchange pays out against the final result. So a
replay built from fills alone will show a long-settled market as still open,
forever.

The live audit shows exactly this signature. Across 200 fills:

```
position transitions: 200
  opened: 155   increased: 45   reduced: 0   closed: 0   reversed: 0

position episodes observed: 155
  still open at end of window: 155
  closed within window: 0
```

Not one close, in a sample dominated by games that have long since finished.
These positions did not stay open; they **settled**, and a fills-only replay
cannot see it.

Two consequences:

1. **Reconciliation against `/portfolio/positions` would fail on almost every
   ticker** if settlements were ignored — and the failure would look like
   "missing history" while the history was complete. A wrong diagnosis is worse
   than no diagnosis, because it points the repair in the wrong direction.
2. **The `REDUCE` / `CLOSE` / `REVERSE` paths of the position state machine are
   still unexercised against live data.** They are covered by tests, but every
   live transition so far has been `OPEN` or `INCREASE`. Until settlements are
   replayed, that remains true, and no sport can be called production-ready on
   this evidence.

`/portfolio/settlements` is therefore a **first-class event type in the replay**,
not a reporting extra.

## Reconciliation invariants

Checked per `(subaccount, ticker)`, since positions never net across subaccounts.
All arithmetic is exact `Decimal`.

| # | Invariant | On violation |
|---|---|---|
| 1 | replayed net position == `position_fp` | history incomplete for that ticker — emit nothing for it |
| 2 | every ticker in positions appears in the replay | missing history |
| 3 | every non-zero replayed ticker appears in positions | contradiction; refuse |
| 4 | replayed fees == `fees_paid_dollars` | fee data incomplete; never reconstruct from the fee schedule |
| 5 | replayed realized P&L == `realized_pnl_dollars` | only meaningful once 1–4 hold **and** settlements are replayed |

Invariant 5 is deliberately last. `realized_pnl_dollars` includes settlement
proceeds, so comparing it against a fills-only replay is guaranteed to
disagree — and that disagreement says nothing about whether the fill history is
complete.

**Scope of a failure is per ticker, not global.** One unreconciled ticker must
not block every other ticker, and must not be silently dropped either: it is
excluded and counted, the same fail-closed rule the fill parser already follows.

## What this phase adds, and what it deliberately does not

Added here: the read-only routes themselves — `/portfolio/positions`,
`/portfolio/settlements`, `/historical/fills`, `/historical/cutoff` — with the
same fail-closed pagination as the fills walk, plus tests pinning that the
allowlist still admits **no** trading route.

Positions and settlements are **not** bounded by `max_fills`. That bound exists
to keep the fill sample small; applying it to positions would drop tickers the
exchange says are held, and invariant 2 would then report missing history when
the only thing missing was a page.

Not added here: the reconciliation itself, and settlement replay. Those change
what the engine claims to know, so they follow separately and are verified
against live data before anything downstream is allowed to depend on them.

## Settlement schema, verified

From the first live reconciliation run (755 settlement rows):

```
event_ticker  exchange_index  fee_cost  market_result  no_count_fp
no_total_cost_dollars  revenue  settled_time  ticker  value
yes_count_fp  yes_total_cost_dollars
```

Two things this settles.

**The result field is `market_result`, not `result`.** The probe's first run
counted 755 of 755 rows as "no result" purely because of that guess. The field
name was a guess; the count was the evidence that caught it.

**A settlement is a complete accounting event, not a notification.** It carries
its own per-leg quantities (`yes_count_fp` / `no_count_fp`), its own per-leg cost
basis (`yes_total_cost_dollars` / `no_total_cost_dollars`), its own `fee_cost`,
its `revenue`, its `value`, and `settled_time` for ordering. So replaying
settlements does not require inventing a closing price — the exchange states the
economics directly, which is exactly the rule this system already follows for
fees.

## Correction: a settled market leaves the positions response

This document previously reasoned that a settled market would appear in
`/portfolio/positions` with a **zero** quantity, and the probe counted a
`replay open / exchange flat` signature on that basis. **That was wrong**, and
the live run disproved it:

```
position rows: 0
settlement rows: 755
markets in the replay: 155
markets only in the replay: 155
replay says open, exchange says flat: 0
```

The account has **no** position rows at all. Settled markets are **absent** from
the response, not flat within it. So the designed detector could never fire.

The correction matters in the safety direction, not just the cosmetic one. Had
the router shipped the rule "a replayed ticker missing from positions means
missing history", it would have condemned an entire, intact account as
unreconcilable — 155 of 155 markets — and refused to emit anything, while the
history was complete and the markets had simply settled.

The real signature is therefore **replayed, absent from positions, and present
in settlements**. An absence that *no* settlement explains is the only one that
is evidence of a gap. The probe now counts those two separately.

## The corrected measurement reconciles completely

Re-run with `market_result` read correctly and the settlement signature fixed:

```
markets in the replay: 155
markets only in the replay: 155
  absent from positions, EXPLAINED by a settlement: 155
  absent from positions, UNEXPLAINED:                 0

settlement results observed: no, yes
settlement economics coverage (of 755 rows):
  value 755   revenue 755   yes_count_fp 755   no_count_fp 755
  yes_total_cost_dollars 755   no_total_cost_dollars 755
  fee_cost 755   settled_time 755
```

**Every replayed market is accounted for, with zero unexplained.** The window
reconciles. Under the pre-correction rule it would have read as 155 of 155
markets missing their history.

All eight economics fields are present on **755 of 755** rows, which confirms a
settlement is a complete accounting event rather than a notification: quantity,
cost basis, fee, payout and timestamp all come from the exchange.

## Settlement payout units: dollars is refuted, cents is under test

The payout reading decides every realized P&L on a settled wager, so it was
measured rather than chosen. A binary contract pays $1 per winning contract, so
"is `revenue` a dollar amount?" has a falsifiable answer: revenue would equal the
winning leg's count exactly.

Live, across 755 settlements:

```
revenue equals the winning leg count (binary par):  0
revenue away from binary par:                     355
revenue is zero:                                  400

value equals one:                                   0
value equals zero:                                396
value strictly between zero and one (SCALAR):       0
value above one:                                  359
cost and counts both present:                     755
```

**Zero rows at dollar par**, so `revenue` is not dollars. And `value` is never
`1` while being greater than `1` on 359 rows — so `value` is not a per-contract
dollar price either.

Both are consistent with **integer cents**: a winning contract pays `100`, and a
payout of `1000` against 10 contracts is $1 each. Kalshi's own naming supports
it — dollar-valued fields carry a `_dollars` suffix (`yes_total_cost_dollars`),
and `revenue` and `value` do **not**. That is suggestive, not proof, so the cents
reading is now a counter of its own rather than an adopted assumption.

**Units are the highest-leverage place to be wrong here.** Reading cents as
dollars misstates every settled payout by 100x, and it would do so silently,
because the resulting numbers are all still well-formed decimals.

### Confirmed: cents, with nothing left over

The next run tested the cents reading directly:

```
revenue equals the winning leg count (binary par):    0
revenue away from binary par:                         0
revenue on a non-binary result (no par applies):      0
revenue equals the winning leg count x100 (CENTS):  355
revenue is zero:                                    400

value equals one hundred (a contract in cents):     359
value NEGATIVE:                                       0
```

`355 + 400 = 755`: **every settlement is accounted for, and nothing is left in
the unexplained bucket.** Every non-zero payout sits at cents par, and all 359
non-trivial `value` readings are exactly `100`. Both fields are integer cents.

### `value` is about the market; `revenue` is about the member

The apparent inconsistency between the two resolves into an ordinary fact, and
the arithmetic closes exactly on it:

```
market settled YES, member unpaid (held NO and lost):  62
market settled NO,  member paid   (held NO and won):   58

YES markets 359 + NO markets 396           = 755  ✓
member wins (359 - 62) + 58 = 297 + 58     = 355  ✓  (= non-zero revenue)
```

So `value` is the **market's** settlement price for a YES contract — `100` or
`0` — while `revenue` is the **member's** payout. They are not two views of one
number, which is exactly why treating them as one made 120 perfectly ordinary
rows look like a data fault.

That mattered: a reconciliation that flagged "revenue and value disagree" would
have raised 120 false alarms on an account with nothing wrong with it, and the
natural response to a false alarm at that volume is to loosen the check.

## Still open

* **`/historical/cutoff` response shape** is unverified against a live response.
* **Scalar settlements are not disproven, just unobserved here.** This account's
  755 settlements are all `yes` or `no`. That makes a binary payout assumption
  *adequate for this account today* and still **wrong in general**: the Tennis
  repo verified 1,836 of ~61k finalized tennis markets settling `scalar`,
  strictly between 0 and 1. A router that assumed binary would be correct on
  every row it has ever seen and wrong the first time tennis is enabled, so the
  scalar path must be handled before tennis routes, not after.
* **Whether 755 settlements cover the whole account.** The settlement walk is
  unbounded, but the fill sample is bounded at 200, so "every replayed market is
  explained" is a statement about the window, not the account.
* **Cost of a full replay.** The bounded 200-fill window already issued 486 API
  requests once metadata resolution ran for 155 markets. A full-history replay
  needs a request budget and a rate-limit strategy before it is run.
