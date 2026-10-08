# Destinations — the profile table, and CFB / NFL / NHL activation

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
| `code_branch` | the importer is CODE and comes from this branch's head, in its own checkout. CFB's importer does not live on its ledger branch at all; MLB's does, but the work tree the router imports into is often the *proposal branch* (seeded on top of `kalshi-router/MLB`), whose copy of the importer is as old as the proposal. Run 36440425803 executed that stale copy twelve minutes after edge-finder-api #250 had fixed it on `main`. Every profile now names `{code}/...` |
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

### CFB settlement economics v2 (2026-09-28)

CFB's profile now declares `settlement_economics = router-settlement-economics.v2`,
so every CFB settlement row the router emits carries `economics_version` and a
net of `gross − stake` (once the exchange's `fee_cost` reconciles to the
owner's entry fees). The 93 CFB settlements filed before this were v1 rows,
whose net subtracted the entry fee twice.

The destination answers a v2 row for a wager it already holds under v1 with an
**append-only amendment** in `settlement_amendments/<season>.jsonl` — never a
rewrite of the v1 row — identified deterministically from the wager's key and
the contract, and receipts it `CORRECTED` with both the `settlement_id` and the
`amendment_id`. A repeat is `DUPLICATE_NOOP`; a disagreeing correction is
`REFUSED` and the batch stays for a person. `CORRECTED` and `DUPLICATE_NOOP`
were already in the gate's no-judgement vocabulary, so nothing in the gate
changed; what changed is the profile: `settlement_amendments/` is a
committable prefix and `settlement_amendments/<year>.jsonl` an exact mergeable
path. `tests/test_cfb_amendment_gate.py` drives the gate to MERGE on a
receipted amendment and to REFUSE on an unreceipted one, a refused correction,
a stray file, a removed row, a non-idempotent re-import and a failed validator.

cfb-edge-finder's own backfill (`scripts/amend_settlement_economics.py`) filed
89 amendments for the rows whose correction the filed row itself proves; the
router's first v2 run lands on those same ids as no-ops and establishes the
four 2026-09-19 shared-position rows v1 had refused, from live fee evidence.

## The observation period

`auto_merge` is a profile field. MLB is `True` — proven in production over many
deliveries. **CFB started `False` on 2026-09-21 and was flipped to `True` on
2026-09-28.**

Holding was not a weaker gate and not a defect. The gate still ran and still
printed its verdict; the rows were still delivered to `accounting-data`; the
pull request was still opened; the destination's validator still ran; a REFUSAL
was still red and still needed a person. The only thing withheld was the final
merge.

The reason was narrow: CFB had never completed a real delivery. The path was
proven by a dry run — 41 real wagers, all NEW, 0 failed, idempotent on
re-import, validator accepted — and by tests that drive the committed bash
against a two-branch fixture. Neither is the same as having watched one land.
So the first real batches were left for a person to read.

They were read. cfb-edge-finder #58 (27 wagers) and #57 (34 settlements)
landed by hand on 2026-09-27, and the 2026-09-26 postmortem reconciled 32 of
32 Saturday wagers with 0 unaccounted. The hold also cost those 27 wagers about
23 hours on the proposal, during which 2 of their markets settled before the
wagers were canonical (see above). Closing the observation period is the
one-field commit this section promised. What did not change: the twelve gate
conditions, the receipt vocabulary, the validator, the mergeable paths beyond
the amendment ledger, and the rule that a REFUSAL needs a person.

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

## NHL activation (2026-09-29) — ACCOUNTING ONLY

NHL is a normal destination for **wagers the owner places by hand on Kalshi**. The router reads the exchange's own fills (read-only
access), and NHL-edge-finder records them in a ledger that is entirely separate from its research models. Nothing
about this gives any NHL model a say in a bet: DATA_ONLY_V1 and DATA_ONLY_V2 remain RESEARCH_ONLY, and no row carries
model provenance. The destination refuses such fields outright.

| | |
|---|---|
| repo | `chmoses98/NHL-edge-finder` |
| ledger branch | `accounting-data`, an ORPHAN branch: a README plus `data/accounting/`, and none of the repository's model or research data |
| code branch | `main` (`scripts/accounting/import_routed_wagers.py`, `import_routed_settlements.py`, `validate_routed_ledger.py`) |
| merge paths (exact) | `data/accounting/wagers.jsonl`, `data/accounting/settlements.jsonl` |
| committable prefix | `data/accounting/` |
| season | none: one file each, because an NHL season spans two calendar years and nothing should have to guess one |
| settlement economics | `router-settlement-economics.v2` from the first row (no v1 history, so no amendment path) |
| identity | minted by the destination: `nhlw-`/`nhls-` + sha256(`source_bet_key`)[:24] |
| CI on the ledger branch | none (orphan). The destination's own validator supplies the verdict, as for CFB and NFL |
| auto-merge | **False** — held until the first observed NHL delivery (see "Production verification" below) |

**Classification.** Kalshi's competition string is the evidence:
- **Resolve to NHL:** "Pro Hockey" (documented by `/milestones` and listed under Hockey in the live `filters_by_sport`), "NHL" and "National Hockey League".
- **Stay `OTHER`:** College hockey and the foreign leagues the catalogue names beside "Pro Hockey" (KHL, SHL, Finland Liiga, Germany DEL, Czech Extraliga, Switzerland National League, plus AHL and PWHL).
- **The Hockey heading:** it is now an *ambiguous* family, so an unknown hockey competition is UNRESOLVED rather than NHL, and the bare word "hockey" in series metadata is never NHL evidence.

**The collision gate stays as strict as it was.** The NHL is a CLOSED-vocabulary league: it is recognised only by
those exact names. A Hockey claimant of some other name therefore offers no rival reading of it. This matters because
the live catalogue has filed "Pro Baseball" under Hockey (2026-09-15), and MLB must stay unaffected. Any claimant
holding an open-vocabulary league (MLB, NFL, CFB) still refuses exactly as before.

**Series registry.** Twenty NHL series, all `verified=True` from NHL-edge-finder's live discovery, still rank as the
weakest evidence and never override a contradicting competition.

**Credential.** Delivery uses the shared `DOWNSTREAM_REPO_TOKEN`. `downstream-credential-probe.yml` now also probes
`chmoses98/NHL-edge-finder` and reports push permission there without writing anything.

**Tennis** remains unrouted: no profile, no row shape.

### Production verification (2026-09-29, router main `1dc65f3`)

| Check | Run | Result |
|---|---|---|
| Series probe | 36624797846 | all 20 NHL registry series CONFIRMED; taxonomy collisions 0; public opening-night sample `{'NHL': 6}` via `L1_event_competition` / `Pro Hockey`; `NHL_LIVE_SAMPLE_ALL_NHL=true` |
| Delivery, dry run | 36624801843 | success; eligible 212, CFB 75 / MLB 81 / NFL 56, all DUPLICATE_NOOP, destinations failed 0; `ROUTER_COVERAGE sport=NHL eligible=0` |
| Delivery, scheduled production | 36627519837 (`DRY_RUN=false`) | success; identical counts; reconciliation UNACCOUNTED 0 for every destination; no `::error::` |
| Settlement, production | 36624693205 | success; NHL listed with `router-settlement-economics.v2`; 131 settled, CFB 75 / NFL 56 DUPLICATE_NOOP; no NHL rows |
| Pre-merge baseline | 36623489485 (`4e9532a`) | the SAME numbers: eligible 212, blocked 14, CFB 75 / MLB 81 / NFL 56. Adding NHL moved no existing routing decision |
| Credential probe | 36624794314 | **NHL-edge-finder: read true, push true, but opening a pull request answers 403** ("the token lacks Pull requests: write"); the other four destinations answer 422 (validation reached) |

`HEALTH: blocked` on those runs is pre-existing and not NHL: 5 soccer orders (OTHER) and 9 NFL combo markets with no
competition, byte-for-byte the same on the pre-merge baseline.

**No NHL wager exists yet**, so no NHL row has been delivered and no NHL settlement has been observed. The settlement
path is TESTED (`tests/test_nhl_destination.py`, including the end-to-end test against NHL-edge-finder's real
importers: import, byte-identical re-import as DUPLICATE_NOOP, settlement import and re-import, orphan refusal,
validator, containment and reconciliation), not OBSERVED.

**Why `auto_merge` stays False.** Two things are not yet proven in production: (1) the router cannot open the pull
request on NHL-edge-finder with today's token, so the last hop of delivery has never succeeded there; (2) no real
NHL row has passed through the production pipeline. Until then an NHL wager fails SAFE: the branch is pushed, the
pull-request step fails with a named `::error::` for NHL only (the loop continues to the other destinations), and every
later run retries the same idempotent batch, so the wager is recorded the first run after the token is fixed. Flipping
the flag is a one-line change to the NHL profile (plus its test expectation) once the first NHL delivery has been
observed going through the gate. The gate itself is unchanged.

## Adding a destination

Add a `DestinationProfile`. That is the whole change — `DESTINATION_REPOS`,
`SPORTS_WITH_AN_IMPORTER`, `routable_sport_names()` and the workflows all derive
from `PROFILES`. What you will have to supply, because the router cannot invent
it: the importer's argv, the paths delivery may commit, and either CI on the
ledger branch or a validator script the destination owns.

## NBA, SOCCER and TENNIS activation (2026-10-02, app-readiness pass) — ACCOUNTING ONLY

Three destinations were added in one change, and they share one importer. Each repository vendors the Edge
Finder contract (`contract/edge_finder_contract/`, authored in this repository) and its
`routed_ledger.py` module is the destination's wager/settlement ledger: the NHL ledger's exact contract
(`payload {"importBatchId", "rows"}`, identity minted from `source_bet_key`, `NEW / DUPLICATE_NOOP / CONFLICT /
REFUSED` receipts, append-only JSONL, provenance fields refused, counts-only stdout), parameterised by sport.
`scripts/accounting/import_routed_wagers.py`, `import_routed_settlements.py` and `validate_routed_ledger.py`
in each repository are thin wrappers over it. One module to prove, three sports that prove it
(`tests/test_shared_destinations.py`, which also runs each destination's own scripts end to end when a sibling
checkout exists).

| | NBA | SOCCER | TENNIS |
|---|---|---|---|
| repo | `chmoses98/nba-edge-finder` | `chmoses98/soccer-edge-finder` | `chmoses98/Tennis-Edge-Finder` |
| ledger branch | `accounting-data` (orphan) | `accounting-data` (orphan) | `accounting-data` (orphan) |
| ledger files | `data/accounting/wagers.jsonl`, `settlements.jsonl` | same | same |
| identity | `nbaw-`/`nbas-` + sha256(key)[:24] | `socw-`/`socs-` | `tenw-`/`tens-` |
| economics | v2 from the first row | v2 | v2 |
| auto_merge | **False** (observation) | **True** (graduated 2026-10-08) | **False** |

**Classification.**
* NBA is a CLOSED-vocabulary league like the NHL: "Pro Basketball (M)", "NBA", "National Basketball
  Association" and nothing else. The Basketball heading is now an *ambiguous* family (WNBA, college,
  EuroLeague sit beside it) that narrows to {NBA} and never resolves on its own; "Pro Basketball (W)", "WNBA"
  and college basketball are positively OTHER.
* SOCCER resolves at the taxonomy SPORT level, exactly as tennis does: everything under Kalshi's Soccer
  heading is SOCCER. Without the taxonomy, only competition names that belong to no other sport decide
  (`SOCCER_COMPETITION_TOKENS`: UEFA, La Liga, MLS, Liga MX, Brasileirão ...). "Premier League" alone is
  UNRESOLVED — the Indian Premier League is cricket. The Football family is untouched: it still means
  NFL/CFB and still fails closed without a league.
* TENNIS was already classified; it only lacked a destination.
* The L5 registry gained the 60 NBA series nba-edge-finder's live discovery classes MODELABLE/BUILDABLE and
  the 253 soccer series soccer-edge-finder's discovery owns by the exchange's own tag and that carried
  markets. Still last-resort evidence; never an override.

**Credential.** Delivery uses the shared `DOWNSTREAM_REPO_TOKEN`, which must be re-scoped to include the three
repositories (and must carry *Pull requests: write* everywhere — the 2026-09-29 probe found it lacks that on
NHL-edge-finder, which is why 17 NHL wagers sit unmerged on `kalshi-router/NHL`). `downstream-credential-probe.yml`
now probes all seven destinations. Until the token is fixed a wager for these sports fails SAFE: the branch is
pushed, the pull-request step fails for that sport only, and every later run retries the same idempotent batch.

**Soccer's own importer.** `soccer_edge.router` (PositionV1, year-sharded `archive/positions/`) remains in that
repository as its translation layer, but delivery goes through the shared ledger so the router has one
destination shape to reconcile, one validator contract, and one merge-path rule for all three.

## Router health repair (2026-10-08): combos, waiting parents, observation

Production before this change (delivery run 37714804665, settlement run 37713992507): `HEALTH: blocked` with
30 BLOCKED orders (29 `sport unresolved` / `competition absent`, 1 `game date not established`), and the
settlement run red because NHL (19) and SOCCER (9) settlements were refused as orphans.

### The 29: multivariate COMBO markets the router could already prove

All 29 were COMBO (parlay) markets -- series category `Exotics`, legs stated in `mve_selected_legs`. A combo's own
event carries no competition and its series names no sport, by construction, so L1-L5 had nothing to stand on.
The legs do: the router's refusal profiler (`refusals.py`) classified every leg through the full hierarchy on the
leg's own metadata and printed, every run, NFL=24, CFB=4, MLB=1 -- and the classifier ignored it. Not a new sport,
not a registry gap, not missing metadata: a classification gap.

`classify.classify_with_legs` closes it, and nothing wider:

* only a combo whose OWN level found nothing (`competition_absent` / `insufficient`) may be completed by its legs;
  every terminal combo-level verdict (malformed metadata, unknown or ambiguous competition, conflict) stays;
* every leg must be stated with a ticker and classify into ONE routable sport -- an unresolved or OTHER leg, a
  nested combo, or two sports keep it UNRESOLVED (`combo_leg_unresolved`, `combo_legs_span_sports`,
  `combo_legs_unavailable`); combo evidence that disagrees with the legs is an `evidence_conflict`;
* its game date comes from the legs only, and only when every leg is dated and all share one date
  (`wager.resolve_combo_game_date`); the combo's own ticker is never parsed for a date;
* the destination must be able to RECORD a combo (`DestinationProfile.records_combo_wagers`): True for CFB, NFL,
  NHL, NBA, SOCCER, TENNIS (importers and validators treat `market_ticker` as an opaque string; settlement is the
  exchange's own settlement of that market, delivered by this router), False for MLB, which settles its own wagers
  from the contract it parses out of the ticker and explicitly defers combos. An MLB combo is refused
  `combo_not_recordable_by_destination` -- still BLOCKED, under its true reason.

  **MLB joined 2026-10-08** (edge-finder-api #278): it records a combo as ONE `wagerStructure: COMBO_CONTRACT`
  wager -- the combo ticker its opaque identity, side/price/stake/contracts/economics/identity as any straight
  wager, the legs only as `comboLegs` provenance -- and settles it ONLY from the exchange's own final yes/no result
  for that contract (`lib/edgelab/combo_contract_settlement.py`), leaving it pending when the result is not final
  or not binary. `to_mlb_import_row` adds `wagerStructure`, `marketFamily: multi_market_combo` and `comboLegs`
  only for a combo, so every straight MLB row is byte-for-byte unchanged.

No ticker prefix, collection name or title decides anything.

### Settlements WAITING_FOR_PARENT_WAGER

`scripts/settlement_parents.py` (policy: `src/kalshi_router/settlement_parents.py`) runs before every settlement
importer. A settled row whose wager is on the canonical ledger imports as before. A row whose wager is on the
router's wager proposal, where that proposal is VALID (open, non-draft pull request from `kalshi-router/<SPORT>`
in the destination's own repository into its ledger branch, head = the commit just read, exactly one wager record
with that key and the same ticker and side) is WITHHELD: not written, not settled, not refused, re-offered every
run, imported normally the first run after the wager merges (the importer's idempotency makes that exactly once).
Anything else goes to the importer, which refuses it -- red, exactly as before. Reconciliation counts the
withheld rows as `WAITING_FOR_PARENT_WAGER`; the health publisher shows them without degrading the router.

### NHL and SOCCER auto-merge: KEEP_OBSERVATION_MODE (both)

| evidence (to 2026-10-08) | NHL | SOCCER |
|---|---|---|
| wager rows delivered / refused / unaccounted | 19 / 0 / 0 | 9 / 0 / 0 |
| importer idempotency (second identical import) | changed nothing, every run | changed nothing, every run |
| destination validator | accepted | accepted |
| merge gate (12 conditions) | MERGE, all PASS (#8) | MERGE, all PASS (#33) |
| proposal history | one router commit (2026-10-02 23:33Z), append-only, no manual edit | same |
| canonical identity reconciliation | proposed-not-merged 19, UNACCOUNTED 0 | 9, UNACCOUNTED 0 |
| real batch read and merged by a person | **no** | **no** |
| settlement observed landing in production | **no** (never possible: no canonical parent yet) | **no** |

The gate passes, but the router's established graduation rule is not the gate alone. CFB's observation closed only
after a person had read and hand-merged real wager AND settlement batches (#58, #57) and a postmortem reconciled
them; NHL's own profile lists `settlement` among the things to prove end to end. Neither has happened for NHL or
SOCCER, so `auto_merge` stays False -- no gate lowered, nothing flipped to clear health. With
`AWAITING_MANUAL_MERGE` and `WAITING_FOR_PARENT_WAGER` the hold no longer reads as a failure. Merging NHL #8 and
SOCCER #33 by hand is the next observation step; the next settlement run then imports their settlements and
proposes them, and once both have been read the flag is the one-line change this file has always described.

### NHL and SOCCER graduate: READY_FOR_AUTO_MERGE (2026-10-08, later the same day)

The one criterion the table above found unmet -- a real batch read and merged by a person, and a settlement landed
-- was then met, on the owner's instruction, with every pre-merge fact re-verified independently of the router's own
gate (head unchanged, router-authored single commit on the current ledger head, append-only, the destination's own
validator incl. `--against` the base, every row carrying the router identity, no duplicate key, no manual edit):

| | NHL | SOCCER |
|---|---|---|
| wager batch merged by hand | #8 → `c237b89` (19) | #33 → `86435d8` (9) |
| settlement run after the merge | 37731709440: 19 NEW, 0 waiting, 0 refused, 0 unaccounted | same run: 9 NEW, 0/0/0 |
| settlement batch merged by hand | #26 → `d2e0842` (19, every one paired with its wager by key, ticker and side) | #37 → `c5c0dab` (9) |
| orphan refusal observed in production | 19 (settle run 37713992507, before the wagers merged) | 9 (same run) |

So every item NHL's profile listed (classifier on real markets, import, identical re-import DUPLICATE_NOOP,
settlement, orphan refusal, validator, containment, reconciliation, a production dry run) and the CFB precedent's
"real batches read by a person" are satisfied. `auto_merge=True` for NHL and SOCCER is the one-field change this file
promised; the twelve gate conditions, the receipt vocabulary, the validator, the mergeable paths and "a REFUSAL needs
a person" are unchanged. NBA and TENNIS stay in observation: no real wager has reached either.
