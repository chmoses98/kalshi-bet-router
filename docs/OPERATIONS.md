# Operations — what runs, what it has measured, and what it cannot do

Written 2026-09-15, at the point where the router became a scheduled production
system. Every number here is from a live run against the real account, and each
one names the run it came from. Nothing in this document is an estimate.

## The absolute boundary

**This system may record bets. It may not place bets.**

The Kalshi client is read-only by construction: every request path is checked
against `READ_ONLY_PATH_PREFIXES`, `/portfolio/orders` is deliberately excluded,
and `test_no_trading_route_is_reachable` fails if any route that could create,
modify or cancel an order becomes reachable. No code in this repository calls an
order endpoint, and none may be added.

## What runs

| Workflow | Trigger | Credentials | Writes downstream |
|---|---|---|---|
| `ci.yml` | pull request | none | no |
| `phase0-readonly-audit.yml` | dispatch, `main` only | Kalshi | no |
| `historical-shadow-compare.yml` | dispatch, `main` only | Kalshi | no |
| `downstream-credential-probe.yml` | dispatch, `main` only | Kalshi + downstream | no — it READS repository metadata |
| `deliver-wagers.yml` | **every 15 minutes**, and dispatch | Kalshi + downstream | yes |
| `recover-wagers.yml` | dispatch only | Kalshi + downstream | yes |
| `backfill-inspect.yml` | dispatch, `main` only | Kalshi | no — holds no write credential |
| `backfill-deliver.yml` | dispatch only, never scheduled | Kalshi + downstream | yes |
| `backfill-settle.yml` | dispatch only, never scheduled | Kalshi + downstream | yes |

The three `backfill-*` workflows are the one-time historical catch-up and it is
CLOSED — see `docs/CLOSEOUT.md`. They are never scheduled, because a one-time
catch-up on a timer stops being one-time, and the two that write require an
acknowledgement phrase typed out before they push.

Every Kalshi-credentialed workflow refuses to run from a ref other than
`refs/heads/main`, requests no sensitive output, and uploads no artifact. Those
three properties are asserted by tests that **enumerate the workflow directory**
rather than naming files, so a workflow added later is covered the day it is
added. Adding one that names the downstream secret fails an allowlist test —
deliberately, so acquiring write access is never accidental.

## Why the cadence is 15 minutes

Not a preference. Run 2 of `deliver-wagers.yml` walked the account's complete
order history — 1756 orders — and built its payload in **5 minutes 19 seconds**.

A 5-minute cadence would therefore start each run before the previous one
finished, and `cancel-in-progress: false` is not negotiable (cancelling between
the destination's commit and this job's receipt would lose the record that a
wager was written), so the runs would queue without bound.

15 minutes gives about 3x headroom. A test asserts the *relationship* —
cadence ≥ 2× the measured build — rather than the number, so changing the
cadence means confronting the measurement.

**The cron is not the latency.** GitHub's scheduler on this account runs hours
late — measured at 126–326 minutes over eight consecutive runs on a sibling
repository, see *"The schedule has not been observed to fire yet"* below. The
cadence is correct as configured; what a wager actually waits for is the next
run GitHub decides to start.

## Why re-running is safe

Measured against the destination's real importer in a throwaway sandbox, not
assumed:

| Action | Ledger | Verdict |
|---|---|---|
| first import | 457 → 458 | `NEW`, canonical `betId` returned |
| byte-identical re-run | 458 → 458 | `DUPLICATE_NOOP`, **same** `betId` |
| same key, stake 5.37 → 9.99 | 458 → 458 | `CONFLICT`, nothing written, exit 1 |

The source key is derived from `(subaccount, ticker, order id)` alone — no time,
no run id, no batch position — so a wager already delivered lands on the same
`betId`. `CONFLICT` is what makes stabilization acceptable: if an order somehow
receives another fill after being imported, the re-import is refused **loudly**
rather than silently understating the wager.

The sandbox was deleted. No production ledger was touched to establish this.

## What the account actually looks like

From the live runs of 2026-09-15:

```
orders the account has ever submitted        1756
  single-fill                                1646
  multi-fill                                  109
  first-to-last fill span, maximum         0 sec   (window: 900)

after the production cutover (2026-09-15)       1
  finality: final, stabilized                   1
  ELIGIBLE for delivery                         0
  refused: sport unresolved                     1
```

The stabilization window is 900 seconds against a measured maximum span of
**zero**. That is the evidence the window rests on.

## What this system currently CANNOT do, and why

### It cannot validate history older than ~68 days

```
settlement coverage: evidence spans (days)   68
/historical/settlements probe status        404
probe for rows older than the floor            0 returned

replay vs exchange, by market:
  in both (reconciled current)                 8
  absent, replay also flat (closed by fills)  748
  absent, EXPLAINED by a settlement             8
  absent, outside settlement evidence         943
  absent, UNEXPLAINED                           0
```

Kalshi exposes settlement evidence for roughly 68 days. The archival route
404s, and a probe for anything older returned zero rows. An episode whose
closure no settlement can explain does not earn an importable identity — because
**a complete walk of fills proves the fill history; it does not, by itself,
prove the position story.**

So 951 of ~1700 episodes are refused for want of an importable identity, and
that is an external limit on validating the past, not a defect. It does **not**
affect production: a post-cutover wager settles inside the window, so its
settlement evidence is always current.

### The historical shadow comparison therefore measured nothing

```
HISTORICAL SHADOW COMPARISON (validation only -- nothing was written)
  ledger rows read: 457   compared: 452
  router wagers built: 0
  matched: 0   router only: 0   ledger only: 418
  ambiguous multiplicity (not scored): 16
```

Phase 6 is built and correct and currently has nothing to compare, because no
historical episode survives to the wager stage. It will become meaningful once
post-cutover wagers accumulate. Reporting it as "0 disagreements" would be true
and worthless; it is reported as **zero rows built**, which is the fact.

### One post-cutover order is refused, and the classifier is why

Of the episodes that do reach classification:

```
routable episodes by classification:
  MLB: 0    NFL: 27   CFB: 27   TENNIS: 3   OTHER: 23
  UNRESOLVED: 676
    competition ambiguous in taxonomy: 649
    competition absent: 27
  a fall-through WOULD have decided:
    Kalshi's own series metadata (L4):   0
    this project's series registry (L5): 100
    nothing would have decided it:       576
```

`classify.py` checks the taxonomy ambiguity gate **before** the direct
competition rule, deliberately: a competition claimed by several sports in
Kalshi's own catalogue is one our local rules must not quietly overrule.

**These refusals have not been weakened.** What exists instead is
`CollisionStructure`, which measures what *kind* of ambiguity each collision is
— whether the claimant sports could contain different ones of our four (a real
conflict), all narrow to the same one (nominal), narrow to none of ours (costs
nothing), or include a sport we have never heard of (narrows nothing, so it is
counted apart rather than folded into either answer). It also prices the
ordering: how many collisions the direct rule would decide, split by whether the
claimants could contain that answer or could not.

That measurement is what any future decision about the gate should rest on. It
changes no verdict, and two tests prove it.

## Delivery lands as a pull request, not a branch

A branch is not the ledger. Each sport has **one long-lived branch**
(`kalshi-router/<SPORT>`) and one pull request on it. Every run rebuilds that
branch from the destination's current `main` and re-runs the importer, so it
always holds exactly *main plus everything not yet recorded* — content that is
a function of the account, not of when the job ran.

The router never pushes to the destination's `main`. The force-with-lease goes
only onto its own branch, whose previous tip is this job's own earlier output —
and it carries an **explicit expected value**, because a bare
`--force-with-lease` does not work from a `--depth 1` clone. That clone is
single-branch, so there is no remote-tracking ref for the router's own branch
and git rejects the push as *stale info*. The first run succeeds (nothing to
lease against yet) and every run after it fails, which is a failure shape worth
naming: it would have looked correct exactly once.

Fetching the branch is necessary and not sufficient — with the ref present the
lease still cannot infer an expectation for a local branch with no upstream —
so the expectation is stated outright, with an empty value meaning "must not
exist yet". Measured on a real shallow clone, including a check that the lease
still refuses when a second clone moves the branch in between.

A run that pushes the branch but cannot open the pull request **fails**, and
says so: the wagers exist but are not recorded, and that is not a success.

> Both properties are fixes. The first version derived the branch name from the
> destination's `HEAD`, which moves — so the same undelivered wager produced a
> new branch on every destination commit, and (worse) a *non*-moving
> destination meant the same branch name with a different commit, rejected
> non-fast-forward, red every 15 minutes. It also opened no pull request at
> all, so delivery terminated in a dead end. The test that was supposed to
> catch this only forbade `$RANDOM`, `date` and `GITHUB_RUN_ID`; none appeared,
> so it passed while testing the wrong property.

## A windowed fill walk was considered and is NOT worth doing

Every run walks the account's complete fill history — about 1750 orders, five
minutes. The obvious optimisation is to walk from `cutover - margin` forward
with `min_ts`: a bounded but EXHAUSTIVE walk of a time range, which is a
different thing from a budget-truncated walk and would not violate the rule
that a budget and a completeness claim are mutually exclusive.

It is not worth doing, for two measured reasons rather than one guessed one.

**It would buy no latency.** The point would be to record a wager sooner. But
the delay is not the build — it is GitHub's queue, measured at 126–326 minutes
on this account. Turning a five-minute build into a thirty-second one changes a
number that is already invisible next to a multi-hour wait.

**Nothing is being throttled.** The transport telemetry exists to answer this.
Three full-history runs, seventy minutes apart, took 5m19s, 5m38s and 5m01s —
the most recent was the fastest, so there is no upward trend — and an exhausted
retry raises, which would fail the job, so all three being green means zero
exhaustions.

The cost it would carry is real: the window boundary has to be provably wider
than any order's fill span, and getting it wrong drops orders SILENTLY, which
is the one failure this system must not have. Paying that for an optimisation
with no measurable benefit is a bad trade.

Revisit it if the queue delay disappears or if the telemetry starts showing
rate-limit retries. Not before.

## Health states

A scheduled run annotates itself with one of five:

| State | Meaning | Action |
|---|---|---|
| `HEALTHY_NO_OP` | nothing post-cutover | none |
| `DELIVERED` | a wager reached a destination | none |
| `DEFERRED` | held by a gate that opens on its own | wait — the next run is the fix |
| `NOT_ROUTABLE` | activity this system is designed never to route | none |
| `BLOCKED` | a wager was placed that this system cannot record, and waiting will not change that | **a human** |

Ordered by attention required, not by outcome: a run that delivered one wager
and cannot record another is `BLOCKED`, because the delivery needs nothing from
anyone and the refusal does.

**A refusal is not a failure.** `BLOCKED` is a warning annotation, never a red
job: failing a scheduled job every 15 minutes for something only the owner can
fix would train them to ignore it. A run that reports *no* health state **is** a
failure, because that is a defect rather than a refusal.

## Where each sport stands

| Sport | Status | Why |
|---|---|---|
| **MLB** | **ARMED** (see below) | The only destination with an importer. Every step of the delivery path is proven; the schedule itself has not yet been observed to fire. Nothing has been delivered because nothing has been eligible. |
| **NFL** | **DISABLED** | `nfl_edge.handicap.schema.Execution` cannot stand alone — it requires a parent `recommendation_id`. A Kalshi execution proves a bet was placed, not that anything recommended it, so synthesizing a parent would be exactly the fabricated provenance this system must not produce. |
| **CFB** | **DISABLED** | `tests/test_no_recommendation_surface.py` mechanically forbids any symbol containing `stake` across the CFB packages. A wager ledger cannot exist there without deleting that test — and recording a CFB wager would not promote the CFB model in any case. |
| **TENNIS** | **DISABLED** | `Opportunity.__post_init__` raises *"authority is fixed: this object cannot express a real wager"*. Its schema is research-authority only. |

Three of the four destinations mechanically refuse to hold a wager. That is
their decision, enforced by their own tests, and this system routes to none of
them. `WagerRefusal.NO_DESTINATION_IMPORTER` is the refusal, and it is reported
as `NOT_ROUTABLE` rather than as a fault.

## The schedule has not been observed to fire yet, and that is normal here

Stated plainly because the alternative is a document that says "production" on
the strength of a `cron:` line nobody has seen run.

The cron went live on `main` at about 01:19Z on 2026-09-15. At 02:26Z, 67
minutes later, no run with `event=schedule` existed.

**That is not late on this account.** Measured directly on a sibling repository
with a long-running cron (`chmoses98/cfb-edge-finder`, Research Settlement,
`0 */6 * * *`), the delay between a cron slot and the run actually starting,
over eight consecutive scheduled runs:

```
126, 154, 169, 206, 219, 294, 323, 326 minutes      (2.1h - 5.4h)
```

Every one of them eventually fired. So GitHub's scheduler works on this
account; it is simply slow, by hours, and 67 minutes is below the minimum lag
ever observed here.

The structural causes were checked anyway, and all are ruled out:

| Cause | Checked |
|---|---|
| workflow disabled | `state: active` |
| cron not on the default branch | `default_branch: main`, and the cron is on `main` |
| repository is a fork (schedules off by default) | `fork: false` |
| archived or disabled repository | both `false` |
| inactive repository (60-day suspension) | `pushed_at` is minutes old |

### What this means for the cadence

The 15-minute cron is sound engineering against a 5-minute build, and **GitHub
will not honour it**. On the evidence above the real latency between a wager
becoming eligible and a run picking it up is hours, not minutes, and it is set
by the platform's queue rather than by this repository.

That is not a reason to change the cron: a shorter one would not be honoured
either, a longer one would only add to a delay that is already dominated by the
queue, and GitHub collapses missed occurrences rather than backfilling them, so
nothing stacks. It IS a reason not to describe this system as recording a wager
"within 15 minutes". It records it on the next run GitHub starts.

`workflow_dispatch` is the path with predictable latency, and it is the one to
use when a wager needs recording promptly.

### What is unproven is only the timer

Every step the schedule would perform is independently proven: the payload
build, the destination clone, the importer round trip (`NEW` →
`DUPLICATE_NOOP` → `CONFLICT`), the receipts summary, the `data/` guard, the
branch, the lease, the credential helper, and the pull-request permission on
all four destinations.

Until a scheduled run is observed, MLB is **ARMED** rather than **PRODUCTION**.

## Privacy

Every destination repository is public, and the owner has decided that
normalized wager rows may live there. What may **not** leave this repository is
raw account evidence:

* the payload goes to `RUNNER_TEMP`, never the workspace — a payload in the
  workspace is one `git add -A` away from being committed here;
* `write_payloads` returns counts, never rows;
* no workflow uploads an artifact, and tests enumerate the directory to check;
* the destination importer prints its receipts (ticker, stake, entry price) to
  **stdout** and its one-line count to **stderr** — verified by running it with
  stdout discarded — so both delivering workflows send stdout to `/dev/null`;
* no raw fill payload, fill id, subaccount identifier or account metadata is
  persisted anywhere. Order ids are transformed into an opaque,
  domain-separated, length-prefixed digest before they leave the process.

**One deliberate, owner-authorised exception, added after the above:** the
account's **available cash balance** is read (`GET /portfolio/balance`, read
only -- no trading capability is enabled) and delivered to the MLB
handicapper. It is never logged, never committed, never uploaded as an
artifact and never written to a job summary; it travels only as an
**encrypted GitHub Actions secret** sealed against the destination
repository's public key, because both repositories here are public and every
other channel would publish it. Five fields are published and nothing else --
no raw response, no account ids, no `portfolio_value`, no positions. See
[BANKROLL_DELIVERY.md](BANKROLL_DELIVERY.md).

The downstream credential reaches git only through a helper that reads it from
the environment at push time. It is never in a URL, never in argv, never in
`.git/config`. Tests reject `://$TOKEN`, `x-access-token:$TOKEN`, `@github.com`,
`--password` and `extraheader`. GitHub's own log masking is treated as a
backstop, never as the mechanism.
