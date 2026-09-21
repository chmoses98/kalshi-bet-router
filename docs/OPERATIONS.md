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
| `settle-wagers.yml` | **every 4 hours**, and dispatch | Kalshi + downstream | yes |

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
(`kalshi-router/<SPORT>`) and one pull request on it, always exactly **one
commit on top of the destination's `main`** — so the tip's parent *is* the base
it proposes against, which is what a `--depth 2` fetch can establish on a
shallow clone and what the next run reads to decide whether the proposal is
still current.

Every run re-runs the destination's importer, and **where** it runs is chosen,
not assumed *(revised 2026-09-19 — see "The branch head is now stable" below)*:

* the branch already proposes against the destination's **current** `main` →
  the importer runs **on top of the branch**, so rows it already proposed come
  back `DUPLICATE_NOOP` and keep their original canonical bytes;
* otherwise (no branch, or `main` has moved) → the importer runs on top of
  `main`, and the batch is reconciled against what is there now.

Either way the branch holds exactly *main plus everything not yet recorded*.

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

## …and a pull request is not the ledger either *(added 2026-09-19)*

`#218` carried 8 MLB wagers from 2026-09-18. It was open, clean, mergeable and
CI-green for three days, and the destination's own 2026-09-18 EdgeLab report
read `Placed bets: 0` — because the rows were on a branch and not on `main`.
Delivery ended at "propose it", and nothing finished the job.

**Two** defects produced that, and the second is the one that made the first
unfixable:

1. Nothing merged it. There was no machine-verifiable decision about whether
   a proposal was safe to land without a human reading it.
2. Nothing *could* have merged it. The import is deterministic, but a commit
   carries a timestamp — so identical content got a new SHA every run, and
   the force-push restarted the destination's ~19-minute pull-request CI on a
   15-minute cadence. The head was therefore almost never a commit whose
   checks had finished.

### The branch head is now stable while the content is

If the remote branch already carries **this exact tree on this exact base**,
the run adopts that commit and pushes nothing. Both halves matter: the same
tree on a *different* base is a different proposal, because its diff against
`main` is something this run never inspected. A genuinely new wager still
produces a new commit, and a moved destination `main` still forces a rebuild.

So the sequence is: run *N* pushes and the gate **waits** (CI is running);
run *N+1* adopts the same commit, finds CI green, and merges. One cycle.

### The gate

`src/kalshi_router/automerge.py` is a **pure function over facts** —
no I/O, no credential, cannot merge anything.
`scripts/merge_delivery_pr.py` gathers the facts and acts on the verdict.
Twelve conditions, all machine-verifiable, all evaluated (it does not
short-circuit, so one run names everything that is not yet right):

| Condition | Refuses when |
|---|---|
| `PULL_REQUEST_IS_OPEN_AND_NOT_A_DRAFT` | someone marked it a draft — a human signal automation must not overrule |
| `BRANCH_ORIGINATED_FROM_THE_ROUTER_WORKFLOW` | head is not `kalshi-router/<SPORT>`, or is a fork, or the base is not `main` |
| `HEAD_IS_THE_COMMIT_THIS_RUN_VERIFIED` | the branch moved since this run inspected it *(waits)* |
| `IMPORTER_REFUSED_NOTHING` | any row came back unsuccessful |
| `EVERY_ROW_NEEDED_NO_HUMAN_JUDGEMENT` | a verdict outside `NEW`/`DUPLICATE_NOOP`/`CORRECTED`, a conflicting field, or a row with no canonical `betId` |
| `THE_IMPORT_IS_IDEMPOTENT` | re-applying the identical payload writes again, or a `betId` moves |
| `ONLY_CANONICAL_WAGER_FILES_CHANGED` | any path outside `data/edgelab/bets/bets.jsonl` |
| `THE_LEDGER_DIFF_IS_APPEND_ONLY` | the diff removes or rewrites an existing canonical row |
| `EVERY_ADDED_ROW_CARRIES_THE_ROUTER_IDENTITY` | an added row is from another batch, unidentifiable, or has no receipt from this run |
| `CONTINUOUS_INTEGRATION_IS_GREEN` | a check failed *(and waits while any is running or none has reported)* |
| `THE_DESTINATION_BRANCH_IS_CLEANLY_MERGEABLE` | `blocked` — a review or protection rule; or `unstable` while every check the gate can see is green, which means something it *cannot* see is withholding the merge *(waits on `unknown`/`dirty`/`behind`, and on `unstable` while checks are still running)* |

Three verdicts, and the difference between the last two is the whole point:

* **MERGE** — squash-merged, pinned to the exact SHA every condition was
  checked against. If anything moved the branch in between, GitHub answers
  `409` and merges nothing, which is the correct outcome rather than a race
  to win.
* **WAIT** — not yet decidable, and it will decide itself. Silent, green,
  costs one cycle. Nobody is paged.
* **REFUSE** — a condition failed. Nothing merges, the rows stay delivered on
  the branch, and the job is red with the condition named.

**Idempotency is now proved every run, not assumed once.** The measurement in
*"Why re-running is safe"* above came from a sandbox that was deleted. The
gate will not land a batch on evidence nobody can re-run, so the payload is
applied a **second** time to the tree the first import produced: every row
must return `DUPLICATE_NOOP` with the same canonical `betId`, and
`git write-tree` must not move.

**A refusal still never costs the rows that succeeded.** The 2026-09-16 rule
is untouched and they are two separate decisions: whatever the importer wrote
is still committed, pushed and proposed; the batch simply does not auto-merge,
because a refusal is exactly the case that needs a person.

> Both properties are fixes. The first version derived the branch name from the
> destination's `HEAD`, which moves — so the same undelivered wager produced a
> new branch on every destination commit, and (worse) a *non*-moving
> destination meant the same branch name with a different commit, rejected
> non-fast-forward, red every 15 minutes. It also opened no pull request at
> all, so delivery terminated in a dead end. The test that was supposed to
> catch this only forbade `$RANDOM`, `date` and `GITHUB_RUN_ID`; none appeared,
> so it passed while testing the wrong property.

## The 2026-09-19 churn: two runs, one payload, two trees

The gate above shipped, and `#218` still did not merge. Two `Deliver wagers
downstream` runs 27 minutes apart, on the same router commit and the same
destination base `067c321e`, produced:

| run | destination commit | tree |
|---|---|---|
| 35465098715 | `7e7108bc` | `a9591950` |
| 35466474703 | `673991474` | `de4b2511` |

Same 41-row payload, same 41 canonical bet ids, 17 `DUPLICATE_NOOP` + 24 `NEW`
and 0 failed rows both times — and two different trees, differing in exactly
three fields on exactly the 24 new rows:

```
createdAt              19:50:03Z -> 20:17:33Z
recordedAt             19:50:03Z -> 20:17:33Z
provenance.ingestedAt  19:50:03Z -> 20:17:33Z
```

### Defect 1 — `unstable` was read as a refusal

One evaluation said, in the same breath:

```
WAIT   CONTINUOUS_INTEGRATION_IS_GREEN            (test still running)
REFUSE THE_DESTINATION_BRANCH_IS_CLEANLY_MERGEABLE (mergeable=true, state='unstable')
```

Two opposite readings of one fact. `unstable` is GitHub restating the **check
rollup** — "mergeable, but the checks are not all green" — and a check that is
still *running* is not a check that failed. So the gate no longer decides it:
it delegates to the check runs, which are what it is derived from. Pending →
**WAIT**. Failing → **REFUSE**. Green everywhere the gate can see, and still
`unstable` → **REFUSE**, because something it cannot see is withholding the
merge and that is not a thing to guess at.

`unstable` is deliberately *not* in `TRANSIENT_MERGE_STATES`. The one-line
version of this fix would have put it there, which waits unconditionally —
including on a genuinely red destination, forever and silently.

### Defect 2 — "the import is deterministic" was not true

The section above used to say the branch's content is *a function of the
account, not of when the job ran*. It was not. Seeded from bare `main`, an
undelivered wager is absent, so the importer writes it as `NEW`, and
`lib.edgelab.bets.build_manual_bet_record` stamps every new row's `createdAt`,
`recordedAt` and `provenance.ingestedAt` with `ids.utc_now_iso()`.

A different tree is a new commit; a new commit is a force-push; a force-push
restarts a ~19-minute check suite on a 15-minute cadence. The branch-reuse
check added the day before was correct and **could never fire**.

The fix is **where the importer runs, not what it writes**. Those three fields
are not noise to strip and not values to fake — the row genuinely *was* first
ingested at 19:50:03Z. The defect was asking the question again from scratch
when the answer was already on the router's own open branch. So when the branch
proposes against the current `main`, the importer runs on top of it and its own
duplicate detection returns the already-stored row untouched. The router still
never edits a ledger file; the destination's importer remains the only thing
that writes its ledger.

The destination already held this principle for its other entry point:
`_content_fingerprint` excludes exactly these fields so that "a rerun against
an unchanged legacy ledger is a true no-op, not a timestamp-only diff on every
row every day". The canonical import path simply never had a caller that let
it apply.

### Why the test suite was green

`test_identical_content_does_not_produce_a_new_commit_every_run` passed because
the harness's stand-in importer wrote rows with **no timestamps at all**. A
deterministic fake produced an identical tree on the second run; the real
importer never could. The fake now stamps the clock like the real one, and
`FAKE_IMPORT_CLOCK` lets a test advance it between runs — with that alone, and
without the repair, six tests in `tests/test_delivery_determinism.py` reproduce
the production churn.

> The rule this keeps re-teaching: a regression test is only worth the fidelity
> of the thing it substitutes for. Both of the last two delivery incidents were
> shipped green by a fixture that was easier to satisfy than production.

`scripts/deliver_branch.py` owns the branch lifecycle and is the only thing on
the delivery path that pushes; every rule it applies lives in the pure
`src/kalshi_router/delivery_branch.py`. Nothing in either is sport-specific —
the destination map is `destination.DESTINATION_REPOS`, which the delivery
workflow now resolves through rather than naming a repository inline, so a
sport added there inherits the repaired lifecycle instead of a copy of it.

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
| **CFB** | **ARMED** *(2026-09-21)* | See below. |
| **TENNIS** | **DISABLED** | `Opportunity.__post_init__` raises *"authority is fixed: this object cannot express a real wager"*. Its schema is research-authority only. |

NFL and Tennis mechanically refuse to hold a wager. That is their decision,
enforced by their own tests, and this system routes to neither.
`WagerRefusal.NO_DESTINATION_IMPORTER` is the refusal, and it is reported as
`NOT_ROUTABLE` rather than as a fault.

### CFB was DISABLED for a reason that no longer holds

The old entry read: *"`tests/test_no_recommendation_surface.py` mechanically
forbids any symbol containing `stake` across the CFB packages. A wager ledger
cannot exist there without deleting that test."*

That was a correct reading of the wrong thing. The CFB repository's rule is
that its **research and prediction** packages may not acquire a staking
surface — the retired model must not come back as a bet sizer. It says nothing
about an accounting ledger recording what the owner actually did on Kalshi.

CFB resolved it by separating the two: `src/cfb_edge_finder/accounting/` is a
package whose own isolation test proves it can neither import from nor be
imported by the research packages, and its ledger lives on an orphan branch,
`accounting-data`, that carries no source at all. The `stake` prohibition still
stands over the research packages, untouched and still enforced.

So the router now has two production destinations. What differs between them,
and the base-branch CI finding that would otherwise have stalled every CFB
delivery, is [DESTINATIONS.md](DESTINATIONS.md).

**Verified, not assumed:** `accounting-data` carries 18 wager rows and 18
settlement rows delivered through this router (PRs #44 and #46), and the
destination's own validator reports 0 problems against them.

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
