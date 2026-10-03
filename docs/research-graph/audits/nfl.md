# Phase 2 research-data audit — NFL (`chmoses98/nfl-edge-finder`)

Audited 2026-10-03 against the local checkout at `the repository checkout` (branch
`claude/edge-finder-app-readiness-x5li03`, HEAD `35fc65ed`; the three data branches were fetched
`--depth=1`: `origin/market-data` @ `c9b9c0cb` (ls-tree'd, selected paths extracted), `origin/handicap-data`
@ `6b5fbfcd` (2026-10-02, "Record Kalshi wagers (NFL) (#89)"), `origin/handicap-reports` @ `cc75aa23`
(2026-10-02T00:08Z, week-4 T-30m packet)). Everything measured is on a committed branch unless marked
"rebuilt per run". Scratch: `(local scratch, not committed)` (`md_lstree.txt` = full ls-tree of
market-data with blob sizes; `md/` = 109 extracted representative files; `handicap-data/`, `handicap-reports/`).

---

## 1. Summary

1. NFL is by far the richest sport in the fleet. Three append-only/replaced data branches hold **32.8 GB /
   62,037 files** of market observations (`market-data`), a 92 MB handicap packet surface
   (`handicap-reports`, 152 published runs since 2026-09-09) and a 200-file immutable decision + wager ledger
   (`handicap-data`, 84 routed wagers, 84 settlements, 18 amendments).
2. **Market history is VERIFIED and deep**: Kalshi quotes/trades/books captured every ~12 minutes since
   2026-09-04 (3,613 quote files = 10.5 GB, 3,568 trade files = 5.3 GB, 3,192 order-book files = 2.3 GB),
   plus a one-off 2025 backfill (61,557 archived markets, 1,007 tickers with 1-minute/60-minute candles and
   trades). Per-ticker time series are trivially reconstructable (`run_id`, `observed_at`, bid/ask/last/vol/OI).
3. **Projections are VERIFIED-as-produced but RESEARCH-as-authority**: four production projection streams
   (incumbent `shadow-0.4.0` ledger: 131 runs, ~59k rows each; coherent simulation `sim-1.1.0`: 70 runs with
   p05..p95 quantiles per player-stat; Shadow v2 arms: 131 snapshots x 10 arms, 4.2 GB; three-arm game-centre
   experiment: 146 runs). Every one is written once, never overwritten, keyed by `ticker` + `game_id` +
   `player_id`/`player_kalshi_id`, and is explicitly flagged research-only (`betting_authorized: false`,
   `PROJECTABLE_NOT_YET_VALIDATED`). Settlement, close, CLV, autopsy and scorecards exist for all of them
   (1.9M evidence rows in `scorecard_v2.json`).
4. **Team metrics, opponent adjustment, matchups, QB profiles, injuries, depth charts, weather** are produced
   on a production cadence inside the RUN NFL packet (`handicap-reports/latest/packet.json`, 64 MB) — but only
   the **current snapshot** is on a branch; history exists only as 90-day Actions artifacts and as
   `history/index.jsonl` manifest lines. The underlying silver tables (`data/silver/team_game_<season>.parquet`,
   `player_crosswalk.parquet`, `kalshi_player_map.parquet`) are **rebuilt every run from nflverse and are not
   committed anywhere** — this is the single biggest structural gap for a research graph.
5. The **opponent-adjusted team ratings** (`nfl_edge/research/team_ratings.py`) are a real, leakage-free
   weighted ridge (offence + opponent defence + HFA, half-life 10 team-games, 0.6 season carry, ridge 4.0,
   3-season window) and feed the packet's `team_profiles[].adjusted` and `matchup.pairs`. They have a
   point-in-time test but **no accuracy/stability test and no committed history** (only
   `research/game_model/ratings_snapshots.parquet`, 9,408 rows 2009–2025, a different simpler EWMA variant).
6. **Player game logs / usage / efficiency** exist as committed research parquets on `main` (63,940
   player-games 2016–2025 with EWMA, routes, snap/target/carry shares, point-in-time defence-allowed
   features) and as per-run cached tables (`data/cache/sim/`), but the 2026 in-season rows are only inside
   per-run artifacts (anatomy/autopsy records, 5,114 anatomy files), not in a committed per-game table.
7. Weakest areas: no committed historical team/player game-log table for 2026; no venue/park effects beyond
   roof/surface/stadium; `model_uncertainty` is null on every incumbent row; the Kalshi→GSIS player map has
   ~8 unresolved players per game; Shadow v2 `DATA_PLAYER_DIST`/`HYBRID_PLAYER_DIST` are flagged
   `known_defects` (disabled); all sim/v2 player projections are `PROJECTABLE_NOT_YET_VALIDATED`.
8. The app export today reads only `latest/manifest.json`, `latest/analysis/games/*.json`, `handicap-data`
   and `season.actual_wagers.json`; it ignores the entire football evidence layer of the packet
   (`team_profiles`, `quarterbacks`, `offensive_line`, `roles`, `injuries`, `weather`, `matchup`, `players`
   ladders, `simulation.player_projections` quantiles, `game_script_inputs`, `markets[].movement`) and all of
   `market-data`'s history.

---

## 2. Data branches and layout

| branch | HEAD | size | files | contract | producer |
|---|---|---|---|---|---|
| `main` | 35fc65ed | code + `research/` ≈ 45 MB of parquet/json + `data/shadow` (2 bootstrap ledger runs from 2026-09-04, 1.7 MB) | — | code; research artifacts committed by one-off studies | humans / research scripts |
| `market-data` | c9b9c0cb | **32.81 GB** (`git ls-tree -r -l`, sum of blob sizes) | 62,037 | **append-only, immutable**; publishers write NEW files per run (`scripts/ci/publish_market_data.py:9-13`); conflicts fail loudly (exit 3) | every scheduled collector |
| `handicap-data` | 6b5fbfcd | 932 KB | 200 | **immutable decision ledger**, one record per file, `write_record` refuses overwrite (`nfl_edge/handicap/schema.py`) ; `scripts/handicap/verify_append_only.py` | ChatGPT/owner via Airtable sync + kalshi-bet-router PRs |
| `handicap-reports` | cc75aa23 | 92 MB | 39 | **replaced surface**: one root commit per publish (force-with-lease), `latest/` replaced atomically, `history/index.jsonl` + `state/horizons.json` + `app/latest/` carried forward (`scripts/ci/publish_handicap_report.py:34-46`) | RUN NFL / shadow cycle / horizon conductor |

### 2.1 `market-data` layout (bytes from `md_lstree.txt`)

| path | files | bytes | what | cadence |
|---|---|---|---|---|
| `data/kalshi/capture/<day>/<run>.quotes.jsonl` | 3,613 | 10.52 GB | one row per changed ticker quote (bid/ask/last/vol/OI/sizes, parsed family/stat/period/team/player/threshold, `game_id`, `minutes_to_kickoff`, `fingerprint`, `changed`) | conductor-driven, ~120 runs/day (every ~12 min, 2026-09-04 → 2026-10-03) |
| `.../<run>.trades.jsonl` | 3,568 | 5.32 GB | Kalshi trade prints (`trade_id`, `created_time`, `count_fp`, yes/no price, taker side) | same |
| `.../<run>.books.jsonl` | 3,192 | 2.30 GB | order book depth (`orderbook_fp` yes/no ladders), capped at 2,500 tickers/run by kickoff proximity | same |
| `.../<run>.manifest.json` | 3,623 | 391 MB | per-series counts, tiers, client stats, `tickers_confirmed_open`, `quotes_written/unchanged` | same |
| `.../<run>.live.jsonl` | 361 | 159 MB | live (in-game) books | game windows |
| `.../<run>.openset.json` | 2,456 | 13 MB | open-set deltas (`n_open` 17,156 on 2026-10-01) | same |
| `data/kalshi/capture/{state.json, schedule_cache.csv, provisional_series.json}` | 3 | 13.4 MB | fingerprints for 59,230 tickers; nflverse schedule cache 7,548 games 1999–2026; 32 provisional series | overwritten (state) |
| `data/kalshi/discovery/<run>/` | 26,820 | 3.72 GB | 30 daily catalogue runs: `series_all.json` (14,567 series, 19 MB) + per-series `markets/<SERIES>.json` (open/unopened/closed/settled buckets) | daily 09:17 UTC |
| `data/kalshi/backfill/` | 2,409 | 2.46 GB | **2025 season backfill** (one-off 2026-09-04): `markets/<SERIES>.jsonl` (381 series, 61,557 markets), `candles/<SERIES>/<ticker>.json` (1,007 tickers, `h60` + `m1` OHLC bid/ask/OI), `trades/<SERIES>/<ticker>.jsonl` (1,007), `horizons/state_<n>.json` | manual |
| `data/shadow/ledger/<day>/<run>.shadow-0.4.0.observations.jsonl.gz` | 131 (+4 older versions) | 354 MB | incumbent model ledger, ~59k rows/run, 28 days 2026-09-04 → 2026-10-02 | every 2h (`shadow-price.yml` 37 */2) + horizon runs |
| `.../<run>.market_implied.json.gz` | 134 | 8.5 MB | monotone market-implied ladders per `player_kalshi_id|stat|game_id` (3,258 ladders) | same |
| `data/shadow/sim/<day>/<run>.sim-1.1.0.projections.jsonl.gz` (+manifest, scripts) | 70 (+8 v1.0.0) | 37 MB | coherent simulation: per-contract `p_football/p_market/p_reconciled`, `football_p05..p95`, per-game script summaries (20k sims) | 2026-09-16 → 2026-10-02, with the shadow cycle and RUN NFL |
| `data/shadow/v2/projections/<day>/<snap>.<ARM>.projections.jsonl.gz` | 1,875 | 4.24 GB | Shadow v2: 131 snapshots x arms BOARD_V2 (31,805 rows), DATA/MARKET/HYBRID_PLAYER_DIST, DATA/HYBRID_PLAYER_V3/V4/V5 (27,417 rows each) + `contexts.json.gz` (player/game contexts) + `coherence.json` | every 2h (`shadow-v2-project.yml` 7 */2) + horizons; 2026-09-13 → 2026-10-03 |
| `data/shadow/v2/{closes,clv,settlements,autopsy,crosscheck}` | 394/94/296/94/264 | 522/262/231/90/184 MB | per-game evaluation streams (47 games with CLV/autopsy, 294 close dirs incl. SEASON) | every 3h (`shadow-v2-settle.yml` 49 */3) |
| `data/shadow/v2/research/` | 48 | 1.08 GB | weekly research corpus `2026_wk0N.research[.partNN].jsonl.gz` | weekly |
| `data/shadow/v2/{board,board_reports,runs,depth,eligibility,hypotheses,reports,scorecards,inactives,horizons,projection_index}` | ~2.1k | ~330 MB | board retention (19,390 rows), drift reports, run summaries, order-book depth captures, production eligibility, hypothesis registry (263), weekly health/localized signals, cumulative scorecards | various |
| `data/shadow/evaluations/<game_id>/` | 477 | 96 MB | incumbent evaluations per game (5,680 rows for PIT_CLE; 80 game dirs) | every 3h (`postgame-settle.yml`) |
| `data/shadow/scorecards/<run>/` | 116 | 8.6 MB | incumbent cumulative scorecards (29 runs) | same |
| `data/shadow/{arms,arm_evaluations,arm_reports}` | 590/192/837 | 41/17/93 MB | three-arm experiment (146 runs; 48 games evaluated) | 15-min horizon gate |
| `data/shadow/player_anatomy/<game_id>/` | 5,114 | 49 MB | per-run per-player-stat decomposition (EWMA opp/stat, shrink, quantiles, p_plays) — 63 game dirs | with every ledger run |
| `data/shadow/player_autopsy/<game_id>/` | 236 | 1.7 MB | postgame opportunity×efficiency miss classification | postgame |
| `data/context/<day>/<run>.{weather.jsonl,sleeper.json,espn_injuries.json,manifest.json}` | 129 runs | 180 MB | NWS + Open-Meteo forecasts per game, Sleeper players (2,591 kept), ESPN injuries (800 rows) — change-suppressed | every 3h + horizons; 2026-09-04 → 2026-10-03 |
| `data/raw/nflverse/_vintages/injuries/injuries_<season>/` | 2,106 | 5.9 MB | content-addressed snapshots of the mutable nflverse injuries release + index lines | every download |
| `data/handicap/actual_wagers/2026/` | 15 | 2.0 MB | owner wager postmortem (84 wagers, CLV, risk, position lifecycle) | every 6h |
| `data/research/board/2026/board_rows.wk0N.jsonl.gz` | 5 | 30 MB | board research rows (67,485 rows wk1 = 13,497 contracts x 5 horizons) | weekly (Tue) |
| `data/research/weekly/2026/week_0N/` | 16 | 2.4 MB | BOARD_EDGE_DISCOVERY / SCRIPT_AUTOPSY / THESIS_IMPROVEMENT md + research.json.gz | weekly |
| `data/ops/{workflow_outcomes,capture_health}` | 77 | 5.1 MB | workflow run outcomes, horizon capture health | 6h |
| `data/kalshi/fees/<date>.json`, `data/kalshi/probes/` | 5 | 0.5 MB | weekly fee schedule verification (392 series); source probes | Mon |
| `data/shocks/*.jsonl` | 2 | 0 B | empty placeholders | — |

### 2.2 `handicap-reports` layout (92 MB)

`latest/packet.json` 64.1 MB (16 games, 10,721 listed contracts) · `latest/slate.md` 180 KB ·
`latest/games/<game_id>.md` x16 (150–218 KB each) · `latest/analysis/manifest.json` + `games/<game_id>.json`
x16 (1.2–2.1 MB each, 501–721 rows) · `latest/manifest.json` 11 KB · `history/index.jsonl` 152 lines
(2026-09-09T06:43 → 2026-10-02T00:06; triggers shadow-cycle 89 / horizon 47 / manual 16; weeks 1:38, 2:52,
3:53, 4:9; model_version `shadow-0.4.0` throughout) · `state/horizons.json` (57 captured horizons) ·
`app/latest/` (not present in the extracted tree — the exporter had not yet published on this commit; the
README describes it).

### 2.3 `handicap-data` layout (932 KB, 200 files)

`data/imported_wagers/2026/week_0N/routed-*.json` 84 (w1 24, w2 18, w3 38, w4 4) · `wager_settlements` 84
(WON 48 / LOST 36) · `wager_settlement_amendments/2026/week_02` 18 · `recommendations/2026/week_01` 3 (all
`test_only`: 2 RECOMMENDED, 1 PASS) · `executions` 1 · `evaluations` 2 · `postmortems` 1 · `import_receipts` 1
· `runs/README.md`. Reader: `nfl_edge/handicap/store.py:45-59` (`KINDS`, `read_kind`).

### 2.4 What is NOT on any branch (rebuilt per workflow run, gitignored)

`data/raw/nflverse/<release>/` (bronze: pbp 2006+, schedules, stats_player/stats_team week, weekly_rosters,
rosters, snap_counts, injuries, depth_charts, ftn_charting, pbp_participation, pfr_advstats, espn qbr,
players, officials — `scripts/data/nflverse_download.py:33-50`), `data/silver/{games,team_game_<season>,
player_crosswalk,kalshi_player_map}.parquet` (`nfl_edge/data/silver.py`, `nfl_edge/data/ids.py`),
`data/cache/sim/{team_games,player_games,carries,targets}_<season>.parquet` (`nfl_edge/sim/data.py`).
Only the injuries release is vintage-snapshotted onto `market-data`.

---

## 3. Identity model

| entity | canonical id | where defined | cross-source map | coverage / collisions |
|---|---|---|---|---|
| team | nflverse 3-letter code (`ARI`…`WAS`, 32) | `nfl_edge/data/ids.py:53-73` (`TEAMS`, `TEAM_NAMES`, `canon_team`, `TEAM_ALIASES`); normalisation `TEAM_FIX` in `nfl_edge/data/silver.py:24` and `nfl_edge/sim/data.py:22` (OAK→LV, SD→LAC, STL/LAR→LA, JAC→JAX, WSH→WAS, ARZ→ARI, BLT→BAL, CLV→CLE, HST→HOU) | Kalshi codes = nflverse codes (`KALSHI_TEAM_CODES`); ESPN injuries carry team *names* + `team_id`; Sleeper carries codes | `distinct team: 33` in ledgers = 32 + `None`/`TIE` subjects. App export: `TEAM_SOURCE = "nflverse_team"` (`scripts/app_export.py:59`) — same scheme |
| player | nflverse **GSIS id** (`00-00xxxxx`) | `nfl_edge/data/ids.py:104-160` `build_player_crosswalk` → `data/silver/player_crosswalk.parquet` (rebuilt): one row per gsis with `espn_id, pfr_id, pff_id, sleeper_id, sportradar_id, rotowire_id, yahoo_id, fantasy_data_id` coalesced from nflverse `players.parquet`, `roster_<season>.parquet` (2016–2026) and `ff_playerids/db_playerids.csv`, with `_src` provenance and `*_conflict` flags; `name_key` normalised name | Kalshi player UUID → gsis: `scripts/kalshi/build_player_map.py` (exact normalised name within team+season roster, jersey tiebreak; `NAME_ALIASES`; `UNRESOLVED` status) → `data/silver/kalshi_player_map.parquet` (549 entries per `board_build.json`) | **Not committed**; the mapping result is visible only through ledger rows: latest incumbent ledger has 862 distinct `player_kalshi_id` but 360 distinct `player_id`; `UNSUPPORTED_IDENTITY` 115 rows/run; packet week 4: 116 identity-blocked rows, "8 player markets have an unresolved Kalshi->GSIS identity" per game. App export: gsis when present else `kalshi_player_id` (`scripts/app_export.py:250-254`) |
| game | nflverse `game_id` `YYYY_WW_AWAY_HOME` | schedules `games.csv` (cached on market-data as `data/kalshi/capture/schedule_cache.csv`, 7,548 rows 1999–2026, cols incl. `old_game_id, gsis, nfl_detail_id, pfr, pff, espn, ftn, stadium_id`) | Kalshi tickers encode `DDMMMYY` + `AWAYHOME` → `game_id` via `nfl_edge/kalshi/classifier.py` (`classify`, `KALSHI_TO_NFLVERSE`) | App export `EVENT_SOURCE = "nflverse_game_id"`. Capture rows: `distinct game_id: 29` per run; v2 BOARD rows: 80 |
| market | Kalshi ticker (`KXNFLRECYDS-26OCT01PITCLE-CLEKCONCEPCION1-10`) | `nfl_edge/kalshi/classifier.py` parses series/family/period/stat/team/player/threshold | `config/kalshi_nfl_series.json` registry (392 series) | App: `mkt_kalshi_<TICKER>` |
| run / snapshot | `YYYYMMDDTHHMMSSZ` run stamp (capture `run_id`, ledger `run_id`, v2 `snapshot_id`, packet `handicap_run_id`) | every artifact carries it; ledger manifest links `snapshot_run_id` (capture) | — | joins across streams are by (run_id, ticker) |
| prediction | `prediction_id` (sha-20) on ledger rows; `record_id` on sim/v2 rows; `content_hash` | `nfl_edge/shadow/*`, `nfl_edge/engines/*` | evaluations/closes/clv/autopsy reference `prediction_id`/`record_id` | stable across runs for the same (run, ticker, version) |
| context sources | Sleeper `player_id` (string int) with `gsis_id`/`espn_id` fields (often null), ESPN `athlete_id` (null in sample), NWS/Open-Meteo by lat/lon from `config/stadiums.json` | `scripts/data/context_capture.py` | packet matches QBs "by name heuristic" (`quarterbacks[].profile_matched_by`) | name-based joins are a known weak point |

Known collisions / caveats: `_norm_name` strips suffixes (Jr/Sr/II…), so same-name same-team players would
collide (documented as "no fuzzy matching at prediction time"); `espn_id_conflict`/`pfr_id_conflict` flags
exist but counts are only printable at build time (not committed); `TEAM_NAMES` is read by the exporter via
AST to avoid importing polars (`scripts/app_export.py:129-134`).

---

## 4. CAPABILITY MATRIX

Status legend per spec. "Packet" = `handicap-reports/latest/packet.json` (replaced every run; current week only).

| capability | status | origin | coverage | cadence | tests | notes |
|---|---|---|---|---|---|---|
| Team metrics (raw EPA/SR splits) | PARTIAL | packet `games[].team_profiles[team].{season_split,recent_split,long_baseline}` from `nfl_edge/handicap/teamprofile.py:86-142` over silver `team_game_<season>.parquet` | current slate teams; 2023–2026 lookback; splits null below 4 games (`MIN_GAMES`) | every RUN NFL / shadow cycle (2h) | `test_run_nfl_isolation.py` (import audit only) | silver table not committed; only current snapshot on a branch; 30 raw columns (`OFF_COLS`, `DEF_COLS`, `RATE_COLS`) |
| Team metrics (opponent-adjusted) | PARTIAL | `nfl_edge/research/team_ratings.py` → packet `team_profiles[].adjusted` (22 off/def ratings) | current week, games strictly prior, 3-season window | same | `tests/test_starter_regime_research.py:110` (ignores post-cutoff games) | see §6; no committed history except research parquet 2009–2025 (different code path) |
| Player metrics (season/EWMA) | RESEARCH | `research/player_distributions/research_table.parquet` (63,940 rows, 59 cols), `research/opportunity/features_with_defense.parquet` (63,852 x 119) on `main` | 2016–2025 regular season, QB/RB/WR/TE | one-off study outputs (`scripts/research/*_study.py`) | `test_opportunity_leakage.py` (8), `test_sim_pit.py` (8) | 2026 in-season values exist only inside per-run anatomy/autopsy rows and sim features (not a table) |
| Team game logs | PARTIAL | silver `team_game_<season>` (rebuilt; 2006–2025 + current) consumed by packet/ratings; `research/game_model/game_features.parquet` (4,175 games 2010–2025, 127 cols) committed | 2010–2025 committed; 2026 rebuilt per run | per run | `test_run_nfl_workflows.py` (silver step wiring) | per-game off/def EPA, SR, dropback/rush EPA, explosive, sacks, turnovers, drives, ST EPA, proe/cpoe/adot |
| Player game logs | RESEARCH | `research_table.parquet` (stat lines per player-game), `player_usage.parquet` (102,422 rows), `data/cache/sim/player_games_<season>` (rebuilt) | 2016–2025 | one-off / per run | leakage tests above | 2026 rows not committed as a table |
| Historical opponents / results | VERIFIED (schedule) | `schedule_cache.csv` on market-data (7,548 games 1999–2026 with scores); `game_features.parquet` | 1999–2026 | hourly refresh of the cache | `test_active_week.py`, `test_final_status.py` | results also in `arm_evaluations[].actual` and `evaluations[].settlement_evidence` |
| Opponent adjustments | PARTIAL | `team_ratings.py` (team ridge); `nfl_edge/features/defense.py` + `nfl_edge/sim/features.py` (defence-allowed EWMAs); `engines/player/v4/volume.py:49-53` | see §6 | per run | PIT tests only | formula documented; no accuracy test; no committed rating history for 2026 |
| Schedule strength | UNAVAILABLE | nothing computes SOS; `research/game_model` has rest/div_game only | — | — | — | could be derived from `adjusted` ratings x schedule |
| Recent-form windows | PARTIAL | `teamprofile.py`: `recent_split` = last 6 games (`RECENT_GAMES`), `long_baseline` = last 34; QB `recent_200_dropbacks`; sim features two half-lives (3 and 10 games) | current slate | per run | — | fixed windows (L6 / L34 / 200 dropbacks), not L3/L5 |
| Usage (snaps, routes, targets, carries) | RESEARCH (history) / PARTIAL (current) | `features_with_defense.parquet` (`routes`, `snap_share`, `target_share`, `carry_share`, `rz_*`, `i5_*`, `tprr`, `adot`); packet `simulation.player_projections` (`attempts`, `carries`, `targets` means); anatomy `ewma_opportunity` | 2016–2025 committed; 2026 per run | per run | `test_role_context.py` (6), `test_player_context_followups.py` (12) | routes from `pbp_participation` (2016+); snap counts from `snap_counts` release |
| Lineups / depth charts | PARTIAL | packet `roles.by_team` (Sleeper depth chart, `depth_chart_order`, status, injury_status); sim `features.depth_chart()` (nflverse daily ESPN chart with cutoff); v2 `contexts.json.gz` `player_contexts[].depth_chart` | current slate; Sleeper captures since 2026-09-04 (126 files) | 3h + horizons | `test_role_context.py`, `test_depth.py` (order-book depth, not lineups) | history reconstructable from `data/context/*/sleeper.json` (change-suppressed) |
| Injuries / availability | VERIFIED (capture) / PARTIAL (interpretation) | `data/context/<day>/<run>.espn_injuries.json` + `sleeper.json`; nflverse injuries vintages (`_vintages/injuries`); packet `injuries.{summary,records}` with diffs vs previous capture; ledger `availability_state/p_plays/p_inactive` (`nfl_edge/settlement/availability.py STATE_PLAY_RATES`) | 2026-09-04 → now, 129 runs; vintages per season 2012–2026 | 3h (`context-capture.yml` 23 */3) | `test_availability.py`, `test_injury_vintage_freeze.py`, `test_injury_vintage_multiroot.py`, `test_weather_vintages.py`, `test_capture_health.py` | `p_plays` null on 92% of ledger rows (non-player markets) |
| Matchup metrics | PARTIAL | packet `matchup.pairs` (offence rating vs defence rating per metric: neutral-script, run, pass, early-down, explosive, protection vs rush, overall) from `adjusted` | current slate | per run | — | derived from the ridge ratings; no history |
| Projection distributions | PARTIAL (produced) / RESEARCH (authority) | sim `football_p05/p25/p50/p75/p95`, `football_sd` per contract (`nfl_edge/sim/prospective.py:172-192`); v2 arms `distribution_summary`/lattice (`nfl_edge/engines/player/dist.py`); sim `scripts.json.gz` per-game environment quantiles | 70 sim runs 2026-09-16 → 10-02 (6,219 rows/run, 15 games); v2 131 snapshots | 2h + horizons | `test_sim_engine.py` (30), `test_sim_script.py`, `test_handicap_sim_projection_exposure.py`, `test_player_v3_and_eligibility.py`, `test_player_v4.py`, `test_player_v5.py` | all `PROJECTABLE_NOT_YET_VALIDATED` / research; quantiles only on PRICED rows (29% null) |
| Raw projections (point) | PARTIAL | incumbent ledger `model_event_probability/model_contract_value` (5,975 SUPPORTED rows/run), anatomy `projected_stat_mean/projected_opportunity_mean`, sim `football_mean/final_mean` | 131 ledger runs 2026-09-04 → 10-02 | 2h | `test_ladder_pricing.py`, `test_incumbent_unchanged.py`, `test_ledger_integrity.py` | `model_uncertainty` null 100% |
| Market prices (current) | VERIFIED | `data/kalshi/capture/<day>/<run>.quotes.jsonl` (7,485 changed rows in sample run; 17,156 open) + packet `markets[]` (730/game) | 2026-09-04 → now | ~12 min | `test_capture_smoke.py`, `test_capture_isolation*.py`, `test_openset_and_close_v2.py`, `test_decision_quotes.py` | parsed family/stat/period/threshold on every row |
| Market price history | VERIFIED | same files, keyed `(ticker, run_id, observed_at)`; `books.jsonl`; `trades.jsonl`; 2025 backfill candles `h60`/`m1`; packet `markets[].movement.horizons{T-72h..T-30m}` | 30 days 2026 + full 2025 season (1,007 tickers) | ~12 min | `test_movement_observed.py`, `test_candle_parsing.py`, `test_close_selection.py` | 18.7 GB; change-suppressed (`changed`, `quotes_unchanged`) so a series must be forward-filled |
| Advanced stats (EPA, SR, CPOE, PROE, xpass) | PARTIAL | silver `team_game` from nflverse pbp (`nfl_edge/data/silver.py:37-123`); QB profiles (`teamprofile.py:165-257`: epa/dropback, success, cpoe, adot, sack/int rate, pressure proxy, deep rate; overall / under_pressure / clean_pocket / recent_200) | current snapshot in packet; raw pbp 2006+ rebuilt | per run | — | QB profile null below 100 dropbacks (`QB_MIN_DROPBACKS`); week-4 CLE QB had 92 → null |
| Situational splits | PARTIAL | silver: neutral-script (`*_ng`), early-down, red-zone (`rz_*`), home/away via `is_home`; QB pressure/clean; sim `team_volume.by_final_margin` (lead14+/lead7-13/within6/trail7-13/trail14+) | current | per run | `test_run_nfl_script_context.py` | no stored home/away split tables; computable from team_game |
| Player props | VERIFIED (market) / RESEARCH (model) | capture `family=PLAYER_STAT` (2,653 rows/run), ledger 24,476 rows/run, packet `players[name].stats[stat].{model,market}` ladders with thresholds | 2026 season | 12 min / 2h | `test_ladder_pricing.py`, `test_market_board_completeness.py` | stats: receiving_yards, receptions, rushing_yards, touchdowns, passing_yards, passing_tds, carries, attempts, completions, interceptions, fantasy_points, longest_*, first_td |
| Team props | VERIFIED (market) | `TEAM_TOTAL`, `TEAM_STAT`, `FIRST_TD_TEAM`, `RACE_TO_N`, `BOTH_TEAMS_SCORE*`, period variants | 2026 | 12 min | same | incumbent prices TEAM_TOTAL/BOTH_TEAMS_SCORE_N only; others Shadow v2 PERIOD/GAME engines |
| Game-level markets | VERIFIED (market) / PARTIAL (model) | `GAME_WINNER`, `SPREAD`, `TOTAL` (+1H/2H/1Q–4Q); packet `market_implied{,_by_period}` (implied spread/total/win prob from ladders, `fast_implied_lines`) | 2026 | 12 min | `test_game_engine_v2.py`, `test_period_engine_v2.py`, `test_three_arm_pricing.py` | `center_source: kalshi_implied_interpolated` |
| Play-by-play | PARTIAL | nflverse pbp 2006+ downloaded per run (bronze, not committed); only aggregates survive | — | per run | — | not exposable from a branch |
| Weather | VERIFIED (capture) | `data/context/<day>/<run>.weather.jsonl` (26 games/run: NWS hourly periods around kickoff + Open-Meteo hourly wind/gust/precip/humidity); packet `weather{temperature_f,wind,wind_direction,precipitation_probability,short_forecast,previous,changed,material}` | 2026-09-04 → now | 3h | `test_weather_vintages.py` | `ledger.weather_vintage` null 100% (not used by the incumbent) |
| Venue / park effects | UNAVAILABLE (effects) / PARTIAL (attributes) | `config/stadiums.json` (lat/lon/roof), schedule `roof/surface/stadium_id/temp/wind`, packet `venue/roof/surface/neutral_site` | — | — | — | no venue effect estimates anywhere |
| Calibration data | VERIFIED (research) | `data/shadow/scorecards/<run>/cumulative.scorecard.json` (434,248 snapshots / 19,652 contracts / 49 games; Brier, log-loss, bands), `data/shadow/v2/scorecards/scorecard_v2.json` (1,910,649 rows, 775 games; Brier 0.1504 vs market 0.1442, ECE 0.0405), `eligibility/latest.json` (130 arm×family cells), `research/ladder_calibration/rung_metrics.parquet` (2,110 rows 2016–2025) | 2026 wk1–4 | 3h | `test_eval_scorecard.py`, `test_autopsy_and_scorecard_v2.py`, `test_evaluation.py` | all research; model is worse than market overall |
| Historical accuracy / postmortems | VERIFIED (research) | `evaluations/<game>/eval-1.0.0.*.jsonl.gz` (per prediction: settled_yes, close, CLV, bands), v2 `autopsy` (18,416 rows/game; classes TARGET_SHARE_MISS, SNAP_MISS, TEAM_VOLUME_MISS…), `player_autopsy`, `arm_evaluations` (actual scores vs 3 arms), weekly `SCRIPT_AUTOPSY.md` | 80 games (incumbent), 47 games (v2) | 3h | `test_player_autopsy.py`, `test_postgame_*.py`, `test_three_arm_evaluation.py` | `autopsy_v2_summary.json`: 525,467 rung×snapshot rows, 5,561 player-game-stats |
| CLV | VERIFIED | v2 `clv/<game>/clv-2.0.0.*.jsonl.gz` (47,617 rows PIT_CLE; `clv_mid_toward_model`, `clv_exec_toward_model`, `clv_net_of_fee`, entry fee state); incumbent evaluations `signed_clv_mid/executable`; owner wagers `clv_state/clv_per_contract` (66 CLV_VALID of 84) | 2026 | 3h / 6h | `test_clv.py`, `test_clv_v2.py` | close rule `close-2.1.0` (freshest pregame quote within tolerance) |
| Historical wager outcomes | VERIFIED | `handicap-data` imported_wagers + settlements + amendments; `market-data/data/handicap/actual_wagers/2026/season.actual_wagers.json` (84 wagers, stake 7,270.79, net −1,068.35 over 82 with economics; by_game 36, by_family 15, position episodes, risk warnings) | 2026 wk1–4 | router PRs + 6h postmortem | `test_imported_wager_accounting.py`, `test_wager_settlements.py`, `test_settlement_amendments.py`, `test_position_lifecycle.py`, `test_wager_risk.py`, `test_actual_wager_pipeline.py` | already consumed by the app export |
| Identity tables | PARTIAL | see §3 | — | per run | `test_player_crosswalk_bootstrap.py`, `test_build_player_map_schema.py`, `test_kalshi_classifier.py` | not committed |
| Schedules | VERIFIED | `schedule_cache.csv`; `nfl_edge/data/nfl_calendar.py` (active-week resolution) | 1999–2026 | hourly | `test_active_week.py` | kickoff times ET→UTC in exporter |
| Seasons covered | — | market data 2025 (backfill) + 2026; football 2006/2016–2026 (rebuilt); research tables 2009/2016–2025 | | | | |
| Shocks (role/injury shocks) | RESEARCH | `research/shocks/shocks_2025.parquet` (1,243 rows); `data/shocks/*.jsonl` empty on main and market-data; `nfl_edge/shocks/` | 2025 only | none live | `test_live_shocks.py`, `test_shock_expectation_gate.py` | not produced in 2026 |
| Order-book depth | VERIFIED | `books.jsonl` + v2 `depth/` captures; ledger `book_depth_yes/no`, `book_imbalance` | 2026 | 12 min / 20 min | `test_depth.py`, `test_shadow_v2_depth_budget.py` | capped 2,500 tickers/run |
| Fees | VERIFIED | `data/kalshi/fees/<date>.json` (392 series), `config/kalshi_fee_schedule.json` | weekly since 2026-09-08 | Mon | `test_fees.py`, `test_fee_freshness.py`, `test_fee_change_supersession.py` | |
| Hypotheses / preregistration | RESEARCH | `research/hypothesis_registry/` (28 H-files), v2 `hypotheses.jsonl` (263 GENERATED), weekly `localized_signals.json` (276 evaluations, 13 under test) | 2026 | weekly | `test_hypothesis_governance.py`, `test_three_arm_preregistration.py`, `test_localized_signal_research.py` | governance-only, "no automatic learning" |

---

## 5. Detailed findings per category

### 5.1 Team metrics
* **Raw**: `nfl_edge/data/silver.py:37-123` builds `team_game_<season>.parquet` from pbp with per-game
  columns: `plays, epa_play, epa_total, success_rate, dropbacks, dropback_epa, dropback_sr, rushes, rush_epa,
  rush_sr, early_down_epa, epa_play_ng, success_rate_ng, dropback_epa_ng, rush_epa_ng, explosive_passes,
  explosive_runs, sacks, qb_hits, ints, fumbles_lost, turnovers, proe_early_ng, proe, cpoe, adot, rz_epa,
  rz_plays, tds, yards, no_huddle_rate, shotgun_rate, neutral_plays, st_epa_for, st_epa_against, fga, fgm,
  td_drives, fg_drives, to_drives, n_drives` for offence (`off_*`) and defence (`def_*`), plus `is_home`,
  `opp`. `teamprofile.py:52-66` derives `off_explosive_rate, def_explosive_rate, off_sack_rate_allowed,
  def_sack_rate, def_qb_hit_rate, off_turnover_rate, def_takeaway_rate, off_td_drive_rate,
  off_plays_per_drive`. Reported splits: `season_split` (current season), `recent_split` (last 6),
  `long_baseline` (last 34 games across seasons), each with `n_games` and `insufficient_sample` (<4 games →
  null). Week-4 packet example: CLE `season_split.n_games=3, insufficient_sample=true`, `long_baseline`
  34 games with 30 metrics. Honesty constraint at week 1 (`basis: prior_season_2025_no_games_played_in_2026`).
* **Adjusted**: `team_profiles[team].adjusted` carries 22 values `off_/def_` × {epa, sr, db_epa, rush_epa,
  epa_ng, explosive, sack_rate, to_rate, proe, st_epa, ed_epa} (`team_ratings.snapshot_ratings`).
* **Committed history**: `research/game_model/ratings_snapshots.parquet` (9,408 rows, 17 seasons 2009–2025
  × 544/576 team-weeks, 25 cols) — produced by `scripts/research/game_model_study.py`, a simpler
  walk-forward snapshot, not the ridge solver. `game_features.parquet` joins h_/a_ ratings to 4,175 games.
* **Rankings**: `teamprofile.league_ranks()` (1-based rank over profiles on one metric) exists
  (`teamprofile.py:145-155`) and is used by the renderer.

### 5.2 Player metrics / game logs / usage
* `research/player_distributions/research_table.parquet` — 63,940 player-games, 2016–2025 (5,994–6,747
  per season), QB 6,411 / RB 15,546 / TE 15,748 / WR 26,235, 0% nulls except `offense_snaps` 1%; columns:
  box stats (`completions … receiving_tds`), `offense_snaps`, `zero_row` (11.6% synthetic zero rows for
  rostered inactive players), market context (`spread_line, total_line, implied_total, home, spread_team`),
  `qb_starter`, `ewma_*` for 15 stats, `n_prior, w_eff, shrink_w`, rate features (`ypa, comp_rate,
  ptd_rate, int_rate, ypc, rushtd_rate, ypt, catch_rate, rtd_rate, anytd_rate`). Built by
  `nfl_edge/research/player_distributions.py:111-222` from nflverse `stats_player_week` + `snap_counts`.
* `research/opportunity/features_with_defense.parquet` — 63,852 × 119: adds `pbp_snaps, routes
  (92.9% >0), rush_snaps, rz_routes, rz_snaps, rz_targets, i10_targets, air_yards, rz_carries, i5_carries,
  team_* volumes, route_share, target_share, tprr, carry_share, rz_target_share, rz_carry_share,
  i5_carry_share, snap_share, adot`, their point-in-time versions `pit_*`, projected team volumes
  (`proj_team_dropbacks/rush_att`), decompositions, and ten `pit_def_allowed_*` opponent-defence features.
* `research/opportunity/player_usage.parquet` (102,422 rows, 2016–2025, per game_id×player_id:
  `pbp_snaps, routes, rush_snaps, rz_routes, rz_snaps, targets, rz_targets, i10_targets, air_yards (56.6%
  null), carries, rz_carries, i5_carries`) and `team_volume.parquet` (5,522 team-games).
* Production per-run equivalents: `nfl_edge/sim/data.py:159-252` (`player_games`, `carries`, `targets`
  with official box-score conventions; cached under `data/cache/sim/`, not committed);
  `nfl_edge/sim/features.py:265-330` `player_features` (shares `sh_target, sh_carry, sh_attempt,
  sh_rz_target, sh_rz_carry` with half-lives 3/10, `rt_ypc, rt_ypt, rt_catch_rate, rt_adot,
  rt_explosive_rate` with `rate_shrink_k=40`, `last_*`, `gap_weeks`).
* 2026 observed usage per player-game exists only inside autopsy rows (`actual: {snaps, snap_share,
  targets, target_share, routes(null), carries, team_volume, …}` in `data/shadow/v2/autopsy/<game>/`,
  `carries/targets/snaps/receptions/pass_attempts` in `player_autopsy`) — joinable by `(game_id,
  player_id)` but not a table.

### 5.3 Quarterback profiles
`teamprofile.build_qb_profiles` (`:165-257`) from pbp of the most recent season with games: per
`passer_player_id` `dropbacks, epa_per_dropback, success_rate, cpoe, adot, sack_rate, int_rate,
pressure_rate_proxy (qb_hit), deep_rate` for `overall`, `under_pressure` (qb_hit or sack), `clean_pocket`,
`recent_200_dropbacks`, plus `rushing{qb_rushes, qb_rush_ypc}`; null below 100 dropbacks. Packet attaches to
`quarterbacks[team][]` with Sleeper depth order, status and `availability_confidence`. Week 4: Watson 92
dropbacks → thin → all null; Gabriel no profile ("rookie, new name spelling, or no prior dropbacks").

### 5.4 Offensive line
`offensive_line[team]` lists injured/listed linemen with `likely_role_impact`; `detailed_metrics_available:
false` ("We capture no pass-block or run-block grades"). UNAVAILABLE beyond injury listing.

### 5.5 Injuries / availability / roles
* Capture: `scripts/data/context_capture.py` — ESPN injuries (800 rows: team, name, position, status, type,
  injury, detail, return_date, short_comment), Sleeper players slim (2,591: injury_status, body_part, notes,
  practice_participation, depth_chart_order/position, status, gsis_id, espn_id), change-suppressed by sha1,
  `manifest.failed_closed`, health HEALTHY/DEGRADED/FAILED.
* nflverse injuries vintages: `data/raw/nflverse/_vintages/injuries/injuries_<season>/<sha16>.parquet` +
  `index.<run>.jsonl` (rows_by_week, teams, sha256) — `nfl_edge/shadow_v2/vintage_snapshots.py`
  (`resolve_injuries` newest at-or-before cutoff, never the mutable file).
* Packet `injuries.summary` (capture run, previous run, per-source content vintage, carried-forward flag)
  and `injuries.records[]` (player, position, state, source, first_seen_in_capture, detail, body_part,
  return_date, comment, espn_report_date, sleeper_status, practice, changed_since_previous).
* Ledger availability: `availability_state, p_plays, p_inactive` (`STATE_PLAY_RATES`); v2 contexts
  `player_contexts[].availability{state, p_plays, p_active_no_snap, sources, stale_minutes}`,
  `depth_chart{rank, group_rank, position, state, team_qb1, vintage}`; `inactives/` collector (research
  only, 150-min window, asymmetric "can only ADD").
* Roles: packet `roles.by_team[team][slot]` (Sleeper depth chart grouped by `depth_chart_position`, e.g.
  `RWR`, `RB`), `source: sleeper depth chart`.

### 5.6 Weather / venue
`data/context/<day>/<run>.weather.jsonl` (one row per outdoor/unknown-roof game within 10 days:
`nws.periods[]` hourly temperature/wind/gust/precip prob/dewpoint around kickoff; `open_meteo` hourly).
Packet `weather`: `forecast_vintage, forecast_updated, temperature_f, wind, wind_direction,
precipitation_probability, short_forecast, period_start, previous{...}, changed_since_previous_capture,
material`. Slate `weather_concerns` count (2 in week 4). Venue attributes only (`config/stadiums.json`,
`roof/surface/stadium` from schedule). No venue effect model.

### 5.7 Market prices and history
* Quote row schema (`quotes.jsonl`): `run_id, observed_at, ticker, event_ticker, series_ticker, family,
  period, stat, team, player_name, player_kalshi_id, threshold, operator, floor_strike, game_id, kickoff_utc,
  minutes_to_kickoff, pregame, fingerprint, changed, yes_bid/ask_dollars, no_bid/ask_dollars,
  last_price_dollars, volume_fp, open_interest_fp, liquidity_dollars, yes_bid/ask_size_fp, status, result,
  close_time, open_time, expected_expiration_time`. Sample run `20261001T235937Z`: 7,485 changed rows
  (9,671 unchanged suppressed) across 29 games, 33 teams; families PLAYER_STAT 2,653, SPREAD 1,031, TOTAL 824,
  TEAM_TOTAL 388, …; periods FULL 3,657 / 1H 423 / 4Q 313 / 1Q 302 / 2H 302 / 2Q 251 / 3Q 232.
* Trades: 23,931 rows/run over 394 tickers (`trade_id, created_time, count_fp, yes/no price, taker side`).
* Books: 2,500 tickers/run (`orderbook_fp.yes_dollars/no_dollars` price×size ladders), 7,238 dropped by cap.
* Daily volume by day (MB): 152 (09-04) … 1,443 (09-13, game day) … 1,385 (09-27) … 755 (10-02). Run
  cadence from manifests: 3,623 runs / 30 days ≈ 121/day.
* 2025 backfill: `backfill/markets/<SERIES>.jsonl` (full Kalshi market objects incl. `result`,
  `settlement_value_dollars`, `rules_primary`), `candles/<SERIES>/<ticker>.json` (`h60` 466 buckets, `m1`
  523 buckets with yes_bid/ask OHLC, OI, volume), `trades/…` (866 prints for the sample ticker),
  `horizons/state_<n>.json` (9,151 tickers done per shard). `research/kalshi_2025/archived_markets.parquet`
  (61,557 rows, 266 games) is the committed index.
* Packet per-market `movement`: observed mid at `T-72h, T-48h, T-24h, T-12h, T-6h, T-3h, T-90m, T-1h,
  T-30m` with `observed/at/mid/move_to_current`, `first_observed` ("first capture, not the market open"),
  `total_move_since_first_capture`, built by scanning 3,493 capture files (`sources.capture_files_scanned_for_movement`).
  Tests: `test_handicap_packet.py:106-126` (never interpolates; first observation is not "the open").

### 5.8 Projections / simulation
* **Incumbent ledger** (`shadow-0.4.0`, `nfl_edge/shadow/`): 58,976 rows/run, 80 games, 360 gsis players;
  support states POST_KICKOFF_EXCLUDED 35,483 / UNSUPPORTED_MODEL 14,030 / SUPPORTED 5,975 /
  UNSUPPORTED_RULES 3,373 / UNSUPPORTED_IDENTITY 115. SUPPORTED by family: PLAYER_STAT 4,738, TEAM_TOTAL
  409, SPREAD 408, TOTAL 304, GAME_WINNER 60, BOTH_TEAMS_SCORE_N 56. Fields: `model_event_probability,
  model_contract_value, calibrated_probability (= same; calibration_version none-v0), model_uncertainty
  (null), model_market_disagreement, raw_yes/no_disagreement, market_implied_mean (null), game_env_version,
  availability_state, p_plays, p_inactive, book_*`. `market_implied.json.gz`: per
  `player_kalshi_id|stat|game_id` → `k[], p_raw[], p_monotone[], side, raw_violations,
  implied_mean_lower_bound`.
* **Coherent simulation** (`sim-1.1.0`, `nfl_edge/sim/*`): `simulate.py` draws plays/pass-rate conditional
  on realised script, Dirichlet-multinomial opportunity shares, empirical per-touch banks binned on ridge
  predictions (`models.py:217-300`), per-game shock `tau2`; `reconcile.py` blends with market
  (`reconciliation_weights.json` per stat: any_td, pass_td, pass_yards, rec_yards, receptions, rush_yards);
  bundle `research/simulation_engine/bundle_2026.json` trained 2018–2025, priors 2016–2025
  (`sim-priors-1.0.0`). Output row: `p_football, p_market, p_reconciled, reconcile_weight,
  disagreement_vs_mid, football_mean/sd/p05/p25/p50/p75/p95, market_mean/p50, final_mean/p50,
  support_state ∈ {PRICED 4,082, MARKET_CENTRED_GAME 1,105, UNSUPPORTED_STAT 653,
  FOOTBALL_ONLY_NO_RECONCILIATION 313, UNSUPPORTED_IDENTITY 33, NOT_ELIGIBLE 33}`, `coherence_ok`.
  `scripts.json.gz`: per game `environment{home_margin,total,home_points,away_points: mean/sd/p05..p95}`,
  `p_home_win, p_one_score, p_blowout_17plus`, `team_volume{plays, pass_att, designed_rush, dropbacks,
  scrambles, pass_rate: mean/range_50/range_90, by_final_margin{…}}` (packet `game_script_inputs`).
  Packet `simulation.player_projections` (61 per game): per player×stat `football_mean/sd/p05..p95,
  market_mean/p50, final_mean…`; `coverage.buckets` (SIMULATED_AND_EXPOSED 61, UNSUPPORTED_STAT 35,
  UNSUPPORTED_IDENTITY 2 of 98 listed groups).
* **Shadow v2** (`nfl_edge/engines/*`, `shadow-v2-1.0.0`, `projection-2.3.0`): engines GAME, PERIOD,
  JOINT, SEASON, PLAYER (data-player-dist 2.0.0/3.0.0/4/5); 40,000 sims; per row `p_yes, contract_value,
  model_market_disagreement_mid, question (exact YES meaning), semantic_confidence, settlement_reachability,
  flags{betting_authorized:false, historically_validated:false, prospectively_validated:false}, feature_lineage
  (availability), subject_id (gsis) / subject_kalshi_id, horizon_label (CYCLE/T-24h/T-6h/T-90m/T-30m)`.
  BOARD_V2 2026-10-03: 31,805 rows, states POST_KICKOFF 18,077 / RESEARCH_REQUIRED 5,499 /
  PROJECTABLE_NOT_YET_VALIDATED 4,346 / NON_FOOTBALL_MODEL 1,738 / PRICED 1,213 / SEMANTICS_AMBIGUOUS 728 /
  JOINT_MODEL_REQUIRED 107. Player arms 27,417 rows, 64 games, `p_yes` null 83–86% (post-kickoff +
  abstentions: `ABSTAIN_MODEL_UNVALIDATED 3,192, ABSTAIN_INJURY_UNCERTAIN 307, ABSTAIN_ROLE_UNCERTAIN 232`).
  `run summary.game_envs` per game: period-engine and game-engine `p_home_win, mean/sd margin, mean/sd total`.
  `coherence.json`: 1,207 mutually-exclusive/exhaustive groups with `sum_mid/sum_bid/sum_ask`,
  `research_incoherence`.
* **Three-arm** (`nfl_edge/arms/`, `three-arm-1.0.0`): per contract `p_current, p_data_only, p_hybrid`
  (CURRENT_MARKET_PRIOR / DATA_ONLY / HYBRID_30_DATA), 1,237 contracts/run over 30 games; per game
  `arm_games` (projected margin/total per arm); evaluations vs actual score with Brier/MAE (`arm_evaluations`),
  weekly scorecards (`min_games_for_verdict 64`, `headline_evidence INSUFFICIENT_EVIDENCE`).
* **Production eligibility**: packet `production_eligibility.families["BOARD_V2|ALL"]` = WATCH (17 games,
  2 weeks, ΔBrier −0.0007 ± 0.0008, CLV +0.011; needs ≥48 games / 3 weeks, upper95 ≤ 0.002, ECE ≤ 0.03,
  CLV ≥ 0 for LIMITED).

### 5.9 Settlement, evaluation, CLV, autopsy
* Incumbent `evaluations/<game>/eval-1.0.0.<batch>.evaluations.jsonl.gz`: per prediction `settled_yes,
  settlement_kind/source/evidence, close_* (freshest pregame quote), signed_clv_mid/executable,
  calibrated_probability, bands (contract value, disagreement, horizon), movement, p_plays`. 5,680 rows for
  PIT_CLE (375 tickers × 41 runs). 80 game directories + run summaries.
* v2: `closes` (close-2.1.0; `CLOSE_NOT_APPLICABLE_SEASON` for season markets), `clv` (clv-2.0.0),
  `settlements` (settle-2.0.0; provisional vs terminal evidence tiers; season settlements
  `REFUSED_SEASON_INCOMPLETE`), `crosscheck` (exchange vs football settlement agreement 99.78% over
  1,666,804 comparable; 3,667 disagreements listed), `autopsy` (autopsy-3.1.0: per rung×snapshot
  `components{catch_rate, efficiency, opportunity, snap_share, target_share, team_volume}` with miss flags,
  `classification`, `robust_z`, `percentile`).
* Owner wagers postmortem (`actual-wager-postmortem-1.1.0`): `wagers[]` (84) with `clv_state`, `close_price`,
  `close_quality`, `execution_price`, `net_*`, `position_episode_id`; `risk` (wager-risk-1.0.0: concentration,
  correlated exposure, opposing pairs; bankroll NOT_AVAILABLE).

### 5.10 Board / weekly research corpus
`data/research/board/2026/board_rows.wk01.jsonl.gz`: 67,485 rows = 13,497 contracts × horizons
{T-24h, T-6h, T-90m, T-30m, latest_pregame}, 120 columns (environment bands, ladder structure
`is_main_rung/rung_offset/ladder_n_rungs/monotone_violations`, fees, close/CLV, settlement, exchange
agreement, data-only/hybrid disagreement bands, `player_gsis_id`, `role_certainty`, `team_implied_points`).
Weekly outputs: `BOARD_EDGE_DISCOVERY.md`, `SCRIPT_AUTOPSY.md`, `THESIS_IMPROVEMENT.md`, `research.json.gz`
for weeks 1–3 and cumulative (`weekly-research.yml`, Tue 15:43 UTC). `board_coverage.json` funnel
(discovered → mapped_to_game → captured_pregame → settled → price_at_primary_horizon → executable_quote →
fee_known → analyzed_primary) by week×family.

---

## 6. Opponent-adjustment audit

### 6.1 Team ratings — `nfl_edge/research/team_ratings.py`
* **Formula** (`solve_ratings`, lines 38-75): for metric column `y` on team-game rows,
  `y_{g,t} − μ = off_t + def_opp + hfa·(+1 home / −1 away) + ε`, weighted least squares with
  `w = 0.5^(weeks_ago / halflife_games) · season_carry^(seasons_back)`; `halflife_games = 10.0` (measured in
  league weeks, `week_no`), `season_carry = 0.6`, ridge `λ = 4.0` on every team parameter (shrinks toward the
  league mean μ, i.e. partial pooling), `λ_hfa = 0.01`. Solved in closed form (`np.linalg.solve`). Returns
  `(off, def)` per team, `hfa`, `mean`, `n`, `eff_n`. Refuses below 40 rows.
* **Simple, not recursive**: one joint regression per metric, no iteration; opponent effect enters as the
  opponent's defence coefficient estimated simultaneously (this is the standard "simultaneous ridge"
  adjustment, not an iterative SRS loop).
* **Baseline**: league mean μ of the windowed sample (weighted average). Ratings are deviations from μ.
* **Metrics** (`snapshot_ratings` lines 90-109): `epa, sr, db_epa, rush_epa, epa_ng, explosive,
  sack_rate, to_rate, proe, st_epa, ed_epa` (off and def each) → 22 numbers per team + `hfa_<metric>`.
* **Window / point-in-time**: games with `(season, week)` strictly before the snapshot and `season ≥
  snapshot − 3` (line 93). `prepare_rows` assigns chronological `game_no`/`week_no`.
* **Sample requirements**: ≥40 rows total; no per-team minimum (ridge handles thin teams); at week 1 the
  window is entirely prior seasons (packet marks `basis`).
* **History**: computed fresh per run; current values in `packet.json`
  (`team_profiles[team].adjusted`, `matchup.pairs`), carried in the 90-day Actions artifact and nowhere
  else. `research/game_model/ratings_snapshots.parquet` (2009–2025, 9,408 rows) is a committed historical
  series but from `scripts/research/game_model_study.py` (not this solver — columns differ:
  `off_epa, def_epa, …, off_proe, def_proe, off_st_epa, def_st_epa` without hfa).
* **Tests**: `tests/test_starter_regime_research.py:110 test_ratings_ignore_post_cutoff_games_entirely`
  (point-in-time), plus `:94/:101` cutoff refusals. No test of accuracy, shrinkage magnitude, or
  stability. `teamprofile.py:113-115` swallows any ratings exception (`adjusted = {}` + note in `basis`).
* **Limitations**: HFA is a single league-wide scalar; no pace/garbage-time weighting beyond the `_ng`
  metric variants; metric scale differs per metric (not standardised); 3-season window with 0.6 carry means
  early-season ratings are dominated by prior seasons; no uncertainty/SE reported.

### 6.2 Player/defence adjustments
* `nfl_edge/features/defense.py:1-45` (`build_defense_features`): per (game, defteam) sums of what the
  defence allowed (`receptions, receiving_yards, targets, rushing_yards, carries, passing_yards,
  passing_tds, any_td` + yds/target, yds/carry), then `point_in_time_ewma` per `defteam` with `halflife=6`
  games, `season_carry=0.35`, `shrink_k=4` toward a league prior fitted on 2016–2018 ("A defence's own
  current game never enters its own feature"). Produces `pit_def_allowed_*` in
  `features_with_defense.parquet` (0% null). Used by `player_distributions.DEFENSE_FEATURES` (research
  `p_def`, `p_roledef` arms in `research/model_vs_market/prop_probs_2025_both_arms.parquet`, 24,731 rows) —
  the module docstring records "market encompasses the model, model coefficient −0.02 ± 0.09".
* Simulation layer: `nfl_edge/sim/features.py` `team_features` carries both own `off_*` and `def_*` columns
  (`allowed_*`), shrunk toward frozen fit-season league means with `team_shrink_k=4` games, half-life 8,
  season carry 0.5; `simulate.py:132-135, 179, 197, 245` uses the opponent's `def_plays, def_sec_per_play,
  def_pass_rate, def_neutral_pass_rate, def_sack_rate, def_pass_td_share, def_ypc, def_ypa, def_comp_rate`
  as regressors in the plays/pass-rate/efficiency ridges (`CARRY_FEATURES`, `TARGET_FEATURES` in
  `models.py:217-220`). Tests: `tests/test_sim_pit.py` (8; priors cannot see the evaluation season) and
  `tests/test_opportunity_leakage.py` (8).
* Player engine v4 `volume.py:49-53`: opponent-allowed pass/rush attempts EWMA (`o_pa_allowed`,
  `o_ra_allowed`) in the team-volume model.
* Shadow v2 player arms and the incumbent `shadow-0.4.0` mean models have **no opponent term**
  (`defense.py` docstring; anatomy `efficiency_decomposition: "none"` for most stats).

---

## 7. Inventories

### 7.1 Time-series-capable datasets

| dataset | x-axis | keys | rows | links to game_id / opponent |
|---|---|---|---|---|
| Kalshi quotes (`capture/*/quotes.jsonl`) | capture run (`observed_at`, ~12 min) | `ticker`, `run_id` | ~7.5k changed rows/run × 3,613 runs (≈27M rows; forward-fill needed) | `game_id` on game/player rows; opponent via ticker teams |
| Kalshi trades | trade time | `ticker`, `trade_id` | ~24k/run | via ticker → game_id |
| Order books | capture run | `ticker`, `run_id` | 2,500/run | yes |
| 2025 candles | 1-min / 60-min bucket | `ticker` | 466 h60 + ~520 m1 per ticker × 1,007 | via `archived_markets.parquet.game_id` |
| Incumbent ledger | run (2h + horizons) | `ticker`, `prediction_id`, `player_id` | 59k/run × 131 | yes (`game_id`, `home_team/away_team`) |
| Sim projections | run | `ticker`, `record_id`, `player_id` | 6.2k/run × 70 | yes |
| Shadow v2 projections | snapshot × arm | `ticker`, `record_id`, `subject_id` | 27–32k/run × 131 × 10 arms | yes |
| Three-arm contracts/games | run | `ticker` / `game_id` | 1,237 / 30 per run × 146 | yes |
| Player anatomy | run | `(game_id, player_id, stat, ticker)` | 282/game-run × 5,114 files | yes |
| Context captures (injuries, depth, weather) | run (3h) | sleeper `player_id`, ESPN `name`, `game_id` (weather) | 129 runs | weather yes; injuries by team only |
| `history/index.jsonl` | packet build | `handicap_run_id` | 152 | counts only |
| `ratings_snapshots.parquet` | season-week | `team` | 9,408 | no game id (team-week level) |
| `research_table.parquet` / `features_with_defense.parquet` | game (season, week) | `player_id`, `game_id` | 63,940 / 63,852 | yes (`game_id`, `opponent_team`, `home`) |
| `game_features.parquet` / `walkforward_predictions.parquet` | game | `game_id` | 4,175 / 3,151 | yes (home/away teams, scores, lines) |
| `schedule_cache.csv` | game | `game_id` | 7,548 | yes |
| Owner wagers / settlements | placed_at / settled_at | `source_bet_key` | 84 | `game` via ticker |
| Evaluations / CLV / autopsy | run (per game dir) | `prediction_id` | 5,680 / 47,617 / 18,416 per game | yes |

Team/QB profile metrics inside the packet are **not** a time series on any branch (current snapshot only).

### 7.2 Split-capable datasets (dimensions actually stored, with counts)

| dataset | dimensions | values / counts |
|---|---|---|
| silver `team_game` / packet team_profiles | offence vs defence; neutral-script (`*_ng`), early-down, red-zone, special teams; home/away (`is_home`) | 30 raw + 9 derived metrics × {season_split, recent_split (L6), long_baseline (L34)} |
| QB profiles | pressure state (`overall`, `under_pressure`, `clean_pocket`), recency (`recent_200_dropbacks`) | 8 rates each |
| sim scripts `team_volume.by_final_margin` | realised margin bucket {lead14+, lead7-13, within6, trail7-13, trail14+} | share_of_rows, pass_rate, pass_att, rush_att, plays |
| capture / ledger / v2 | `period` {FULL, 1H, 2H, 1Q, 2Q, 3Q, 4Q} | e.g. ledger FULL 37,379 / 1H 4,145 / 2H 2,112 / 4Q 1,642 / 2Q 1,625 / 3Q 1,624 / 1Q 1,602 |
| scorecards / evaluations | family, player statistic, contract value band, event probability band, disagreement band, model direction, horizon (T-24h/T-6h/T-90m/T-30m/latest), width band, liquidity band | `cumulative.scorecard.json.segments` 13 dimensions |
| board rows | horizon (5), env bands (spread band, total band), rung offset band, price bands, move band, role certainty | 67,485 rows wk1 |
| research_table | position {QB, RB, TE, WR}, home, qb_starter, zero_row, indoor | 63,940 |
| owner wagers postmortem | by_family (15), by_game (36), by_week (4), position lifecycle | 84 |

No stored home/away or surface/roof split of team metrics; derivable from `team_game` + schedule.

### 7.3 Market-history inventory
* Per-ticker quote series: **yes** — `data/kalshi/capture/<day>/<run>.quotes.jsonl` (change-suppressed:
  `changed=true` rows only; `state.json.fingerprints` holds last fingerprint per ticker, 59,230), granularity
  ≈12 min (3,623 runs in 30 days), retention: entire season append-only (18.7 GB so far, ≈0.6 GB/day).
  Books (2,500 tickers/run), trades (full prints), live in-game books (361 files).
* Per-horizon pre-aggregations: packet `markets[].movement` (9 horizons), board rows (5 horizons), v2
  `horizon_label`, evaluations `close_*`, `clv_v2` `horizon_*` vs `close_*`.
* 2025 history: candles (1-min and 60-min OHLC of bid/ask + OI + volume) and trades for 1,007 tickers of
  KXNFLGAME/SPREAD/TOTAL/TEAMTOTAL/player series; 61,557 archived market objects.
* Size: capture total 18.7 GB; a single ticker's series is small (≈100 bytes × ≤ a few hundred changes).

### 7.4 Projection inventory

| stream | per entity? | per market? | distribution or point | stored across runs | where |
|---|---|---|---|---|---|
| incumbent `shadow-0.4.0` | player (gsis) / team / game | yes (every listed ticker) | point probability (+ market-implied ladder means) | 131 runs, immutable | `data/shadow/ledger` |
| sim `sim-1.1.0` | player×stat, game | yes (6,219 contracts) | distribution (p05..p95, sd) + point; per-game environment quantiles | 70 runs | `data/shadow/sim` |
| Shadow v2 arms | subject (gsis/team/game) | yes (27–32k) | lattice distributions (summary) + p_yes | 131 snapshots × 10 arms | `data/shadow/v2/projections` |
| three-arm | game (margin/total) + contract | yes (1,237) | point per arm + sims (`n_sims 40000`) | 146 runs | `data/shadow/arms` |
| anatomy | player×stat | yes | `model_quantiles` + opportunity/efficiency means | per ledger run | `data/shadow/player_anatomy` |
| research 2025 | ticker | yes | point (`model_p`, `p_base/p_role/p_def/p_roledef`) | one-off | `research/kalshi_2025`, `research/model_vs_market`, `research/player_engine_v2/rung_scores_2025.parquet` (77,207) |

---

## 8. Existing ranking / percentile / league-average code
* `nfl_edge/handicap/teamprofile.py:145-155 league_ranks(profiles, key, split, higher_is_better)` → 1-based
  rank over teams for one metric/split (used in game md rendering).
* League mean baseline: `team_ratings.solve_ratings` (`mean` = weighted μ); `sim/features.py`
  `fit_priors` (frozen league means / position priors from fit seasons, `PriorSet`);
  `research/player_distributions.position_priors`; `features/opportunity.group_priors`.
* Percentiles: `autopsy.percentile` and `robust_z` per prediction (`nfl_edge/engines/player/autopsy_v2.py`,
  `nfl_edge/evaluation/research_record.py:186`); quantile banks in sim models (`np.quantile` 201-point
  grids); `LatticeDistribution` quantiles (`nfl_edge/engines/player/dist.py`).
* Rankings of markets: packet `largest_disagreements` (25), `largest_moves` (15), `tail_rungs` (10),
  `highest_liquidity_markets`, `game_priority_for_handicap` (`nfl_edge/handicap/packet.py`).
* Empirical-Bayes shrinkage of cell returns: `nfl_edge/research/board_miner.py:258`.
* No percentile-of-league computation for team or player metrics exists.

---

## 9. Size estimates (NFL, if the research layer exposed …)

Basis: 32 teams, ~16 games/week, ~1,800 player contexts (`contexts.json.gz player_contexts: 1,812`),
~380 skill players with markets, ~10.7k contracts/week.

| surface | per-entity bytes (measured analogue) | count | total |
|---|---|---|---|
| (a) team profiles (raw splits + adjusted + ranks + L6/L34 + QB + OL) | packet `team_profiles[team]` 1.5 KB + `quarterbacks` 1 KB + `offensive_line` 1 KB + ranks ≈ 5 KB | 32 | ≈160 KB |
| (b) player profiles (EWMA usage/efficiency, availability, depth, ladder summary) | anatomy row ≈1 KB × ~6 stats + `players[name]` packet block ≈2.2 KB (67 KB / 31 players) | ~400 with markets (1,812 contexts) | ≈1.3–4 MB |
| (c) per-game detail (matchup, injuries, weather, roles, script, sim projections, 700 market rows) | packet game ≈3.1 MB (markets 3.4 MB of which 2.5 MB is `movement`); without market rows ≈0.4 MB | 16/week | ≈6 MB/week slim, 50 MB full (= current packet `games`) |
| (d) metric time series (team: 22 adjusted + 30 raw per week; player: ~20 per game) | ≈100 B/point | 32×18×52 ≈ 30k team points + 400×17×20 ≈ 136k player points | ≈3 MB + 14 MB per season (needs a committed history that does not exist yet) |
| (e) market history | quote row ≈1 KB raw; compact point ≈60 B | 10.7k contracts × ~150 observed changes | ≈100 MB/week compact; raw 4–6 GB/week |
| evaluation/CLV/autopsy per game | 1.2 MB (eval) + 7.5 MB (clv) + 3 MB (autopsy) gz | 16/week | ≈190 MB/week (research only) |

Recommendation: lazy per-entity files; market history per event compacted to (captured_at, bid, ask, last,
vol, OI) deltas (~60 B/point).

---

## 10. Recommended research capabilities to expose in this pass

**Expose now (evidence supports, all keyed to app ids):**
1. `market_prices` + `market_price_history` (VERIFIED): per-event `market_history/<event_id>.json` built
   from capture quotes (forward-filled by fingerprint) — plus the packet's 9-horizon `movement` as a cheap
   summary. 2025 candles for last season's events.
2. `event_research` (PARTIAL): straight from `packet.json` per game — `matchup.pairs`, `team_profiles`
   (raw + adjusted), `quarterbacks`, `offensive_line`, `roles`, `injuries.records` (with diffs), `weather`,
   `game_script_inputs` (environment + team volume ranges), `simulation.player_projections` (quantiles,
   flagged RESEARCH), `shadow_v2` summary, `largest_moves/disagreements`. Mark projections `research_only`.
3. `team_profiles` + `team_metrics` + `matchup_metrics` + `opponent_adjustment` (PARTIAL): current-week
   values with `quality.limitations: ["current snapshot only; no committed history"]`, basis/`n_games`.
4. `injuries` (VERIFIED capture), `weather` (VERIFIED capture), `lineups` (PARTIAL, Sleeper depth chart).
5. `historical_results` + `opponents` + `schedules` (VERIFIED): from `schedule_cache.csv` (scores 1999–2026).
6. `clv`, `historical_accuracy`, `calibration`, `wager_history` (VERIFIED research): owner postmortem +
   scorecards (already partly exported); per-event evaluation summaries.
7. `player_props`, `team_props`, `game_markets` (VERIFIED market side).
8. `projection_distributions` / `raw_projections` (PARTIAL, research_only): sim quantiles + incumbent
   points per market, with support states.

**Mark RESEARCH:** `player_metrics`, `player_game_logs`, `usage`, `advanced_stats` history (committed only
as 2016–2025 study parquets; 2026 values are not in a table), `shocks`, three-arm/v2 arm probabilities,
hypotheses/localized signals.

**Mark PARTIAL with explicit reason:** `team_game_logs` (silver table rebuilt per run, not committed →
expose only the last-34-game baseline numbers and the `ratings_snapshots` 2009–2025 history),
`recent_form_windows` (fixed L6/L34, no L3/L5), `time_series` for team metrics (only once a committed
per-week ratings file exists — recommend adding `team_ratings` output to `market-data` each run).

**UNAVAILABLE:** `schedule_strength`, `venue_effects` (attributes only), `play_by_play` (bronze not
committed), OL quality metrics, situational home/away split tables (derivable but not stored).

---

## 11. Open questions / UNKNOWN items

1. **Silver tables are not on any branch** — verified by `git ls-tree` of all three data branches (no
   `data/silver/`), `.gitignore` on main, and `scripts/ci/refresh_market_data_worktree.sh`/workflows
   rebuilding them. Whether a 2026 `team_game` history can be recovered exactly depends on nflverse pbp
   (deterministic rebuild) — UNKNOWN only in the sense that no committed artifact proves past values.
2. **Kalshi→GSIS resolution rate**: only observable per run (549 map entries; 862 vs 360 ids in the ledger
   include non-slate players). The map parquet itself is not committed; exact unresolved list UNKNOWN.
3. **`app/latest`** was absent from the extracted `handicap-reports` tree at `cc75aa23` although the README
   and `docs/APP_EXPORT.md` describe it (real-data proof dated 2026-10-02) — likely published after this
   commit; not re-fetched (disk budget).
4. **Actions artifacts** (90-day history of every packet) were not inspected (outside the repo); they are
   the only history of team/QB profiles.
5. `data/shocks/*.jsonl` are empty on both main and market-data; whether the shocks pipeline runs in 2026 is
   UNKNOWN (`research/shocks/shocks_2025.parquet` is the only data).
6. The three-arm `DATA_ONLY` artifact (`research/three_arm/data_only_artifact_2026.json`) and v2
   `research/` corpus (1.08 GB) were not opened beyond inventory.
7. Row counts for the full quote history (~27M) are extrapolated from one run × 3,613 manifests; a precise
   count would require streaming 10.5 GB (not done under the disk budget).
8. Checks performed: `git ls-tree -r -l origin/market-data` (62,037 blobs summed), `git archive` of 109
   representative files, polars/json measurement of every committed research parquet and the extracted
   files (commands and outputs recorded in the session transcript; key numbers reproduced above).

