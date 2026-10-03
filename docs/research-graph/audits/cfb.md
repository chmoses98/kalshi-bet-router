# Phase 2 research-data audit — CFB (`chmoses98/cfb-edge-finder`, main @ c392269)

Audited read-only on 2026-10-03. Data branches were fetched (`research-data` deepened to its full
7,098 commits, `research-sidecar` to 57, `main` unshallowed to 316) and extracted under the scratch
dir; nothing in the repository was modified. Every count below was measured with Python against the
real files; commands and paths are cited inline.

---

## 1. Summary

1. **The live product is a Kalshi market inventory, not a model.** `data/live/cfb_market_catalog.json`
   + `data/live/games/<game_key>.json` (261 games, 13,922 contracts, 19 families) is rebuilt by
   `kalshi-market-catalog.yml` every 30 min in the Thu–Sun window / 6-hourly otherwise and committed
   only on fingerprint change: **69 catalog commits from 2026-09-18 to 2026-10-03** (~4.3/day).
   `app/latest` is exported from this plus the `accounting-data` ledger and nothing else. VERIFIED.
2. **The accounting ledger is real and production-fed**: 104 wagers / 102 settlements / 93 economics
   amendments (2026-09-11 → 2026-10-03), written by the kalshi-bet-router via `kalshi-router/*`
   branches, validated by 20 test files, surfaced in `app/latest/performance.json`. VERIFIED.
3. **There IS substantial team-level stats data — but it is frozen, off `main`, and invisible to the
   app.** The `research-data` branch holds a complete CFBD cache for **2014–2025** (games with scores
   and pregame Elo, per-game team box scores, per-game advanced stats with PPA/success rate/
   explosiveness/line yards/havoc, drives, multi-book betting lines, SP+, season advanced, recruiting,
   talent, returning production, coaches, portal, AP/Coaches/CFP rankings, FBS team table with venue
   coordinates), fetched **once** on 2026-09-02 on the CFBD free tier (quota now 0/1000 until
   2026-10-01). 2026 has only the preseason tables and the schedule; **2026 in-season team stats were
   deliberately firewalled and never fetched.** PARTIAL (real vendor data, single snapshot).
4. On top of that cache sits a **10,393-game × 261-column feature table (2015–2026)** with
   walk-forward **opponent-adjusted** offense/defense states for 26 metrics per team per game, plus
   preseason priors, rest/travel/venue, and market lines — built ad hoc on a research branch whose
   builder code is **not on main**. RESEARCH.
5. **Market price history exists in two forms**: (a) a 44,019-row prospective observation ledger
   (7,344 tickers, 156 games, 9 pre-kickoff checkpoints incl. 4,283 CLOSING rows) for 2026-08-26 →
   2026-09-16 with the retired model's probability attached, collector hibernated since 2026-09-17;
   (b) the catalog's git history, one snapshot per fingerprint change, reconstructable per ticker
   (sampled: median 4 distinct quote-states per ticker, 44,003 distinct tickers across 18 of 69
   commits). Neither is a VERIFIED series; both are PARTIAL.
6. **There is NO player-level data anywhere**: no rosters, no player stats, no player game logs, no
   usage, no depth charts. The only player-shaped rows are transfer-portal entries (2021–2026) and
   ESPN injury URL references in a sidecar (276 non-empty rows). UNAVAILABLE.
7. **Weather/odds/injury capture exists but is hibernated**: `research-sidecar` branch, 5,103 Open-Meteo
   kickoff-hour forecasts + 5,103 DraftKings odds rows + 9,510 ESPN injury rows for 254 games,
   2026-09-02 → 2026-09-17. 2025 historical weather (650–736 games) on `research-data-v2enrich`. RESEARCH.
8. **Identity is three disjoint schemes with no committed cross-map**: Kalshi team codes/UUIDs (catalog,
   app), CFBD school names + numeric ids (research cache), canonical slugs (`ids.py`, 138-team registry
   with empty `vendor_ids`). The research observation ledger is the only Kalshi-ticker ↔ canonical
   game ↔ CFBD-id bridge, for 156 games of 2026 weeks 1–4.
9. Strongest areas: market inventory, market history (short), wager accounting, 12-season team-level
   historical stats with a tested opponent-adjustment method. Weakest: anything player-level, anything
   2026 in-season for team stats, any scheduled research ingestion (all hibernated, CFBD quota exhausted).
10. Honest bottom line: **today's production surface supports market-inventory + market-history +
    accounting only.** Team/game research capabilities can be exposed from the frozen 2014–2025 corpus
    as RESEARCH/PARTIAL with explicit "as of 2026-09-02, no 2026 in-season stats" labelling.

---

## 2. Data branches and layout

`git ls-remote --heads origin` lists 66 heads. Data-bearing ones (everything else is `claude/*` feature branches):

| Branch | Root | Contents | Size | Commits / span | Immutable vs replaced |
|---|---|---|---|---|---|
| `main` | `data/live/` | `cfb_market_catalog.json` (1.37 MB), `games/*.json` (261 files, 58.8 MB, avg 225 KB), `cfb_catalog_status.json` | 59 MB | 69 catalog commits, 2026-09-18 00:23 → 2026-10-03 02:44 | **Replaced** each change; past games pruned (`game_files_pruned`); history only in git |
| `main` | `app/latest/` | contract bundle `edge_finder.app.v1`: board/events/markets/wagers/settlements/performance/health/manifest/runs + 261 `event_detail/*.json`; `model_prices`,`theses`,`recommendations` empty by design | 29.9 MB | 5 commits (2026-10-02→03), `app-export.yml` `*/30 * * * *` + `workflow_run` | Replaced |
| `main` | `data/research_inputs/v2_games_for_weather.csv` | 10,391 games 2015+ with ESPN/CFBD game id, kick, venue_id, lat/long, dome, name/city/state | 0.9 MB | 1 commit 2026-09-03 | Static |
| `accounting-data` (orphan) | `wagers/2026.jsonl`, `settlements/2026.jsonl`, `settlement_amendments/2026.jsonl` | 104 / 102 / 93 rows | 252 KB | HEAD 2026-10-03 00:15 (PR #76); fed by `kalshi-router/CFB`, `kalshi-router/settle-CFB`, `kalshi-router/backfill-CFB`, `ledger/economics-v2-backfill` | **Append-only**, deduped on `source_bet_key` |
| `research-data` (orphan) | `data/research_cache/v2/{2014..2026}/*.json.gz` | 219 gz files: CFBD endpoint caches (see §5) | 25.8 MB gz | 1 bot commit 2026-09-02 05:24 (`preseason-research-fetch.yml` mode=v2) | Static snapshot |
| `research-data` | `data/research_cache/preseason/{2019,2021..2026}.json` | games/coaches/talent/returning_production | 8.6 MB | 4 commits 2026-08-28 | Static |
| `research-data` | `data/research/observations/2026/*.jsonl` | 44,019 Kalshi research observations | 117 MB | 298 commits 2026-08-26 → 2026-09-16 | **Append-only**, UTC-date shards, dedup on `observation_key` |
| `research-data` | `data/research/settlements/2026/` | 11,509 market settlement rows | 12.1 MB | 29 commits → 2026-09-16 | Append-only (fact fingerprint dedup) |
| `research-data` | `data/research/attributions/2026/` | 37,979 per-observation outcome attributions incl. closing linkage & research P&L | 85.2 MB | 18 commits → 2026-09-14 | Append-only |
| `research-data` | `data/research/shadow/2026/`, `v2_shadow/2026/` | 41,663 talent-prior shadow rows; 37,449 V2 shadow rows; `v2_shadow/2026.artifact.json` (329 KB frozen predictions) | 78.6 + 43.9 MB | 286 / 249 commits → 2026-09-16 | Append-only |
| `research-data` | `data/research/capture_state/`, `heartbeats/`, `operational_state/`, `cfbd_access/` | 66,256 capture-state rows; 7,034 heartbeats; two state JSONs (`CFBD_QUOTA_EXHAUSTED`, `DEADLINE_AT_RISK`) | 30 MB | heartbeats 7,035 commits → 2026-09-25 15:26 (last commit on branch, FAIL-CLOSED) | Append / replaced |
| `research-data` | `data/research/football_state/2026.json` (+manifest) | 3,679 all-division 2026 schedule games (CFBD) + history 2022–2025 (`games` 3,705/season, `advanced` plays-only) + 683 all-division teams | 15.1 MB | 15 commits, last 2026-09-03 | Replaced |
| `research-data` | `data/research/schedule_state/2026.json` | 250 ESPN schedule facts (status/kickoff/provider_event_id) | 78 KB | 3,475 commits → 2026-09-17 | Replaced |
| `research-data` | `data/research/v2/` | `dataset.parquet` (10,393×261, 11 MB) + 6 variant parquets, `registry.jsonl` (92 experiments), 5 leaderboards, `preds/*.parquet` (6,266 rows each), `control_predictions.csv` (3,675), `eval/*.json` | 84 MB | 1 human commit 2026-09-02 07:11 | Static |
| `research-data-v2enrich` | `data/research_cache/v2_enrich/weather/` | Open-Meteo 2025 kickoff weather: archive 670, hforecast 653, prevrun 736 games (parquet) | 150 KB | 1 commit 2026-09-03 | Static |
| `research-sidecar` (orphan) | `data/research/live_sidecar/{weather_forecast,espn_odds,espn_injuries}/2026.jsonl` | 5,103 / 5,103 / 9,510 rows, 254 games | 10.3 MB | 57 commits 2026-09-02 → 2026-09-17 21:45 | Append-only |
| `research-data-shard-migration-proof` | same as research-data minus last shards | proof branch | — | 2026-09-14 | n/a |
| `claude/cfb-model-v2-research-krupoc` | `src/cfb_edge_finder/research/v2/*.py`, `scripts/v2_*.py`, `scripts/fetch_v2_research_cache.py` | **the only copy of the v2 cache fetcher and dataset/state builder** | code | HEAD c1eedc9 | Not merged to main |

Gitignored on main (never committed): `data/research/`, `data/execution/` (the ~16 MB execution slate with ESPN-derived records/form/rest/weather context), `data/schedules/`, `data/raw/`, `data/archive/`.

**Workflow cadence on main** (`grep cron .github/workflows/*.yml`):
- `kalshi-market-catalog.yml`: `*/30 18-23 * * 4`, `*/30 * * * 5,6`, `*/30 0-6 * * 0`, `40 */6 * * 0-4` — ACTIVE.
- `app-export.yml`: `*/30 * * * *` + `workflow_run` — ACTIVE.
- `cfb-execution-slate.yml`: `0 11,15,22 * * 6`, `0 23 * * 5` — ACTIVE (writes gitignored local state only).
- `research-capture.yml` (`*/10`), `research-collection-conductor.yml`, `research-settlement.yml` (`0 */6`), `live-info-sidecar.yml` (`17 */6`): schedules **commented out**, registry `src/cfb_edge_finder/research/hibernation.py` lists all four `HIBERNATED` since 2026-09-17; a runtime `hibernation-gate` refuses unattended dispatch.
- `preseason-research-fetch.yml`, `backtest-cfb-baseline-live.yml`, `validate-*-live.yml`, probes: `workflow_dispatch` only.

---

## 3. Identity model

| Entity | Scheme | Where | Coverage | Notes |
|---|---|---|---|---|
| Game (live) | `game_key` = event-ticker suffix `YYMONDD<AWAYCODE><HOMECODE>` (e.g. `26OCT02LIBDEL`); `identity.milestone_id` (Kalshi `football_game` milestone) | `src/cfb_edge_finder/catalog/identity.py:1-70`, catalog `games[].identity` | 261 games; 216/261 milestone-named; 45 with `division`/`week` = null | Verified against 1,946 live event tickers (1,669 matched) |
| Game (app) | `event_id = ids.event_id("CFB","kalshi_milestone_id",milestone_id)` else `("CFB","kalshi_game_key",game_key)` | `scripts/app_export.py`, `contract/edge_finder_contract/ids.py` | 261 events | Research-era games cannot be mapped to milestone-based ids (they carry no milestone_id) |
| Team (live/app) | Kalshi ticker code (`LIB`,`DEL`) + `custom_strike.football_team` UUID; names from event `title` ("Liberty at Delaware") | catalog markets, `execution/semantics.py:210 build_game_teams` (home/away from milestone "A at B" phrasing, "A vs B" = convention) | all 261 games | **No code→school table exists**; `app_export.py:118` emits participant source `kalshi_team_code` |
| Team (research) | canonical slug `slugify_team()` (`ids.py`); registry of 138 FBS `TeamRecord`s, 74 exact-match aliases, 1 ambiguous (`Miami`); `vendor_ids={}` for all 138 | `src/cfb_edge_finder/teams/registry.py`, `ids.py` | FBS only | FCS recognised only as a pooled set (`teams/fcs_identity.py`) |
| Team (CFBD cache) | `school` string + numeric `id` (e.g. Air Force 2005), `abbreviation`, `alternateNames`, conference, venue `location{id,lat,long,dome,elevation,capacity,timezone}`, logos | `research_cache/v2/<yr>/teams_fbs.json.gz` 128–138 rows × 13 seasons; `season_advanced`/`ratings_sp`/`talent` keyed by school name | 2014–2026 | **Best available identity table**; `abbreviation` is CFBD's, NOT verified equal to Kalshi codes |
| Game (research) | canonical `cfb-2026-wk02-new-mexico-state-at-hawaii`; `game_result.source_game_id` = CFBD numeric (401864578) | observations / settlements / attributions | 156 games (obs), 153 (settlements) | Observation rows carry `kalshi_event_ticker` + `kalshi_market_ticker` ⇒ **implicit Kalshi↔canonical↔CFBD bridge for those 156 games only** |
| Game (v2 dataset) | CFBD numeric `game_id`, `home`/`away` school names, `home_id_cfbd`, `home_slug`/`away_slug` | `data/research/v2/dataset.parquet` | 10,393 games 2015–2026 | 140 distinct home teams |
| Game (sidecar / weather csv) | ESPN event id (= CFBD id, 401…) + `espn_team_id`, `venue_id` | `research-sidecar`, `data/research_inputs/v2_games_for_weather.csv` | 254 games 2026; 10,391 games 2015+ | |
| Market | Kalshi `market_ticker` everywhere; app `market_id = mkt_kalshi_<TICKER>` | all layers | — | **The one key shared by every layer** (catalog, app, observations, attributions, wagers) |
| Wager | `source_bet_key = kalshi:v1:<sha256>`; `wager_id` | accounting-data | 104 | joins to game via ticker → event ticker → game_key (`parse_event_ticker`) |

Known collisions / gaps: no Kalshi-code↔CFBD map (would need building from `teams_fbs` `abbreviation`/`alternateNames` + manual review); registry is FBS-only while 101/261 catalog games are FCS; neutral-site home/away may differ between Kalshi and CFBD (`ids.py` sorts slugs for neutral games); bowl slugs sponsor-branded (documented risk in `ids.py`).

---

## 4. CAPABILITY MATRIX

| Capability | Status | Origin | Coverage | Cadence | Tests | Notes |
|---|---|---|---|---|---|---|
| Team metrics (season) | **PARTIAL** | `research-data:data/research_cache/v2/<yr>/season_advanced.json.gz`, `ratings_sp`, `talent`, `recruiting_teams`, `returning_production` (CFBD) | 2014–2025, 128–137 FBS teams/season; 2026 only talent/recruiting/returning | one-off 2026-09-02 | none read it on main | real vendor data, frozen snapshot, no 2026 in-season |
| Player metrics | **UNAVAILABLE** | — | — | — | — | manifest `not_fetched['/roster']`; no player stats anywhere |
| Game logs (team, per game) | **PARTIAL** | `games_teams.json.gz` (35 box categories), `advanced_regular/_postseason.json.gz` (PPA/SR/expl/line yds/down splits), `drives_*.json.gz` | 2014–2025: 10,676 games box; 24,714 + 1,038 advanced team-game rows; ~250k drives | one-off | none | no 2026 rows |
| Game logs (player) | **UNAVAILABLE** | — | — | — | — | |
| Historical opponents / results | **PARTIAL** | `games.json.gz` 2014–2025 (11,362 games, all `completed`, line scores, pregame/postgame Elo, win prob, attendance, venue); 2026: 888 FBS schedule rows (8 completed @09-02), `football_state` 3,679 all-division (126 completed @09-03), `schedule_state` 250 ESPN finals (@09-17), research settlements 96 final games | 2014–2026 | one-off / hibernated | `test_result_provider.py`, `test_research_settlement.py` | 2026 results stop mid-September |
| Opponent adjustments | **RESEARCH** | `modeling/ratings.py:271 fit_fbs_efficiency_ratings` (tested, on main); `research/v2/state.py` (branch only) → stored states in `dataset.parquet` | 2015–2026 per-game pregame states (10,393 rows) | none | `test_modeling_ratings_and_priors.py` (33 tests) | see §6 |
| Schedule strength | **RESEARCH** (derivable, not computed) | SP+ `sos` field present but **null** in cache; pregame Elo on every game; dataset `h_pre_prev_margin_strength` | — | — | — | nothing computes SOS |
| Recent-form windows | **RESEARCH** | v2 state uses `season_decay=0.5` recency; `execution/context_sources.py` derives recent results/rest from ESPN scoreboard at run time into gitignored `data/execution/` | not stored | — | `test_execution_context.py` | no stored L3/L5 |
| Usage | **UNAVAILABLE** | returning_production `usage` is a team-level share of prior-season production, not usage | — | — | — | |
| Lineups / depth charts | **UNAVAILABLE** | `WEEK1_FOOTBALL_INPUT_AUDIT.md`: "`starting_qb` and `depth_chart` appear in zero source files" | — | — | — | |
| Injuries / availability | **RESEARCH** | `research-sidecar:…/espn_injuries/2026.jsonl` 9,510 rows (276 non-empty; items are ESPN `$ref` URLs, not parsed) | 253 games, 2026-09-02→09-17 | hibernated | none | |
| Matchup metrics | **RESEARCH** | v2 `diff_*`/`sum_*` features (frozen artifact, `modeling/v2/research_features.py` sha-pinned) | 2026 frozen slate only | none | `test_v2_shadow.py` | not stored per matchup on main |
| Projection distributions | **RESEARCH** | `control_predictions.csv` (3,675 rows 2021–25, margin_sd/total_sd/corr), `preds/*.parquet` (6,266 rows, pred_margin/total/p_home/margin_sd), backtest p05/p95 code | historical only | none | `test_modeling_score_model.py` | no samples stored |
| Raw projections | **RESEARCH** | `observation.model_probability` on 42,838 obs rows (versions 0.4.0 / 0.5.0); shadow/v2_shadow ledgers | 156 games 2026 wk1–4 | hibernated | many | retired model |
| Market prices (current) | **VERIFIED** | `data/live/games/*.json` via `scripts/build_kalshi_cfb_catalog.py`, `catalog/*` | 261 games / 13,922 contracts / 19 families; FBS 115, FCS 101 | 30 min slate window, 6 h otherwise | 31 test files (`test_catalog_*`, `test_app_contract_v1.py`) | yes/no bid/ask+sizes, last, volume, OI, fee mechanics, rules |
| Market price history | **PARTIAL** | (a) `research-data` observations: 44,019 rows, 7,344 tickers, 9 checkpoints; (b) catalog git history: 69 snapshots, 3.6 GB raw blobs | (a) 2026-08-26→09-16; (b) 2026-09-18→now | (a) hibernated; (b) change-detected | (a) `test_research_*`; (b) none for history | see §7 |
| Advanced stats | **PARTIAL** | CFBD `advanced_*` (ppa, successRate, explosiveness, lineYards, secondLevel, openField, powerSuccess, stuffRate, standard/passing downs, rush/pass) + garbage-time-excluded variant; `season_advanced` adds havoc, fieldPosition, pointsPerOpportunity | 2014–2025 | one-off | none | |
| Situational splits | **PARTIAL** | stored dims: home/away, neutral, conference game, regular/postseason, garbage-time excluded, rush/pass, standard/passing downs, FBS-vs-FCS, dome | 2014–2025 | one-off | — | see §7 |
| Player props | **PARTIAL** (inventory only) | catalog families `touchdown_scorer` 81, `team_stat_prop` 166, `game_stat_prop` 14 | current slate | 30 min | catalog tests | no player data to research them |
| Team props | **VERIFIED** (inventory) | `team_total` 1,256, `first_half_team_total` 238 | current slate | 30 min | catalog tests | |
| Game-level markets | **VERIFIED** (inventory) | spread 2,449, total 1,768, moneyline 522, halves, quarters, overtime 30 | current slate | 30 min | catalog tests | |
| Play-by-play | **UNAVAILABLE** (drives: PARTIAL) | manifest `not_fetched['/plays']` "too expensive"; drives 2014–2025 | — | — | — | |
| Weather | **RESEARCH** | sidecar Open-Meteo forecasts 5,103 rows/254 games (lead_hours recorded); `research-data-v2enrich` 2025 archive/hforecast/prevrun; `v2_games_for_weather.csv` coords for 10,391 games | 2025 (650–736 games), 2026-09-02→09-17 | hibernated | none | |
| Venue / park effects | **PARTIAL** | `teams_fbs` location (lat/long/dome/elevation/capacity/timezone) ×13 seasons; `venues.json.gz` (2025); dataset `venue_elev_m`,`venue_dome`,`home_travel_km` | 2014–2026 | one-off | none | no effect estimates |
| Calibration data | **RESEARCH** | attributions: 36,798 rows with model prob + entry price + outcome; `analytics/calibration_report.py`; `v2/eval/dist_eval_*.json` | 102 games 2026 wk1–3; 2017–2025 backtest | hibernated | `test_analytics_calibration.py` | |
| Historical accuracy / postmortems | **RESEARCH** | `v2/eval/market_benchmark_final.json` (v2 MAE 12.55 vs market 12.12 → "degrades"); `leaderboard_round1-5.csv`; `reports/weekly/2026-wk01.json`; `scripts/cfb_postmortem.py` (ledger only) | 2021–2025, 2026 wk1 | none | `test_postmortem.py` | |
| CLV | **PARTIAL** | model-era: attributions `closing.*` (36,294 closing-captured rows), `research/clv.py` side-aware; owner wagers: NOT computed; 33/101 wager tickers in obs ledger, 83/101 seen in sampled catalog history, 7/101 in current catalog | — | none | `test_research_clv.py`, `test_analytics_metrics.py` | catalog is not a near-kickoff capture |
| Historical wager outcomes | **VERIFIED** | `accounting-data` (104 wagers: 69 YES/35 NO, 101 tickers; 102 settlements: 61 WON/41 LOST; 93 amendments) → `app/latest/wagers.json`, `settlements.json`, `performance.json` | 2026-09-11 → 2026-10-03 | router-driven PRs + app export 30 min | 20 test files | |
| Identity tables | **PARTIAL** | registry 138 slugs/74 aliases (vendor ids empty); `teams_fbs.json.gz` CFBD ids/abbrev/alt names ×13 seasons; no Kalshi map | — | — | `test_teams_registry.py`, `test_team_name_variants.py` | |
| Schedules | **PARTIAL** | catalog kickoffs (2026-10-02→10-17 live); CFBD 2026 schedule 888 FBS (as of 09-02); football_state 3,679 all-division; schedule_state ESPN | 2026 | catalog 30 min; rest hibernated | `test_capture_resilience.py` | |
| Seasons covered | — | CFBD 2014–2025 full; 2026 preseason + Kalshi markets Aug 26 → now | | | | |

---

## 5. Detailed findings per category

### 5.1 CFBD research cache (`research-data:data/research_cache/v2/`)
Manifest (`manifest.json`): `source=https://api.collegefootballdata.com`, `fetched_at=2026-09-02T05:17:45Z`, `cache_version=v2_research_cache_v1`, 422 metered calls (410 ok, 12 HTTP 400 — the per-season `/games/teams` without week param), quota 768→358 of 1,000 (Free tier). `not_fetched`: `/plays` (too expensive), `/ratings/elo` (redundant), `/roster` (retroactively revised), **"2026 per-game postgame endpoints: FIREWALLED"**, injuries (no source), weather (no forecast archive). Writer: `scripts/fetch_v2_research_cache.py` exists only on `claude/cfb-model-v2-research-krupoc`; the bot commit says `preseason-research-fetch.yml (mode=v2)` but main's workflow has no such mode.

Row counts per endpoint per season (measured by loading every gz):

| file | 2014 | 2015 | 2016 | 2017 | 2018 | 2019 | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 | 2026 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| games | 868 | 870 | 873 | 874 | 884 | 888 | 570 | 887 | 896 | 910 | 920 | 934 | 888 (8 completed) |
| games_teams (box) | 867 | 870 | 872 | 874 | 884 | 888 | 568 | 887 | 896 | 910 | 919 | 934 | — |
| advanced_regular | 1620 | 1644 | 1632 | 1658 | 1684 | 1694 | 1084 | 1698 | 2822 | 2850 | 3112 | 3216 | — |
| advanced_regular_nogarbage | 1618 | … | … | … | … | … | … | … | 2822 | 2850 | 3112 | 3216 | — |
| advanced_postseason | 78 | 82 | 82 | 80 | 78 | 80 | 52 | 76 | 96 | 134 | 100 | 100 | — |
| drives_regular | 21436 | 21758 | 21436 | 21439 | 21734 | 21412 | 13506 | 20713 | 20874 | 20850 | 20673 | 20666 | — |
| drives_postseason | 1055 | 1097 | 1079 | 1080 | 983 | 980 | 640 | 898 | 1043 | 1016 | 1093 | 1096 | — |
| lines_regular | 829 | 829 | 832 | 834 | 845 | 848 | 542 | 849 | 1417 | 1350 | 1523 | 1547 | — |
| lines_postseason | 39 | 41 | 41 | 40 | 39 | 40 | 26 | 38 | 46 | 66 | 50 | 50 | — |
| ratings_sp | 129 | 129 | 129 | 129 | 131 | 131 | 131 | 131 | 132 | 134 | 135 | 137 | — |
| season_advanced | 128 | 128 | 128 | 130 | 130 | 130 | 128 | 130 | 131 | 133 | 134 | 136 | — |
| teams_fbs | 128 | 128 | 128 | 130 | 130 | 130 | 128 | 130 | 131 | 133 | 134 | 136 | 138 |
| talent | 0 | 232 | 237 | 157 | 236 | 231 | 219 | 224 | 233 | 238 | 134 | 134 | 138 |
| recruiting_teams | 231 | 231 | 238 | 233 | 229 | 223 | 206 | 191 | 184 | 177 | 194 | 232 | 221 |
| returning_production | 125 | 128 | 128 | 128 | 130 | 130 | 130 | 128 | 130 | 131 | 133 | 134 | 136 |
| coaches | 132 | 132 | 134 | 139 | 139 | 134 | 137 | 152 | 147 | 143 | 152 | 161 | 138 |
| rankings (weeks) | 16 | 15 | 15 | 15 | 15 | 16 | 15 | 15 | 15 | 15 | 16 | 16 | 1 |
| portal | — | — | — | — | — | — | — | 1770 | 2273 | 2502 | 3378 | 4499 | 4470 |
| venues | — | … | — | — | — | — | — | — | — | — | — | (2025 only) | — |

Schemas (sample 2025):
- `games`: `id, season, week, seasonType, startDate, startTimeTBD, neutralSite, conferenceGame, attendance, venueId, venue, homeId, homeTeam, homeClassification, homeConference, homePoints, homeLineScores[4], homePostgameWinProbability, homePregameElo, homePostgameElo, (away…), excitementIndex, completed, notes, playoff`. 2025: 934 games, 934 completed, 1 missing Elo, 126 FBS-vs-FCS.
- `games_teams`: `{id, teams:[{teamId, team, conference, homeAway, points, stats:[{category, stat}]}]}` — 35 categories: completionAttempts, defensiveTDs, firstDowns, fourthDownEff, fumblesLost/Recovered, interceptions(+TDs,Yards), kickReturns(+TDs,Yards), kickingPoints, netPassingYards, passesDeflected/Intercepted, passingTDs, possessionTime, puntReturns(+TDs,Yards), qbHurries, rushingAttempts/TDs/Yards, sacks, tackles, tacklesForLoss, thirdDownEff, totalFumbles, totalPenaltiesYards, totalYards, turnovers, yardsPerPass, yardsPerRushAttempt. Stats are strings.
- `advanced_*`: `{gameId, season, week, seasonType, team, opponent, offense:{plays, drives, ppa, totalPPA, successRate, explosiveness, powerSuccess, stuffRate, lineYards(+Total), secondLevelYards(+Total), openFieldYards(+Total), standardDowns{ppa,successRate,explosiveness}, passingDowns{…}, rushingPlays{…,totalPPA}, passingPlays{…}}, defense:{same}}`.
- `drives_*`: `{id, gameId, offense, defense, driveNumber, isHomeOffense, startPeriod/Time/Yardline/YardsToGoal, end…, elapsed, plays, yards, driveResult, scoring}`.
- `lines_*`: `{id, season, week, seasonType, startDate, homeTeam/Id/Conference/Classification/Score, away…, lines:[{provider, spread, spreadOpen, formattedSpread, overUnder, overUnderOpen, homeMoneyline, awayMoneyline}]}`; providers 2025: ESPN Bet 1,542, Bovada 887, DraftKings 759(+14 "Draft Kings"); 1,547/1,547 games have ≥1 line.
- `ratings_sp`: `{team, year, conference, rating, ranking, offense{rating,ranking,…nulls}, defense{…}, specialTeams{rating}, sos: null, secondOrderWins: null}` — component sub-metrics and SOS are null in the sample.
- `season_advanced`: offense/defense with `havoc{total,frontSeven,db}`, `fieldPosition{averageStart,averagePredictedPoints}`, `pointsPerOpportunity`, `totalOpportunies` plus the per-game advanced fields.
- `teams_fbs`: `{id, school, mascot, abbreviation, alternateNames[], conference, division, classification, color, alternateColor, logos[], twitter, location{id,name,city,state,zip,countryCode,timezone,latitude,longitude,elevation,capacity,constructionYear,grass,dome}}`.
- `rankings`: `{season, seasonType, week, polls:[{poll, ranks:[{rank, school, teamId, conference, firstPlaceVotes, points}]}]}` — polls: Coaches, AP Top 25, FCS Coaches, D-II, D-III, Playoff Committee (6 weeks).
- `returning_production`: `{season, team, conference, totalPPA, totalPassingPPA, totalReceivingPPA, totalRushingPPA, percentPPA, percentPassingPPA, percentReceivingPPA, percentRushingPPA, usage, passingUsage, receivingUsage, rushingUsage}`.
- `talent {team, year, talent}`, `recruiting_teams {team, year, rank, points}`, `coaches {coach, school, year, hireDate}`, `portal {firstName, lastName, position, origin, destination, transferDate, rating, stars, eligibility, season}`.

Missingness measured: 2025 games 0% missing venueId, 0.1% missing Elo; dataset-level (below) `home_pregame_elo` 12.7% null, `away_pregame_elo` 17.9% null (FCS sides).

### 5.2 Preseason cache (`research_cache/preseason/`)
Manifest: `fetched_at=2026-08-28T15:11:36Z`, `cache_version=preseason_research_cache_v1`, endpoints `/games` (3,676 all-division 2026 rows, scores for week-0/1 games), `/coaches` 138, `/player/returning` 136, `/talent` 138, all `USABLE`. Seasons 2019, 2021–2026 (0.7–1.5 MB each). Writer `scripts/fetch_preseason_research_cache.py` + `preseason-research-fetch.yml` (manual). Readers on main: `research/result_provider.py:142`, `research_scan_and_capture.py:221,390`, `research/preseason/shadow_spec.py`. Tests: `test_preseason_experiments.py`, `test_result_provider.py`, `test_prospective_shadow.py`.

### 5.3 V2 feature dataset (`research-data:data/research/v2/`)
`dataset.parquet.meta.json`: `dataset_version=v2_dataset_v1`, `built_at=2026-09-02T05:34:26Z`, `n_games=10393`, `n_features=231`, `state_config={season_decay:0.5, lam:6.0, fcs_lam:2.0}`, 26 `state_metrics` (pts_for, margin, o_ppa, o_sr, o_expl, o_pass_ppa, o_rush_ppa, o_pass_sr, o_rush_sr, o_sd_sr, o_pd_sr, o_line_yds, o_plays, o_ppa_ng, o_sr_ng, dr_pts_per_drive, dr_ppo, dr_to_rate, dr_3out_rate, dr_start_ytg, dr_sec_per_play, b_third_rate, b_pen_yds, b_havoc_def, b_sack_rate_def, b_takeaways).
Columns (261): ids (`game_id, season, week, season_type, kickoff, home, away, home_id_cfbd, away_id_cfbd, home_class, away_class, home_conf, away_conf, conference_game, neutral, venue_id, venue`), targets (`home_points, away_points, completed, margin, total, home_won, both_fbs, postseason`), per metric `h_o_<m>, a_o_<m>, h_d_<m>, a_d_<m>, mu_<m>, hfa_<m>` (home/away offense & defense state, league mean, HFA), preseason `h_pre_*`/`a_pre_*` (talent, recruit_0..3/avg4, ret_* returning production, coach_change, coach_tenure, prev_games/pf/pa/win_pct/margin_strength, prev2_margin_strength, sp_prev_rating/off/def, poll_pre_ap/coaches, fbs_new, conference), context (`home_rest_days, away_rest_days, home_games_so_far, away_games_so_far, home_travel_km, away_travel_km, venue_elev_m, venue_dome, kick_hour_utc, home_fcs_opp, away_fcs_opp`), market (`mkt_spread_margin, mkt_spread_open_margin, mkt_total, mkt_p_home, mkt_n_books`), `home_slug, away_slug`.
Rows/season: 2015 870 … 2025 934, 2026 888 (all `completed=False`, NaN targets). `both_fbs` 9,086 / 1,307. Missingness: `mkt_p_home` 62.2%, `mkt_spread_open_margin` 58.2%, `a_pre_recruit_3` 27.5%, `away_pregame_elo` 17.9%, `home_pregame_elo` 12.7%, `margin` 8.5% (2026), state columns 0%.
Variants: `dataset_d020/d025/d025w90/d035/d035w90` (decay sweeps), `dataset_d070`, `dataset_long` meta only. `registry.jsonl` 92 experiment rows (candidate spec, folds 2017–2025, per-season MAE/RMSE/log-loss/Brier). `preds/*.parquet` 8 finalist prediction sets × 6,266 rows (`game_id, season, week, pred_margin, pred_total, p_home, margin_sd`) incl. `market_close.parquet`. `control_predictions.csv` 3,675 rows 2021–2025 (`ctrl_margin, ctrl_total, ctrl_p_home_sim/closed, talent_delta, v050_*, home_sd, away_sd, corr, margin_sd, total_sd, history_rows`).
`eval/market_benchmark_final.json`: n=3,771 common games 2021–2025; v2 margin MAE 12.545 vs market 12.115 (delta +0.43, CI [0.30,0.56], verdict **degrades**); totals 12.887 vs 12.609 (degrades); winner log-loss 0.562 vs 0.548; ATS at ≥2/3/5-pt disagreement 50.2% / 50.5% / 49.4%. This is the quantitative basis for the retirement.
Builder code: `claude/cfb-model-v2-research-krupoc:src/cfb_edge_finder/research/v2/{state.py,features.py,dataset…}`, `scripts/v2_build_dataset.py` — **not on main**. Main holds only a sha256-pinned verbatim copy of `features.py` (`modeling/v2/research_features.py`) and the frozen-artifact loader (`modeling/v2/artifact.py`).

### 5.4 Prospective observation ledger (`research-data:data/research/observations/2026/`)
44,019 rows, 20 UTC-date shards, 2026-08-26T06:58Z → 2026-09-16T23:30Z, 297 capture sweeps. Schema `research_corpus_v1` (`schemas/corpus_row.py`): envelope `{observation_key, run_id, season, capture_mode=PROSPECTIVE, capture_window_version, schema_version, game_status_at_capture, kickoff_utc_at_capture, schedule_source_timestamp, data_versions{model_version, feature_version, mapping_version, fee_schedule_version, …}}` + `observation` (`KalshiResearchObservation`): `game_id, kalshi_event_ticker, kalshi_market_ticker, family (moneyline 1,956 / spread 23,630 / total 17,252; 2.7% null = unresolved), threshold (7.1% null), team (41.9% null), side (60.8% null), semantic_operator, executable_yes_price, executable_no_price, market_midpoint (0% null), market_status (active 42,295), estimated_taker_fee, fee_schedule_version, model_probability, gross/research/fee_adjusted gap, coverage_outcome (evaluated 42,838 / ticker_unresolved 1,181), pricing_status, parse_status, snapshot_id, snapshot_timing{label, hours_before_kickoff 0.15–592.5}, training_cutoff, uncertainty{data_completeness, early_season_prior_weight, qb_status_confirmed, notes}, provenance{…}, model_version{model_version, pricing_engine_version, ratings_component_version}`.
Distinct: 7,344 market tickers, 466 event tickers, 156 canonical games (wk01 17,112 rows, wk02 20,861, wk03 6,042, wk04 4). Obs per ticker: min 2, median 8, p90 9, max 9 (one per label). Labels: EARLY_OPEN 7,344, T_3D 6,804, T_24H 4,743, T_6H 4,387, T_30 4,287, CLOSING 4,283, T_90 4,235, T_60 4,235, T_7D 3,701. **92/156 games have a CLOSING row**; 84 games have all 9 labels; 55 games have only EARLY_OPEN/T_3D/T_7D (collection died 2026-09-16 before their kickoffs). Model versions: 0.5.0-early-season-talent-prior 39,862; 0.4.0 2,976. Families are only the three the retired mapper supported (the catalog now covers 19).
Writer: `scripts/research_scan_and_capture.py` via `research-capture.yml` (`*/10`, hibernated), persistence `research/persistence.py` + `research/shards.py` (45 MB rollover; append-only proven by `test_research_persistence.py::test_rows_are_immutable_on_disk_no_line_ever_rewritten`), durable push `research/git_durable_store.py` (`test_research_git_sync.py`).

### 5.5 Research settlements & attributions
- `settlements/2026/`: 11,509 rows (`settlement_v1`): 4,552 `settled` (derived yes 1,923 / no 2,629) + 6,957 `pending_not_final`; 7,205 distinct (game,ticker); 153 games, 96 final; `official_kalshi_settlement` null for all 11,509 (cross-check never populated); 0 mismatches flagged. Result source CFBD `/games` via `research/settlement.extract_game_result`; fallback ESPN host 403 (documented gap).
- `attributions/2026/`: 37,979 rows (`attribution_v1`), one per observation: state SETTLED_NO 21,169 / SETTLED_YES 15,629 / NOT_APPLICABLE 1,181; `closing.closing_status` CLOSING_CAPTURED 36,294 / MISSING_NO_SCAN_IN_WINDOW 1,685; `yes_economics`/`no_economics` research-unit P&L (fee-adjusted) on 36,798 rows; 102 games. Semantics: spread/total strict `>` on persisted threshold (all half-points), winner on final score (`docs/SETTLEMENT.md`).
- `reports/weekly/2026-wk01.json` (3 KB) is the only report artifact.

### 5.6 Shadow ledgers
- `shadow/2026/`: 41,663 rows (`shadow_observation_v1`): talent-prior shadow vs control per observation (`shadow_probability, control_probability, talent_home/away/differential, beta=0.018993`).
- `v2_shadow/2026/`: 37,449 rows (`v2_shadow_observation_v1`): `v2_probability, v2_pred_margin/total, v2_sd_*` — sample row has `unavailable_reason: "game … not in the frozen V2 slate"`; non-null share not measured (UNKNOWN). Artifact `v2_shadow/2026.artifact.json` (329 KB, spec `cfb-v2-candidate-2026-09-02`).

### 5.7 Live sidecar (`research-sidecar` branch)
`scripts/live_info_sidecar.py` (keyless, ESPN core API + Open-Meteo), workflow `live-info-sidecar.yml` hibernated. 57 commits 2026-09-02 23:36 → 2026-09-17 21:45. Rows carry `fetched_at, source URL, http_status, payload_sha256, parsed, game_id (ESPN/CFBD), kickoff_utc, lead_hours`.
- `weather_forecast/2026.jsonl`: 5,103 rows, 254 games, all HTTP 200; parsed `temperature_2m, relative_humidity_2m, precipitation(+probability), rain, snowfall, wind_speed_10m, wind_gusts_10m(+max3h), wind_direction_10m, cloud_cover, precipitation_sum3h`, `venue_id`; 10–31 captures per game (forecast lead series).
- `espn_odds/2026.jsonl`: 5,103 rows; books DraftKings only (`spread, over_under, home_ml, away_ml, home_favorite, details`).
- `espn_injuries/2026.jsonl`: 9,510 rows (per team per capture), 276 with `count>0`, items are `$ref` URLs (athlete/injury ids) not resolved.
Games file: `data/research_inputs/v2_games_for_weather.csv` on main (10,391 games 2015+, venue lat/long/dome).

### 5.8 Live catalog (main)
`cfb_catalog_status.json` @2026-10-03T02:42Z: 261 physical games, 1,539 events, 13,922 markets, 36 unknown-family, 504 requests, 120.6 s, `HEALTHY`, `capture_complete=true`, 238 game files written / 23 unchanged / 1 pruned, `game_bytes_total` 58.8 MB.
Index `games[]`: `{game_key, title, kickoff, identity{milestone_id, division, tier, season{year,type,week}, conference, league, …}, events[], family_distribution, market_count, markets_file, completeness{…}}`. Divisions FBS 115 / FCS 101 / null 45; tiers A 28, A- 24, B 12, C 47, D 103, null 47; weeks 5 (112), 6 (104), null 45; kickoffs 2026-10-02 → 2026-10-17.
Market record (51 fields): `market_ticker, event_ticker, series_ticker, title, yes_sub_title, no_sub_title, family, period, classification_confidence/rationale, market_type, strike_type, floor_strike, cap_strike, functional_strike, custom_strike{football_team UUID}, is_alternate_line, status, open_time, close_time, expiration_time, expected_expiration_time, occurrence_datetime, yes_bid/ask (+_size), no_bid/ask (+_size), last_price, previous_price, volume, volume_24h, open_interest, liquidity_dollars, notional_value, fee{model, formula, …}, fee_multiplier/type, mechanics, rules_primary/secondary, settlement_sources, settlement_timer_seconds, can_close_early, early_close_condition, updated_time, exchange_index`. Prices in dollars 0–1. Post-settlement 0/1 sentinel books flagged.
Families (13,922): game_spread 2,449; game_total 1,768; quarter_total 1,496; quarter_spread 1,446; team_total 1,256; first_half_spread 1,167; first_half_moneyline 783; first_half_total 763; second_half_spread 722; game_moneyline 522; second_half_total 475; quarter_moneyline 408; first_half_team_total 238; team_stat_prop 166; second_half_moneyline 102; touchdown_scorer 81; unknown 36; overtime 30; game_stat_prop 14.
Tests: `tests/test_catalog_{classification,discovery,fees,pagination,production_wiring,quote_liquidity,schema_and_isolation}.py`, `test_run_cfb_consumer_contract.py`, `test_app_contract_v1.py`; CI (`ci.yml`) runs `ruff` + `pytest -v` on push/PR (2,799 test functions in `tests/`).

### 5.9 Accounting ledger (`accounting-data`)
`wagers/2026.jsonl` (104 rows, `cfb_accounted_wager.v1`): `wager_id, source_bet_key, venue=kalshi, market_ticker, side (YES 69/NO 35), contracts, execution_price, stake, fees_paid, fees_are_estimated, executed_at (2026-09-11T23:22Z → 2026-10-03T03:18Z), game_date, week (null), season, entry_method=IMPORTED_RECEIPT, import_batch_id, result/settlement_status/gross_return/net_profit_loss (null on wager rows), notes`. 101 distinct tickers.
`settlements/2026.jsonl` (102, `cfb_wager_settlement.v1`): `settlement_id, source_bet_key, market_ticker, side, result (WON 61 / LOST 41), settlement_status=SETTLED, gross_return, net_profit_loss, settled_at, refusals[]`.
`settlement_amendments/2026.jsonl` (93, `cfb_settlement_amendment.v1`): economics v1→v2 restatement (`net = gross − stake`, stake already includes entry fee), `evidence{…}`, `supersedes_economics_version`.
Writers: `scripts/import_routed_wagers.py` / `import_routed_settlements.py` / `amend_settlement_economics.py` via router PRs from `kalshi-router/CFB`, `kalshi-router/settle-CFB`, `kalshi-router/backfill-CFB`, `ledger/economics-v2-backfill`. Validation `accounting/wager.py::validate` refuses any model-implying field; `test_accounting_isolation.py` forbids predictive packages from importing it. Exported to `app/latest` (`wagers` 102, `settlements` 97 after refusal filtering, `performance.by_market_family`).
Join to games: ticker → `parse_event_ticker` → `game_key`; **only 7/101 wager tickers are in the current catalog** (games pruned after kickoff), so `event_id` is null for most exported wagers.

### 5.10 Retired model inputs — still ingested?
Inputs were: CFBD `/games` + `/stats/game/advanced` (plays) for 4 trailing seasons (`football_state.history`), preseason talent/returning/coaches cache, CFBD schedule, Kalshi observations. **None is ingested on a schedule today**: `research-capture.yml` hibernated 2026-09-17, external cron-job.org scheduler disabled, `cfbd_access/state.json` = `CFBD_QUOTA_EXHAUSTED` (1000/1000 used, resets 2026-10-01), `operational_state` = `DEADLINE_AT_RISK` since 2026-09-25. What is stored: everything listed in §5.1–5.6, frozen at the dates given. Nothing on main's live path reads any of it (`test_catalog_schema_and_isolation.py` asserts the catalog imports nothing from modeling/research).

---

## 6. Opponent-adjustment audit

**A. Production-era (retired) — `src/cfb_edge_finder/modeling/ratings.py` (on main, tested)**
- Formula (`ratings.py:1-30`, solve at `:402`): `points_per_play(team, game) ≈ mu + offense[team] − defense[opponent] + hfa·home_indicator`, fit as a simultaneous ridge least-squares over two rows per game (one per side, `modeling/corpus.py` `TeamGameLine`), `beta = solve(XᵀX + λI_params, Xᵀy)` with no penalty on `mu`/`hfa`.
- Baseline: league average = 0.0 for offense/defense; `__league_average__` pace key (`ratings.py:423-471`).
- Regularisation: `DEFAULT_RIDGE_LAMBDA=10.0` (FBS), `DEFAULT_FCS_RIDGE_LAMBDA=4.0` (one pooled FCS pseudo-team, or 3 tiers with `fcs_mode="tiered"`), `DEFAULT_PACE_SHRINKAGE_K=4.0`; pace estimated separately and recombined multiplicatively (`pace_mode="matchup"` default).
- Sample requirements: none explicit — ridge shrinks thin teams toward 0; season carryover `priors.py:54 season_carryover_weight = g/(g+4)` ⇒ Week 1 is 100% prior-season rating (documented in `docs/WEEK1_FOOTBALL_INPUT_AUDIT.md`).
- Recursive vs simple: single simultaneous linear solve (not iterative); walk-forward refits per as-of in backtests (`backtest.py`, `score_model.build_expanding_residual_pool`).
- Leakage: every row asserted strictly before `as_of` (`fit_fbs_efficiency_ratings(lines, as_of, …)`).
- History: ratings snapshots are **not persisted** anywhere; only their downstream `model_probability` lives in the observation ledger (`ratings_component_version` string records the config).
- Tests: `tests/test_modeling_ratings_and_priors.py` (33 tests: sign/HFA/neutral-site exclusion/FCS pooling and tiering/determinism/leakage/pace shrinkage/carryover), `test_modeling_backtest.py`, `test_modeling_leakage.py`, `test_modeling_score_model.py`.
- Limitations: points-per-play only (no efficiency sub-metrics); FBS-only parameters; no 2026 in-season data available to refit.

**B. V2 research — `research/v2/state.py` (branch `claude/cfb-model-v2-research-krupoc` only)**
- Formula (docstring): `m_off(T,g) = mu_m + off_m[T] − def_m[O] + hfa_m·home_ind + e` for 26 metrics at once, shared design matrix (rows = team-games, cols = [mu, hfa, off_1..k, def_1..k]), weighted ridge normal equations, recency weight `season_decay=0.5` per season back (`week_decay=1.0`), `lam=6.0`, `fcs_lam=2.0`, FCS pooled to `__fcs__`; higher `def` = better defense.
- Stored history: **yes** — the pregame state for every game is materialised as `h_o_*, a_o_*, h_d_*, a_d_*, mu_*, hfa_*` columns in `dataset.parquet` (10,393 games 2015–2026), so per-team-per-week opponent-adjusted trajectories can be read back without refitting.
- Tests: on the research branch only (not audited); main has none for `state.py`. Status RESEARCH.

---

## 7. Inventories

### 7.1 Time-series inventory
| Dataset | x-axis | Keys | Rows | Links to game id? | Opponent id? |
|---|---|---|---|---|---|
| `advanced_regular/_postseason` (CFBD) | game (gameId, season, week) | team name, opponent name | 24,714 + 1,038 | yes (CFBD gameId) | yes (name) |
| `games_teams` box | game | teamId/team, homeAway | 10,676 games × 2 | yes | via other team row |
| `drives_*` | drive within game | gameId, offense/defense | ~250k | yes | yes |
| `games` (scores, Elo) | game | homeId/awayId | 11,362 (+888 2026) | yes | yes |
| `lines_*` | game (open vs close) | game id, provider | 12,245 + 516 | yes | yes |
| `rankings` | week | school/teamId, poll | ~190 week-rows | no | no |
| `ratings_sp`, `season_advanced`, `talent`, `recruiting`, `returning_production`, `coaches` | season | team | 128–238/season | no | no |
| `dataset.parquet` state cols | game (pregame as-of) | game_id, home/away | 10,393 | yes | yes |
| observations ledger | capture time / timing label | kalshi_market_ticker, game_id | 44,019 | yes (canonical) | implicit via game |
| attributions | per observation | observation_key | 37,979 | yes | — |
| sidecar weather/odds/injuries | fetched_at (lead_hours) | ESPN game id, espn_team_id | 5,103 / 5,103 / 9,510 | yes | — |
| catalog git history | commit `captured_at` | market_ticker, game_key | 69 snapshots × ~13–15k markets | yes (game_key) | — |
| accounting | executed_at / settled_at | source_bet_key, market_ticker | 104 / 102 | via ticker | — |

### 7.2 Splits inventory (dimensions actually stored)
- Home / away / neutral: `games.neutralSite`, `games_teams.homeAway`, `drives.isHomeOffense`, dataset `neutral` (all games 2014–2026).
- Regular vs postseason: separate files; `seasonType`.
- Conference game: `games.conferenceGame`; conferences on every row.
- FBS vs FCS opponent: `homeClassification/awayClassification`; dataset `home_fcs_opp/away_fcs_opp`, `both_fbs` (9,086/1,307).
- Garbage time excluded: `advanced_regular_nogarbage` (3,216 rows/2025 vs 3,216 incl.).
- Down/play type: `standardDowns`, `passingDowns`, `rushingPlays`, `passingPlays` sub-objects in every advanced row.
- Half/quarter scoring: `homeLineScores[4]`/`awayLineScores[4]` on every game.
- Venue: dome, elevation, surface (`grass`), timezone in `teams_fbs.location`; `venue_dome`, `venue_elev_m` in dataset.
- Rest/travel: dataset `home_rest_days`, `away_rest_days` (15.1% null away), `home_travel_km`, `away_travel_km`.
- Period markets: catalog `period` label (game/first_half/second_half/quarter/overtime).
- Not stored: handedness, game state (score situation), strength state, surface splits beyond `grass` flag.

### 7.3 Market-history inventory
- **Research observation ledger** (`research-data`): per-ticker checkpoint series, 9 labels, 7,344 tickers, 2026-08-26 → 09-16, median 8 obs/ticker; granularity = checkpoint window (EARLY_OPEN, T_7D, T_3D, T_24H, T_6H, T_90, T_60, T_30, CLOSING 0–14 min); retention: permanent append-only; size 117 MB JSONL (2.66 KB/row; compact form ~100 B/row ≈ 4.4 MB). Only moneyline/spread/total families. Frozen.
- **Catalog git history** (`main`): 69 commits (first 2026-09-18 00:23Z `0ddf764`, latest 2026-10-03 02:44Z), per day 1–8 (8,7,6,3,4,3,4,5,6,7,2,2,3,3,5,1), i.e. every 30 min in-window only when the fingerprint changed; `games/` tree per commit 14.0–77.9 MB, **3.60 GB total blob bytes across history**; each market row carries full book + `capture.captured_at` per file. Sampling every 4th commit (18 of 69) yielded 44,003 distinct tickers and a per-ticker distinct-quote-state distribution of median 4 / p90 6 / max 16 — so a full reconstruction would give roughly 4–6 price points per ticker over its ~2-week listing life. Reconstruction = `git ls-tree` + `git show` of ~16.5k blobs (minutes); output compact table ≈ 44k tickers × ~5 states × ~80 B ≈ **18–25 MB** (per-ticker lazily ≈ 0.5 KB). Caveats: not a near-kickoff capture (no CLOSING guarantee), post-settlement sentinel books, games pruned after kickoff so a ticker's last snapshot may precede kickoff by hours. Retention: git history on main (permanent unless rewritten).
- **CFBD lines** (`lines_*`): open and close spread/total/ML per book per game 2014–2025 (two points per book, not a series).
- **ESPN odds sidecar**: 5,103 DraftKings quotes, 254 games, 10–31 per game (Sep 2–17 2026).
- **Owner CLV feasibility**: 33/101 wager tickers overlap the observation ledger; 83/101 found in the 18-commit sample of catalog history (likely >90% with a full scan); 7/101 in the current catalog. Entry price/fees are in the ledger; closing side must be reconstructed from the catalog snapshot nearest each wager's `close_time` (defined in `docs/MODEL_RETIREMENT_2026.md` "If CLV research is reintroduced").

### 7.4 Projection inventory
- Per market, per checkpoint: `observation.model_probability` (point) on 42,838 rows, two model versions; uncertainty metadata only (`data_completeness`, `early_season_prior_weight`), no distribution stored.
- Per game: `control_predictions.csv` 3,675 rows 2021–2025 with `margin_sd/total_sd/corr` (moment-matched from bootstrap sims — samples not stored); `preds/*.parquet` 6,266 rows × 8 candidates (point + `margin_sd`); `v2_shadow/2026.artifact.json` frozen per-game predictions for the 2026 slate.
- Across runs: shadow ledgers store the control and shadow probabilities per observation (41,663 / 37,449 rows) — i.e. projections ARE stored across runs for Aug 28 → Sep 16 2026.
- Nothing per player.

---

## 8. Existing ranking / percentile / average code
- `modeling/ratings.py:423-471` — league-average pace (`__league_average__`) and shrinkage toward it; offense/defense centred on league mean 0.
- `modeling/backtest.py:343-346` — `np.percentile(margins/totals, 5/95)` per game.
- `analytics/calibration_report.py` — binned calibration, Brier, log-loss, ECE, market comparison; `analytics/uncertainty.py` — game-cluster bootstrap CIs; `analytics/slices.py` — gap/price/timing/family cells.
- `research/paper_card.py` — ranks candidates by `rank` (research view).
- Data-side ranks only: CFBD `ratings_sp.ranking` (+ offense/defense rankings), `recruiting_teams.rank`, poll ranks in `rankings`, `talent`.
- No league-percentile / z-score computation for display exists anywhere (grep `percentile|league_average|zscore|\.rank\b` over `src`/`scripts`).

---

## 9. Size estimates (bytes, for lazy-loading design)
| Surface | Basis | Estimate |
|---|---|---|
| (a) Team profiles (138 FBS) | per team: 12 seasons × (season_advanced ~2 KB + SP+ 0.3 KB + talent/recruit/returning/coach 0.4 KB) + identity/venue 1 KB | ~35 KB/team JSON; **~5 MB total**, ~1.5 MB gzipped. FCS: identity only (683 names). |
| (b) Player profiles | none | **0** (UNAVAILABLE) |
| (c) Per-game detail | historical: box (~3 KB) + advanced both sides (~4 KB) + drives (~25 drives × 0.3 KB = 7.5 KB) + lines (~1 KB) + dataset row (~3 KB) ≈ 18 KB/game → 11,362 games ≈ **200 MB raw**, ~35 MB gz (source gz totals: advanced 9.1 MB, drives 7.9 MB, box 1.8 MB, lines 1.3 MB). Live: catalog game file avg 225 KB (58.8 MB/261); app `event_detail` ~8.8 KB each (2.3 MB/261). |
| (d) Metric time series | per team-season: ~13 games × 26 metrics (+ opponent-adjusted state) ≈ 13 × 0.5 KB = 6.5 KB; 138 teams × 12 seasons ≈ **11 MB** raw (dataset.parquet is 11 MB for all of it). |
| (e) Market history | observation ledger compact (ticker, ts, label, yes_bid/ask, no_bid/ask, mid): 44,019 × ~100 B ≈ **4.4 MB** (117 MB as stored); catalog reconstruction ≈ 44k tickers × ~5 states × 80 B ≈ **18–25 MB**; per ticker ≈ 0.5 KB; sidecar odds/weather 10.3 MB as stored (~2 MB compact). |
| Accounting | 104 + 102 + 93 rows | 252 KB (app export carries it in `wagers.json`/`settlements.json`). |

---

## 10. Recommended research capabilities to expose in this pass

**Expose (evidence supports):**
1. **Market inventory** (VERIFIED) — already in `app/latest`; 19 families, periods, fees, rules.
2. **Market history** (PARTIAL) — reconstruct per-ticker quote states from the 69 catalog commits (one-off script reading git objects; ~20 MB compact; label as "change-detected snapshots, not a closing series"), and optionally surface the frozen research observation ledger's 9-checkpoint series for the 7,344 tickers of 2026 wk1–4 (label "model-era research capture, Aug 26–Sep 16 2026").
3. **Accounting / historical wager outcomes** (VERIFIED) — already exported; add owner CLV only after a near-kickoff capture exists (today's catalog cannot promise one).
4. **Team profiles & season metrics, 2014–2025** (PARTIAL, label "CFBD snapshot 2026-09-02; no 2026 in-season stats") — SP+, season advanced (PPA/SR/explosiveness/havoc), talent, recruiting, returning production, coaches, pregame-Elo trajectories, venue; identity via `teams_fbs` CFBD ids (needs a Kalshi-code map to link to live events — build from `abbreviation`/`alternateNames` + review).
5. **Team game logs & historical opponents/results, 2014–2025** (PARTIAL) — box, per-game advanced, drives, multi-book open/close lines, line scores.
6. **Opponent-adjusted metric time series** (RESEARCH) — read `dataset.parquet` state columns (26 metrics, walk-forward, lam=6/decay=0.5) per team per game; label as research-branch output, builder not on main, no 2026 in-season rows.
7. **Situational splits** limited to the stored dimensions in §7.2.
8. **Weather / odds / injuries 2026 (Sep 2–17) and 2025 weather** (RESEARCH) — only as "captured evidence", hibernated.

**Mark RESEARCH:** projections (retired model, benchmark says it degrades vs market), calibration/CLV analytics, matchup features, shadow ledgers, 2026 research schedule state.

**Mark UNAVAILABLE:** player metrics, player game logs, usage, lineups/depth charts, play-by-play, 2026 in-season team stats, schedule-strength computations, recent-form windows (not stored), any scheduled research ingestion (all hibernated; CFBD quota 0/1000).

**Honest framing for the app:** CFB's live, maintained surface is **market inventory + market history + accounting**. Everything team/game-level is a frozen 2014–2025 research corpus on an orphan branch that no workflow refreshes; exposing it is fine if dated and labelled, but it will not update unless `preseason-research-fetch`/v2 fetch is revived and a CFBD tier with quota is paid for.

---

## 11. Open questions / UNKNOWN items (and what was checked)
- **Kalshi team code ↔ CFBD school map**: none found (`grep kalshi_team_code|TEAM_CODE|code_to_school` over src/scripts → only `app_export.py:118`). UNKNOWN how many of CFBD `abbreviation` values equal Kalshi codes; not measured.
- **v2_shadow non-null rate**: sample row `unavailable_reason` set; share of rows with `v2_probability` not computed (file deleted from scratch to free disk; re-readable via `git show origin/research-data:data/research/v2_shadow/2026/*.jsonl`).
- **Full-history catalog reconstruction**: only 18/69 commits sampled for ticker/quote-state counts; the 83/101 wager-ticker hit rate is a lower bound.
- **v2 builder provenance**: `scripts/fetch_v2_research_cache.py` / `research/v2/state.py` exist only on `claude/cfb-model-v2-research-krupoc` (c1eedc9); their tests were not run or inspected.
- **SP+ sub-components/SOS**: null in the 2025 sample row; whether earlier seasons populate `sos`/`secondOrderWins` not checked.
- **CFBD licence**: `docs/DATA_SOURCES.md` notes raw-data redistribution is prohibited — exposing CFBD rows verbatim in a shared app needs a licence check.
- **`research-data` `src/` copy**: a stale mirror of older code (no v2); `.github` there has 7 workflows — ignored as non-authoritative.
- Disk: the scratch filesystem filled during extraction; large ledgers were re-read via `git show` rather than kept on disk.
