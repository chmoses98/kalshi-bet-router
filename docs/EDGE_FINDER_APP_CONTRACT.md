# Edge Finder app contract — `edge_finder.app.v1`

The one contract every sport repository and the Kalshi bet router publish for a future Edge Finder web/mobile
app. The app never learns where NFL keeps its board, how tennis names a match, or what NHL calls a candidate:
every sport emits the same files, with the same fields meaning the same things, at a location named by one
registry file. That is the whole design.

```
   sport-specific engines (unchanged)             Kalshi (fills, settlements)
   MLB CFB NFL NBA NHL SOCCER TENNIS                        |
            |  one adapter per repo                 kalshi-bet-router (classify, route)
            v  scripts/app_export.py                        |  canonical wager rows
   edge_finder.app.v1  (app/latest/*.json)          sport accounting ledgers  --->  app export (wagers,
            |                                       (accounting-data branches)      settlements, P&L)
            v                                               |
       Edge Finder app  <-------  router_health.json / recent_deliveries.json (app-data branch)
```

## 1. Where the app reads from

`contract/edge_finder_contract/registry.json` (also published as `sports_registry.json` on the router's
`app-data` branch) names, per sport, the repository, branch and app root. Every root has the same layout:

```
app/latest/
  manifest.json          what this publication is: run_id, generated_at, commit, files + sha256 + counts, freshness
  health.json            the sport's health; written on EVERY attempt, success or failure
  board.json             the compact home-screen board: one row per event with counts, freshness, top picks
  events.json            events (games / matches / fixtures) with participants
  markets.json           every Kalshi market on the board, with current quotes
  model_prices.json      the model's fair probability per market (distinct from recommendations)
  recommendations.json   what the repo's process recommends / flags (distinct from wagers)
  theses.json            why, when the repo already produces that text or structure
  wagers.json            actual observed wagers (router-delivered or recorded), with temporal model links
  settlements.json       how those wagers settled
  runs.json              the model run that produced this publication
  performance.json       P&L / ROI / pending / recent results, never fabricated
  event_detail/<event_id>.json   everything about one event, for the detail screen
```

| sport | repo | branch | app root |
|---|---|---|---|
| MLB | chmoses98/edge-finder-api | main | app/latest |
| CFB | chmoses98/cfb-edge-finder | main | app/latest |
| NFL | chmoses98/nfl-edge-finder | handicap-reports | app/latest |
| NBA | chmoses98/nba-edge-finder | data-archive | app/latest |
| NHL | chmoses98/NHL-edge-finder | data-archive | app/latest |
| SOCCER | chmoses98/soccer-edge-finder | data-archive | app/latest |
| TENNIS | chmoses98/Tennis-Edge-Finder | tennis-data | tennis-edge-finder/data/app/latest |
| CBB | chmoses98/cbb-edge-finder | app-data | app/latest |
| router | chmoses98/kalshi-bet-router | app-data | app/latest (router_health, recent_deliveries, sports_registry) |

Raw GitHub URLs (`raw.githubusercontent.com/<repo>/<branch>/<root>/<file>`) are the Option A static read
surface. Option B (a thin API) maps one to one: `GET /v1/sports` = registry; `GET /v1/sports/{sport}/health`
= health.json; `/events` = board.json / events.json; `GET /v1/events/{id}` = event_detail; `/markets`,
`/recommendations` = filters of the same; `GET /v1/wagers` = wagers.json across sports; `GET /v1/performance`
= performance.json; `GET /v1/system/health` = every health.json + router_health.json.

## 2. Schema versions and evolution

Every document carries `schema_version: "edge_finder.app.v1"` and a `kind`. A consumer rejects any other
version rather than rendering it. Schemas are JSON Schema (`contract/edge_finder_contract/schemas/*.json`),
generated from `schema_defs.py` and pinned by a test; the vendored validator (`validate.py`, no dependencies)
implements the exact subset the schemas use and a test refuses any keyword outside it.

Policy: within v1 a change may only ADD optional (nullable) fields or enum values that a consumer can ignore.
Anything that removes, renames or re-types a field, or changes the meaning of an id, is v2 with a new
`schema_version`, published beside v1 for one season. `CONTRACT_VERSION` (semver) tracks additive changes;
`MANIFEST.json` carries a sha256 per contract file, and every repository's `test_app_contract_v1.py` fails if
its vendored copy drifts. `python -m edge_finder_contract.sync --copy-to <repo>` re-vendors;
`--check <repo>` compares.

## 3. Identities

All ids are deterministic (`ids.py`, sha256 over domain-separated, length-prefixed parts; never a display name):

| id | from | note |
|---|---|---|
| `event_id` `evt_…` | sport + provider namespace + provider id | MLB `mlb_game_pk`, CFB `kalshi_milestone_id`, NFL `nflverse_game_id`, NBA `espn_event_id`/`nba_game_id`, NHL `nhl_game_id`, SOCCER `fixture_id`, TENNIS `kalshi_event_ticker`, CBB `cbb_game_id` (`G` + ESPN event id). Every other provider id travels in `source_ids`. |
| `participant_id` `prt_…` | sport + TEAM/PLAYER/PAIR + namespace + id | tennis players are participants; `home_participant`/`away_participant` are null there |
| `market_id` `mkt_kalshi_<TICKER>` | the Kalshi ticker | already globally unique and stable; no digest |
| `model_price_id` `mp_…` | run_id + market_id + model_version | |
| `recommendation_id` `rec_…` | the repo's own stable id when it has one, else run + market + selection | |
| `wager_id` `wgr_…` | the router's `source_bet_key` (deterministic per Kalshi order) | a manual wager without one uses repo + native id |
| `settlement_id` `stl_…` | the wager_id | one wager settles once |
| `run_id` `run_…` | sport + repo + native run id (or generated_at) | the same run_id is on every file of one publication |

The app correlates schedule → model → Kalshi → recommendation → wager → settlement by these ids alone. Fuzzy
matching (tickers to games, names to players) happens once, in each repository's adapter, and the resolved
mapping is persisted in `source_ids`/`extensions`.

## 4. Time and freshness

Every timestamp is ISO-8601 UTC with a `Z` (`timeutil.to_iso`; a naive input raises). Events carry
`start_time_utc`, `start_time_source`, `start_time_confidence` (SCHEDULED / VERIFIED / ESTIMATED /
**PLACEHOLDER** for a Kalshi nominal time / UNKNOWN) and `effective_start_time_utc` when it differs. Markets
carry `captured_at`; model prices `generated_at` and `inputs_as_of`; recommendations `created_at`/`expires_at`;
events `schedule_updated_at`. `freshness.py` classifies an age as FRESH / AGING / STALE / UNKNOWN against
thresholds the health file publishes, so a client can recompute the same verdict at read time. A stale
Kalshi price is marked STALE, never hidden.

## 5. Health

`health.json` is written on every export attempt. `overall_status` is HEALTHY, DEGRADED (a component degraded
or the last export failed while a good payload stands), STALE (required market/model data past its stale
threshold), UNAVAILABLE (no usable payload or required data missing) or RESEARCH_ONLY (everything fresh and
the sport's models have no betting authority). It is never HEALTHY with stale or missing required data.
`payload_run_id` says which publication the rest of the directory belongs to, `last_export_attempt` when it
was last tried. The router's `router_health.json` (`docs/APP_HEALTH.md`) follows the same vocabulary.

## 6. Atomic publication and last-known-good

`publish.publish` validates every document, checks cross references (recommendation.market_id in markets,
settlement.wager_id in wagers, one run_id everywhere, counts and sha256 in the manifest), writes to a staging
directory and moves files in with the manifest last, then removes files of the previous publication the new one
does not name. On any problem it raises without touching the tree. Exporters catch failures and call
`publish.write_health_only`, so `app/latest` is always the last valid publication plus a health file that
says whether the latest attempt failed.

## 7. Router flow and the wager contract

Kalshi fills → `kalshi-bet-router` (classification by Kalshi's own competition/taxonomy evidence, fail-closed;
one order = one wager; `source_bet_key` = sha256(subaccount, ticker, order id)) → a per-sport payload in the
destination's dialect → the destination's own importer (NEW / DUPLICATE_NOOP / CONFLICT / REFUSED receipts;
re-import must change zero bytes) → a pull request into the sport's ledger branch → the merge gate. Settlements
follow the same path (`settle-wagers.yml`, economics v2). MLB settles itself from the MLB Stats API. NBA,
SOCCER and TENNIS import through the contract's shared `routed_ledger.py`; CFB, NFL, NHL and MLB keep their
own importers. Each sport's app export reads its ledger and emits `wagers.json` / `settlements.json`;
`linkage.apply_links` attaches the newest model price and recommendation that EXISTED BEFORE `placed_at`, or
null — never a later one.

## 8. Accounting

`performance.py` folds wagers and settlements into totals (and by market family, month, source): counts by
result, stake, gross payout, fees, net P&L, ROI over settled stake, open exposure, pending wagers, recent
results, recommended-vs-wagered, CLV when a repo has it, bankroll history when a repo has it. A settlement
without established money counts in `unknown` and `data_completeness`, never as zero.

## 9. What the client must never see

Kalshi keys, GitHub tokens, Airtable credentials, workflow secrets, bankroll dollar balances from the MLB card
(it is sealed into that repository as a secret for a reason), or any write capability. The app layer is
read-only; wager placement stays outside this contract behind an authenticated backend that does not exist
yet. Every exporter's tests assert no secret-shaped string reaches an output file.

## 10. Adding a sport

1. Vendor the contract (`python -m edge_finder_contract.sync --copy-to <repo>`), add `contract` to pytest's
   pythonpath. 2. Write `scripts/app_export.py` using `build.*` constructors and `publish.publish`; read only
   the repo's existing outputs. 3. Add `tests/test_app_contract_v1.py` (sync check, end-to-end on fixtures,
   determinism, failure safety, stale health, real-data smoke). 4. Add the export to the workflow that
   already publishes the repo's latest state. 5. Add the sport to `registry.json`, the router's `Sport` enum,
   competitions, a row builder and a `DestinationProfile`; vendor `routed_ledger.py` wrappers under
   `scripts/accounting/`; create the orphan `accounting-data` branch. 6. Re-scope `DOWNSTREAM_REPO_TOKEN`.

## 11. Known gaps (2026-10-02)

* CFB has no production model: `model_prices.json` is empty by design; the catalog is the product.
* NBA had not simulated its first slate at the time of this pass (preseason starts 2026-10-03): model prices
  and recommendations are empty until the first simulate; health says so.
* NHL's accounting ledger is empty because the router's token cannot open pull requests on NHL-edge-finder;
  17 real wagers wait on `kalshi-router/NHL`. The same token scope blocks NBA/SOCCER/TENNIS deliveries until
  the owner re-scopes it.
* Tennis has no automated recommendation: `recommendations.json` carries the human decision track
  (BET/PASS/WATCH, authority ASSISTED) and the slate's discrepancy flags as RESEARCH_CANDIDATE rows.
* No repository records a commit sha on its model outputs; exporters stamp the exporting workflow's sha.
* CLV is only available where a repo already computes it (MLB bets, tennis research ledger).

## 12. The research graph (contract 1.1.0)

`app/latest/explorer/` adds the navigable research layer (metric registry, rankings with full comparison
universes, time series, entity profiles, event research, market history, capability manifest, search index)
and `packet.py` the AI-ready handicap packet. Everything is additive; see `docs/EDGE_FINDER_RESEARCH_GRAPH.md`.

## 13. Contract 1.2.0 (additive): CBB

* `CBB` (NCAA Division I men's basketball) joins `SPORTS`, with aliases `cbb`, `ncaab`, `ncaam`, `ncaamb`,
  `college basketball`, `ncaa basketball`. Plain `basketball` still means NBA; CBB is never CFB.
* `health.json` gains an OPTIONAL `extensions` object (sport-specific operational state). It is not required:
  every health file published before 1.2.0 still validates, and a consumer may ignore it.
* `health.build_health(market_required=False)`: for a sport that publishes no executable markets by design,
  the market component is NOT_APPLICABLE and does not decide the overall status. The model component stays
  required; the default (`True`) leaves every existing sport unchanged.
* `protocols/edge_finder.handicap.cbb.v1.json`: the CBB handicap protocol extension (frozen research system,
  opponent-adjusted semantics, roster confidence, thin markets).
* `registry.json`: CBB publishes from `chmoses98/cbb-edge-finder`, branch `app-data`, root `app/latest`, built by
  `cbb_edge/app/sift_app` from the immutable pre-tip projection archive; no recommendations, no wagers.

The router does not classify or route CBB wagers (no destination profile); `router_health.by_sport.CBB` reports
NOT_ROUTABLE until one exists.

## 14. Contract 1.3.0 (additive): router health tells pending from failed

Only `router_health` and `recent_deliveries` change, and only by ADDING optional fields and enum values; every
document published before 1.3.0 still validates (pinned by
`tests/test_router_health_repair_2026_10_08.py::test_a_health_document_published_before_1_3_0_still_validates`).

* `router_health.by_sport.<SPORT>.status` gains `AWAITING_MANUAL_MERGE`: every eligible wager is delivered to the
  router's open proposal, every gate condition passed, and the destination is in its observation period
  (`auto_merge` off), so a person merges it. Pending by design; it never degrades `overall_status`.
* `by_sport.<SPORT>` gains optional `on_ledger`, `proposed_not_merged` and `settlement` (null when the last
  settlement run did not consider the sport): `{status, rows, on_ledger, proposed_not_merged,
  waiting_for_parent_wager, refused, unaccounted}` with `status` one of `SETTLED`, `NO_OP`,
  `WAITING_FOR_PARENT_WAGER`, `AWAITING_MANUAL_MERGE`, `FAILED`, `UNKNOWN`.
* top level gains optional `awaiting_manual_merge` and `waiting_for_parent_wager` (counts).
* `recent_deliveries.items[].status` gains `WAITING_FOR_PARENT_WAGER` and `AWAITING_MANUAL_MERGE`.

`overall_status` stays DEGRADED for every genuine problem: a BLOCKED wager, a failed destination, a red
settlement run, or a settlement a destination refused. `blocked` and `deferred` are now populated (they were
null: the publisher read counters the status line never carried).

No other repository consumes `router_health`; their vendored 1.2.0 copies stay valid and pick 1.3.0 up on the next
`edge_finder_contract.sync`.
