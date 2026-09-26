# Destinations — the profile table, and CFB activation

Until 2026-09-21 this router had exactly one production destination. MLB was
not a *case* in the delivery workflow; MLB was the workflow. Every fact about
where wagers go and how they get imported was a literal in a bash step:

```yaml
# the shape of the old deliver-wagers.yml
git clone .../edge-finder-api
python scripts/import_kalshi_wagers.py --payload "$PAYLOAD"
```

A second sport could not be added to that without either copying the whole job
or teaching it a chain of `if [ "$SPORT" = ... ]`. So the facts moved into
`src/kalshi_router/destinations.py`, and the workflow reads them.

## `DestinationProfile`

One frozen record per routable sport. Every field below is a thing that
actually differs between MLB and CFB — none of them is speculative generality.

| Field | Why it is per-destination |
|---|---|
| `repo` | where the ledger lives |
| `ledger_branch` | **MLB writes to `main`. CFB's ledger is an orphan branch, `accounting-data`.** |
| `code_branch` | the importer script does not live on CFB's ledger branch, so it is cloned separately |
| `wager_importer` | argv template, rendered with `{code} {payload} {work} {season} {receipts}` |
| `settlement_importer` | CFB has one. **MLB does not** — `None` means "this destination settles itself" |
| `committable_prefixes` | what delivery is allowed to commit. Anything else the importer touched is dropped and logged by path |
| `mergeable_paths` | what auto-merge is allowed to see changed. A diff outside this set stops the merge |
| `ledger_branch_runs_ci` | see below |
| `ledger_validator` | the destination's own ledger check, run before the PR is opened |
| `row_identity_field` | `sourceBetKey` for both today, but it is the destination's choice |
| `requires_season` | CFB's ledgers are `wagers/<season>.jsonl`; MLB's is one file |

`profile_for(sport)` raises `UnknownDestinationError` rather than returning a
default. There is no fallback profile, because a fallback would route a sport
somewhere *plausible* instead of refusing it.

`scripts/destination_profile.py` prints one field, so the workflow gets its
facts from the same table the tests assert against rather than from a copy.

## The finding that would have stalled every CFB delivery

**A `pull_request` workflow only runs if the workflow file exists on the PR's
BASE branch.**

CFB's `accounting-data` is an orphan branch. It carries ledger files and
nothing else — no `.github/`, no source. So a PR targeting it gets **zero
check runs**. Not "pending", not "failing": zero.

The auto-merge gate required CI to pass. Applied to CFB it would have waited
forever, on every delivery, silently — a queue of open PRs and no error
anywhere to explain them.

This was found by inspecting the branch, not by reasoning about it, and it is
why two fields exist:

* `ledger_branch_runs_ci=False` tells the gate that absent checks are the
  expected state here, not a missing signal;
* `ledger_validator` replaces what CI would have given. It is the
  **destination's own** script (`scripts/validate_accounting_ledger.py`, on the
  CFB code branch), run in the workflow against the post-import tree, and its
  result is passed to the gate as `--validator-passed`.

So CFB is not merged with *less* verification than MLB. It is merged with
verification the base branch is structurally incapable of running for itself.
Where `ledger_branch_runs_ci` is true — MLB — the gate still demands real
checks, and a destination with neither CI nor a validator cannot auto-merge at
all.

Verified against the live branch: 18 wager rows, 18 settlement rows, 0
problems.

## What CFB delivery does

```
router payload (ephemeral, runner-only)
   -> clone accounting-data (ledger) + main (code)
   -> season derived from the payload's own game dates
   -> python scripts/import_routed_wagers.py --payload ... --season ...
   -> receipts, per row, verdict only
   -> scripts/validate_accounting_ledger.py
   -> commit ONLY wagers/ and settlements/
   -> PR into accounting-data
   -> gate: validator passed + diff inside mergeable_paths + receipts clean
```

Season comes from `scripts/payload_season.py`, which reads the payload's game
dates rather than the wall clock (`SEASON_START_MONTH = 8`). A January bowl
game belongs to the previous season, and a clock-derived season would file it
under the wrong year every single January.

## Receipts

Importers do not agree on a receipt shape, and they should not have to.
`src/kalshi_router/receipts.py` normalises three:

* a bare list of row receipts;
* `{"rows": [...]}`;
* counts only — `{"keysWritten": n, "alreadyPresent": n, "refusals": n}`.

A counts-only receipt is a *valid* receipt and never a failure. What the
normaliser produces is verdict counts and, where a row conflicted, the
**names** of the conflicting fields. Never their values: a receipt that said
`stake: 40 != 70` would put a stake in a public Actions log.

`NO_JUDGEMENT_VERDICTS` and `IDEMPOTENT_RERUN_VERDICTS` are what the gate reads.
A verdict the router has never seen before is not assumed benign.

## Live settlement

`settle` walks fills in a bounded window and structurally cannot reach a
settlement that happened before it. That is fine for a backfill and useless for
a market that settled last Saturday and pays out this Tuesday.

`settle-live` asks Kalshi for **currently settled positions**, so its reach is
the account's open history rather than a fill window. It takes no `--since` by
design — a window parameter on it would imply a reach it does not have.

`.github/workflows/settle-wagers.yml` runs it every four hours, dry-run by
default on dispatch, one destination at a time, and re-runs the import a second
time to prove idempotency before the gate will merge. It carries the same
privacy rules as delivery: counts, never rows.

### A settlement can arrive before its wager is canonical

A wager goes *generated → delivered onto a proposal → merged*, and its market
can settle while it is still on the unmerged proposal (CFB is held for
observation, so that can last a while). On 2026-09-26 that was 2 of 43 CFB
settlements, and `scripts/settlement_season.py` refused the whole batch, so the
41 whose wagers were on the ledger never reached the importer.

The season a batch belongs to is decided by the rows that **match** a wager on
the ledger. If at least one matches and every match is in one season, that
season is used, and the unmatched rows go to the destination importer with the
rest. It refuses them **per row**. They are never written, the delivery is
reported PARTIAL (and stays red), and reconciliation counts them as `refused`,
noting how many "await their wager on the canonical ledger". Every run
re-offers every settled market, so they land on the first run after their
wager merges. It still fails closed, before any importer runs, when **no** row
matches (no season is guessed), when matches span two seasons, or when a row
has no source key.

MLB is not settled from here (`settlement_importer=None`); its repository does
its own. `router_branches()` therefore never produces a settle-MLB branch, and
that is asserted, not incidental.

## The observation period

`auto_merge` is a profile field. MLB is `True` — proven in production over many
deliveries. **CFB starts `False`.**

That is not a weaker gate and not a defect. The gate still runs and still
prints its verdict; the rows are still delivered to `accounting-data`; the pull
request is still opened; the destination's validator still runs; a REFUSAL is
still red and still needs a person. The only thing withheld is the final merge.

The reason is narrow and worth stating: CFB has never completed a real
delivery. The path is proven by a dry run — 41 real wagers, all NEW, 0 failed,
idempotent on re-import, validator accepted — and by tests that drive the
committed bash against a two-branch fixture. Neither is the same as having
watched one land. So the first few real batches are left for a person to read.

Flip it to `True` in `PROFILES` once they look right. That is a one-line,
reviewable commit, which is the point: turning the loop on is a visible act
rather than a default nobody chose.

## NFL activation (2026-09-24)

**What was lost.** NFL had a row shape (`to_nfl_import_row`) from the gap backfill onward, but no profile. Every
post-cutover NFL order was therefore refused as `NO_DESTINATION_IMPORTER`, which `BY_DESIGN_REFUSALS` counted as
correct behaviour: health read `not_routable`, never `blocked`, and no run went red. Week 1 had reached
`handicap-data` only through the one-time backfill, whose window ends at the cutover, so **2026 week 2's NFL
wagers were never delivered** (`no destination importer: 18` on every scheduled run).

**Never silent again.** A sport that HAS a row shape but no active profile is now refused as
`DESTINATION_NOT_ACTIVATED`, which is not excused and so derives into `NEEDS_ATTENTION_REFUSALS`: health is
`blocked` and a person is asked. `NO_DESTINATION_IMPORTER` remains for a sport no repository can hold (TENNIS).

**The profile.** Ledger `handicap-data`, importer on `main` (two checkouts, like CFB). The destination resolves the
week from the real nflverse schedule (`--allow-schedule-download`) and refuses a date matching no single week. Its
importers return one receipt per row (`source_bet_key`, `imported_wager_id` / `settlement_id`, `status` in NEW /
DUPLICATE_NOOP / CONFLICT / REFUSED), and a re-delivery that disagrees with a filed record is a CONFLICT, never a
rewrite. `handicap-data` has no `.github/`, so `scripts/handicap/validate_routed_ledger.py` (the destination's own
whole-ledger rules) supplies the verdict.

**One record per file.** `record_layout="json_per_file"`: the gate reads ADDED files under
`data/imported_wagers/` / `data/wager_settlements/` as rows and any modified, deleted or renamed ledger file as a
rewrite (`merge_delivery_pr.ledger_diff_per_file`). `mergeable_patterns` admit only the minted shapes
`routed-<24 hex>.json` and `stl-<24 hex>.json` under `<season>/week_<NN>/`.

**Auto-merge on.** The same importer path landed 24 week-1 wagers and 24 settlements in production (nfl-edge-finder
#24, #26), and the owner asked for delivery to be hands-off. The gate still decides every batch.

## Adding a destination

Add a `DestinationProfile`. That is the whole change — `DESTINATION_REPOS`,
`SPORTS_WITH_AN_IMPORTER`, `routable_sport_names()` and the workflows all derive
from `PROFILES`. What you will have to supply, because the router cannot invent
it: the importer's argv, the paths delivery may commit, and either CI on the
ledger branch or a validator script the destination owns.
