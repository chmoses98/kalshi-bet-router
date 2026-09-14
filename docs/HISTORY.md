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

## Settlement replay

Implemented, using only exchange-stated economics:

* realized P&L is the settlement's own `revenue` against the episode's cost
  basis — no closing price is invented, the same rule already applied to fees;
* the settlement's `fee_cost` is charged as stated;
* `revenue` and `value` are converted from cents to dollars **explicitly**, in
  one place, because the units are mixed within a single row.

Applied after the fills rather than merged into them: a market settles at expiry
and cannot take a fill afterwards, so for any one ticker every fill already
precedes its settlement. Settlements are ordered among themselves by
`settled_time` so the result stays deterministic.

### Two refusals, both fail-closed

**Size disagreement.** When a settlement states a quantity that does not match
the replayed position, some of the settled contracts were bought outside the
window. Applying it anyway would credit the full payout against a partial cost
basis and **overstate profit silently**, so the settlement is refused and
counted, and the position is left open rather than zeroed.

**Ambiguous subaccount.** The live settlement schema carries **no subaccount
field**. With one subaccount that is harmless; with several, a settlement cannot
be attributed to one, and guessing would merge two independent positions. A
ticker held in more than one subaccount refuses its settlement.

That second limit is a property of the exchange's schema, not of this code, and
it constrains any multi-subaccount future: settlement-aware accounting is only
sound while a ticker is held in at most one subaccount.

### Verified live

Settlement replay, run against the same 200-fill window:

```
position episodes observed: 155
  still open at end of window:   3     (was 155)
  closed within window:        152     (was 0)
  with complete cost basis:    155
  with complete exchange fees: 155
  provable from supplied history: 0
  with an importable identity:    0

settlements applied: 152
  REFUSED, size disagreed with the replay:      3
  REFUSED, ticker held in several subaccounts:  0
```

**152 of 155 episodes close, and the 3 still open are exactly the 3 refused** —
their contracts were partly bought outside the window, so the settlement's size
disagreed with the replay. The two numbers matching is not a coincidence, it is
the fail-closed rule being exactly as conservative as the evidence requires: no
episode was closed on a payout its cost basis did not cover.

**Identity did not move.** `provable` and `importable` are still 0, because a
settlement proves where an episode ended and identity is keyed on where it
began. Closing is not importability.

## Absence is only a gap when the replay still holds something

The first full-history run (archive walked, 500 fills per route) reported:

```
absent from positions, EXPLAINED by a settlement: 464
absent from positions, UNEXPLAINED:               423
```

423 was **over-reporting**, and the cause was this probe again. It flagged every
replayed market missing from the positions response — including markets the
replay itself had already closed to zero by trading. A market both sides agree
is flat is **agreement**, not a gap.

There are three legitimate ways to be absent from positions and only one that
means missing history:

| replay says | settlement exists | verdict |
|---|---|---|
| flat | either | **agreement** — both sides say the member holds nothing |
| open | yes | **explained** — the exchange drops settled markets |
| open | no | **the gap** — history is incomplete for that market |

Flat is checked first, because once the replay is flat there is nothing left for
a settlement to explain. A test asserts the three buckets partition the absent
markets exactly, so a future fourth case cannot go uncounted.

This is the same hazard as the 120 false `revenue`/`value` alarms: a check that
cries wolf at volume gets loosened, and that is how a real fault later slips
through.

## A complete fill history does not earn authority over positions

The first genuinely exhaustive run walked both fill routes to the end:

```
live fills:      883 over  9 pages   exhausted, not truncated
archived fills: 1050 over 11 pages   exhausted, not truncated
fills rejected: 0                    cutoff retrieved: True

HISTORY IS COMPLETE: True
```

That claim is correct — 1,933 fills, 1,746 orders, nothing refused. And it was
immediately followed by an unearned one:

```
markets in the replay: 1698          settlement rows: 755
absent, UNEXPLAINED:    943          exchange position rows: 0
```

The replay asserted authority over a position state holding **943 markets the
exchange does not report at all**.

**The settlement route does not reach as far back as the archive fill route.**
Only 755 of the 1,698 traded markets have a settlement row; the rest settled
beyond whatever window `/portfolio/settlements` retains. A settlement closes a
position *without a fill*, so for those markets no event the replay can see will
ever close them. The ledger is internally consistent and externally wrong.

So completeness of fills is **necessary but not sufficient** for authority over
position state, and `claims_complete_position_state` was derived from fills
alone. It is now suppressed whenever reconciliation finds an unexplained open
market, and the report says why in words rather than leaving a reader to notice
that two numbers cannot both be true.

This is the sharpest form of the rule the whole system is built on: *a walk that
finished is not the same as a story that closes.*

## Settlement coverage is a second completeness dimension

Suppressing the authority claim (above) stated the problem honestly but left
943 markets in one undifferentiated pile called UNEXPLAINED. That pile is not
homogeneous, and treating it as though it were has a cost in both directions:
it buries whatever genuine defects it contains, and it reports a permanent,
structural limit as though it were a fault someone could go fix.

So there are **two** completeness questions, not one:

| Question | Answered by | What it proves |
|---|---|---|
| Where did this position open? | `/portfolio/fills` + `/historical/fills` | the cost basis and the episode's identity |
| Did this position ever close? | `/portfolio/settlements` | the outcome |

The fill routes come in a pair — a live one and an archive one serving
everything older than `/historical/cutoff`. The settlements route, as far as
this account can show, does not. A market bought, held and settled before the
settlements route's reach leaves a **complete fill trail and no settlement
row**: the replay holds it open, and nothing it can ever fetch will close it.

### The floor, and the asymmetry that makes it honest

The **floor** is the earliest `settled_time` in an exhausted settlements walk.
It is *not* a documented retention boundary — it is simply the oldest row the
route produced. That distinction is the whole argument:

* **At or above the floor**, the route demonstrably had data. A market the
  replay still holds open there, with no settlement row, is real evidence of
  absence — a genuine contradiction, and a defect to chase.
* **Below the floor**, the route returned nothing at all. That is not evidence
  the market never settled; it is the absence of evidence either way. Such an
  episode's outcome is **unprovable** — neither open nor closed.

The comparison uses the episode's **last activity**, not its opening. A market
cannot take a fill after it settles, so its settlement time is at or after its
final fill; an episode whose last fill lands above the floor would have had its
settlement inside the route's reach, had one existed.

### Fail-closed rules

The floor is withheld entirely — nothing is reclassified — whenever it cannot
be trusted:

* the settlements walk did not exhaust its cursor;
* **any** settlement row carried an unreadable `settled_time` (the unreadable
  one could be the oldest);
* any settlement row failed normalization (same reason);
* no settlement row came back at all.

Withholding is the safe direction. Over-marking would convert genuine
contradictions into an explained boundary, which is precisely the failure this
work exists to prevent. A caller of `probe_reconciliation` that supplies no
bounded set likewise gets the older, louder answer.

Two cases are explicitly **not** coverage:

* A market with a settlement row that was **refused** (size disagreed, or the
  ticker is held in several subaccounts). The route spoke; the reconciliation
  failed. Filing that under coverage would hide a real defect. Such episodes are
  marked outcome-unprovable without being marked coverage-bounded — because a
  refused settlement is not an open position either: the exchange settled the
  market, and reporting it as live inventory would overstate the portfolio by
  the whole episode.
* A ticker holding one bounded open episode **and** one the route did cover.
  "Every open episode below the floor", not "any" — otherwise the bounded
  sibling would speak for the uncovered one.

### Authority now has three conditions

`position_state_is_authoritative` is one property with one definition:

1. the fill history is complete;
2. the exchange's own view does not contradict it;
3. no episode's outcome is beyond what the available routes can establish.

Conditions 2 and 3 both deny authority, and they mean different things. (2) is a
defect. (3) is a stated limit — *a portfolio containing markets of unknown
outcome is not a portfolio*, however well-understood the reason. The report
prints each in its own words.

### Testing this module's own assumption: is the walk windowed?

The settlements walk ends when its cursor runs out, and it would be easy to read
that as *the route gave everything*. It only means the route gave everything
**for the query that was asked**. If the default query carries an implicit
window, an exhausted walk and a complete one are indistinguishable — and every
conclusion above would be measuring a query rather than the data.

So the audit asks the route for **one settlement strictly older than the walk's
earliest row** (`max_ts = floor - 1`, `limit = 1`). A row that really is older
proves the walk was windowed. The floor is then **withheld and nothing is
reclassified**, because the right response is to re-walk with `min_ts` — not to
state a limit that is really a missing parameter.

A route that ignores an unknown parameter answers with its *newest* rows, so
every returned row's own `settled_time` is checked against the floor rather than
trusted because it arrived. Otherwise the guard would invent a windowing that is
not there and throw away a perfectly good floor.

Both outcomes are tested: a route that hides older settlements until asked, and
a route that ignores `max_ts` entirely.

### An archival settlements route would close this instead of bounding it

If settlements have the same live/archive pair the fills have, the gap does not
need bounding — it disappears. That is worth one request, so the audit probes
`GET /historical/settlements` (one page, never walked) and records the answer
either way. An absent route is a finding, not an error; the probe degrades the
measurement instead of failing the audit. The day that route appears, the audit
will say so.

### What the live run proved

Run 19, on a complete history (883 live + 1,050 archived fills, both walks
exhausted, none rejected):

```
settlement rows walked: 755    all with a readable settled_time
walk exhausted: True           truncated: False
evidence spans (days): 67      evidence floor usable: True

asked for settlements older than the walk's earliest:  status 200, 0 rows
DEFAULT WALK APPEARS WINDOWED: False

archival settlements route: 404, not available
```

Both probes came back decisive, and in opposite directions:

* **The walk is not windowed.** The route accepted `max_ts` and returned
  **nothing** older than the floor. Had it ignored the parameter it would have
  answered with its newest row — one row, not zero. So the exhausted cursor
  really did mean the route had nothing more.
* **There is no archival settlements route.** `GET /historical/settlements` is
  a 404. Fills have an archive; settlements do not.

Together those turn an assumption into a measurement: **the settlements route is
retention-limited, and the limit is roughly 67 days of evidence.** The 943
markets below the floor demonstrably *did* settle — the exchange reports zero
open positions anywhere — the route serves nothing before the floor, and no
archive exists. Their outcomes are not merely unretrieved; they are
unretrievable.

The episode accounting partitions exactly:

```
position episodes observed: 1698
  still open in the replay: 951
    open, inside settlement evidence:        0
    OUTCOME UNPROVABLE:                    951
      of which bounded by settlement coverage: 943
  closed within window: 747

absent from positions, replay also flat (agreement):  747
absent from positions, EXPLAINED by a settlement:       8
absent from positions, outside settlement evidence:   943
absent from positions, UNEXPLAINED:                     0
```

**Zero genuine contradictions.** Every market C.14 flagged is accounted for by
the coverage boundary or by a refused settlement. And `open, inside settlement
evidence: 0` agrees exactly with the exchange's `position rows: 0` — two
independent sources saying the account holds nothing. That agreement is what
C.14 correctly refused to claim on fills alone.

Authority is still withheld, and correctly: 951 episodes have outcomes the
evidence cannot establish. A portfolio of unknown outcomes is not a portfolio.
But the reason is now a measured, bounded limit rather than an open question.

### The last discrepancy: which way did the 8 refusals run?

Eight settlements were refused because their stated size disagreed with the
replay — the only thing left in the whole accounting that coverage does not
explain. A count says how often reconciliation failed. It does not say what
failed, and the diagnoses point in opposite directions:

* a settlement **larger** than the replayed position suggests fills the replay
  never saw, or a gross rather than net count;
* a **smaller** one suggests the opposite;
* an **opposite direction** is not a size disagreement at all.

Guessing between them is exactly the move this codebase keeps having to unlearn,
so the shape is recorded per refusal and left for a live run to decide. The
shapes partition the refusals — a test pins that, so the diagnostic cannot
quietly lose a case and read as though less went wrong.

### Also fixed here

A settled episode used to record `closed_at = opened_at`. An episode opened in
January and settled in June did not close in January, and any holding period
derived from it would have been wrong by months. It now carries the exchange's
own `settled_time`, or `None` when that timestamp will not parse — an unreadable
clock is left unread rather than filled in with a nearby one.

The settlements route was also being walked **twice** per audit — once to build
the replay's settlements and once inside the reconciliation probe. Beyond the
doubled cost, the two views could disagree if a settlement landed between them.
One walk now serves both.

## Authority is earned, in the data model rather than in the report

C.14 found the right defect and fixed it in the wrong place. It suppressed the
authority claim **in the rendered report**, by printing a gated expression:

```python
claims_complete_position_state and not position_state_contradicted_by_exchange
```

while `claims_complete_position_state` itself still derived from fill
completeness alone — and `as_dict()` still exported that raw `True`. One true
value and one false presentation, under a single name. A human reading the
report got the right answer; every machine consumer got the wrong one.

The same defect ran deeper. `provable` was also set from completeness alone, so
a market the exchange contradicted still received a **stable, importable episode
identity**. The gate a downstream importer would actually hit was wide open.

### Three concepts, kept apart

| Concept | Question | Answered by |
|---|---|---|
| `fill_history_complete` | Did both fill routes exhaust with nothing rejected? | the fill walks |
| position authority | Does the replayed position reconcile with exchange truth? | positions + settlements |
| episode importability | Is *this* episode proven enough for downstream use? | both gates together |

The first is necessary for the other two and sufficient for neither.

### One value, three surfaces

`claims_complete_position_state` is now the **effective** claim: true only when
the fill history is complete *and* every episode's position story has been
earned. There is no second, ungated value anywhere. The object, `as_dict()` and
the rendered report carry the same boolean, and a test asserts they can never
disagree across reconciled, unexplained, conflicted, never-reconciled and
closed-by-fills cases. The raw walk result survives under its own honest name,
`fill_history_complete`, which grants nothing.

### Per-episode authority

Withholding the global claim must not destroy valid evidence, so authority is
recorded per episode:

| State | Meaning | Earned? |
|---|---|---|
| `CLOSED_BY_FILLS` | returned to flat by trading, every fill observed | yes |
| `EXPLAINED_SETTLED` | an authoritative settlement closed it | yes |
| `RECONCILED_CURRENT` | open, and the exchange reports the same net position | yes |
| `UNEXPLAINED` | open, exchange reports nothing, no settlement explains it | no |
| `CONFLICTED` | exchange truth and the replay disagree | no |
| `NOT_RECONCILED` | open, and no exchange view was supplied | no |

A closed episode is proven by the events that closed it — the exchange's
current-position view has nothing to say about a span that already ended, since
a settled market leaves that response entirely. An open episode is a claim about
what the account holds *now*, and only the exchange can confirm that.

`NOT_RECONCILED` is deliberately distinct from `UNEXPLAINED`: absence of a check
is not the result of a check. Nothing was asked, so nothing was earned.

### Importability is structural, not advisory

`PositionEpisode.identity` now requires **both** gates: a provable opening *and*
earned authority. Either failing yields a `ProvisionalIdentity`, which exposes no
`source_key` or `source_id` attribute at all, so `require_importable_identity()`
refuses it. A caller cannot bypass the gate by forgetting to read a warning
boolean, because there is no key to read.

Crucially, this is per-episode. One unexplained historical market withholds the
account-level claim while leaving every reconciled market its identity — the
evidence that *is* good is not thrown away to describe the evidence that is not.

### Reconciliation moved before the replay

It used to run afterwards and set a flag on the diagnostics. That ordering is
what made the defect possible: a check that runs after the fact can only
complain about an identity that already exists. The exchange's position view is
now an **input** to the replay, and every authority flag is derived from the
episodes themselves rather than set by the orchestrator.

Two failure modes in reading that view fail closed:

* a row whose ticker will not parse is dropped — a quantity with no market says
  nothing about any market;
* a row whose **quantity** will not parse is kept as `None`, not dropped.
  Dropping it would make the market look *absent*, and absent is exactly how the
  exchange reports a market it has settled — so a parse failure would read as a
  contradiction nobody observed. `None` becomes `CONFLICTED`.

One ticker held in two subaccounts is also `CONFLICTED`: the positions response
carries no subaccount field, and attributing one reported quantity to one of two
independent positions would merge them.

### The governing principle

> **A complete walk of fills proves the fill history. It does not, by itself,
> prove the position story.** Position authority must be earned through
> reconciliation with exchange state or an authoritative closure event.

## Still open

* **`/historical/cutoff` response shape** is unverified against a live response.
* **Scalar settlements are not disproven, just unobserved here.** This account's
  755 settlements are all `yes` or `no`. That makes a binary payout assumption
  *adequate for this account today* and still **wrong in general**: the Tennis
  repo verified 1,836 of ~61k finalized tennis markets settling `scalar`,
  strictly between 0 and 1. A router that assumed binary would be correct on
  every row it has ever seen and wrong the first time tennis is enabled, so the
  scalar path must be handled before tennis routes, not after.
* **The exact retention rule.** Run 19 proves the route serves nothing older
  than the floor and that the floor sits ~67 days back for this account. Whether
  that is a fixed retention window, a row cap, or something else is not
  established — only that it exists and that `max_ts` will not reach past it.
* **Whether a market below the floor settled at all.** The evidence says
  collectively yes (the exchange reports no open positions) but cannot say it
  per market. `GET /markets/{ticker}` reports a market status and would resolve
  it one request at a time — affordable for a bounded sample, not for 943
  markets, and not yet attempted.
* **Which way the 8 refusals run.** The shape diagnostic ships; a live run has
  not yet reported it.
* **Classification is the expensive half, not the fill walk.** 155 markets cost
  486 requests, almost all of it metadata resolution at several requests per
  market; the fill pagination itself is a handful. This account has settled 755
  markets, so a full-history audit that also classified every market it touched
  would be several thousand requests. `--max-classify-markets` therefore bounds
  the metadata sweep alone, leaving the ledger to replay every fill.

  A market left unclassified by that bound is **not** unresolved. Counting work
  never attempted as work that failed would understate classification quality,
  and in the direction that looks like a defect in the classifier rather than a
  budget the caller chose — so it would invite the wrong repair.

* **Cost of a full replay.** The bounded 200-fill window already issued 486 API
  requests once metadata resolution ran for 155 markets. A full-history replay
  needs a request budget and a rate-limit strategy before it is run.
