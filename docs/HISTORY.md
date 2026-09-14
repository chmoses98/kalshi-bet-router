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

## Still open

* **`/historical/cutoff` response shape** is unverified against a live response.
* **Settlement schema** — field names, whether the value is a dollar string, and
  whether a scalar settlement is distinguishable — is unverified here. The
  Tennis repo's finding that 1,836 of ~61k finalized tennis markets settled
  `result="scalar"` strictly between 0 and 1 means a binary payout assumption is
  known to be wrong; see `DOWNSTREAM_REPOS.md`.
* **Cost of a full replay.** The bounded 200-fill window already issued 486 API
  requests once metadata resolution ran for 155 markets. A full-history replay
  needs a request budget and a rate-limit strategy before it is run.
