# Phase 2 research-data audit — NBA (`chmoses98/nba-edge-finder`)

Audited 2026-10-03 against `main` @ `b7b3417` (checkout `the repository checkout`), `origin/data-archive` @ `4121d07`
(2026-10-03T06:07:10Z, shallow fetch, extracted to `scratchpad/phase2/nba/data-archive/`) and `origin/accounting-data` @ `861932e`
(extracted to `scratchpad/phase2/nba/accounting-data/`). Read-only; every count below was produced by a command run against the real
files (pyarrow 25 / polars 1.44; pandas 3.0.6 is installed but imports too slowly to be usable here).

The previous phase's picture ("data-archive only; stats ingestion unknown; first simulate pending") was materially incomplete: the
`main` branch carries a committed three-season ESPN box-score + shot-event history (64 MB), a committed Kalshi settled-market +
hourly-candle history for 2025-26, a point-in-time feature builder with an iterative opponent adjustment, and a frozen research
baseline. The first `simulate` of 2026-27 has now run (three times on 2026-10-03), but produced no usable market-relative output
because of a quote-projection defect (section 5.14).

---

## 1. Summary

1. **Strongest asset — historical game logs.** `data/history/espn/` on `main` holds 3 seasons (2023-24, 2024-25, 2025-26): 4,160
   games, 8,320 team-game rows (25 cols), 112,427 player-game rows (33 cols), 927,349 shot events (29 cols, ~80 % with court
   coordinates and zones). Zero nulls in the box-score tables; `season_type` labelled on every row. Keyed by `espn:<id>` game ids,
   NBA-com team ids, and negated-ESPN player ids — the same keys `src/nba_edge/app_export.py` emits.
2. **Second asset — market history.** `data/history/kalshi/`: 137,059 settled 2025-26 NBA markets across 13 series, plus
   653,308 hourly candle rows for 23,881 tickers in 8 series (moneyline, spread, total, team total, pts/reb/ast/3pt). A derived
   research table (`data/research/market_table.parquet`, 68,235 rows) joins executable prices at 5 pre-tip horizons to outcomes.
3. **Feature layer is real but computed on the fly.** `features/build.py` computes EWM-shrunk pace/ORtg/DRtg with an iterative
   6-pass opponent adjustment (`opponent_adjusted_ratings`), player minutes/role/rotation profiles and per-minute rates. Nothing
   stores these as a series; they exist only inside each run's `slates/.../packet.json`.
4. **Model status is honest and negative.** Eight market families walk-forward tested; the raw Kalshi price beats the model in all
   eight; authority is RESEARCH everywhere; parameters frozen under `NBA_BASELINE_2026_PRESEASON_V1` (digest-pinned by test).
5. **Prospective archive is thin and pre-season.** `data-archive` has 16 days of context snapshots (schedule/rosters/injuries,
   47 each), 18 market checkpoints + 25 15-minute deltas, 18 order-book checkpoints, and one simulated preseason game. No
   boxscores, settlements or evaluations have ever been written to it.
6. **Weakest areas:** no lineup/stint/on-off/tracking data (all cloud-blocked, documented in two source audits); no official injury
   PDF ever captured (ESPN injuries only; `nba_id` null on every injury row); player registry covers only 175 prop-market players
   (40.6 % of 2025-26 played rows); shot events key teams by ESPN ids (1–30) while every other table uses NBA ids.
7. **Defect found:** `workflows/simulate.py:290` strips the captured `*_dollars`/`_quote_cents` quote fields before normalisation, so
   all 73 contracts of the first real slate carry `p_market = null`, `p_production = null`, and the app export has 0 model prices.
8. **`data-archive` was force-updated** between the previous phase's fetch (`927c49b`) and now (`4121d07`); the branch's own
   `manifest.jsonl` (238 entries) is internally consistent, but I could not verify that no earlier partitions were lost (shallow fetch).

---

## 2. Data branches and layout

| branch | root | contents | immutable vs replaced | size |
|---|---|---|---|---|
| `main` (`b7b3417`) | `data/` | `history/espn/*.parquet` (3 seasons × {team_games, player_games, shot_events}), `history/kalshi/{markets,candles}_*.jsonl.gz`, `identity/{teams.csv,players.jsonl,kalshi_players.json}`, `research/*.parquet`, `catalog/`, `fixtures/` | **Replaced whole-file** on each manual pull (`history.yml`, `shot_events.yml`, `kalshi_history.yml` rewrite the parquet/gz and commit to the triggering branch; `done_events_*.json` make re-runs incremental). Dedup key `(game_id, team_id[, nba_id])` (`data/history.py:433-434`). Git history preserves prior versions. | `data/` = 64 MB (espn 16.9 MB, kalshi 21.2 MB, research 26.2 MB) |
| `data-archive` (`4121d07`, orphan) | `/` | `manifest.jsonl` (238 entries, `merge=union`), `context/{schedule,injuries,rosters}/dt=*/`, `kalshi/{markets,markets_delta,orderbooks,orderbooks_delta}/dt=*/`, `predictions/`, `contracts/`, `matchup/context/`, `slates/{dt=*,latest}/`, `app/latest/`, `STATUS_*.json`, `LEASE_capture.json`, `EVIDENCE_HEALTH.json`, `catalog/` | Ledger partitions are **append-only** (`Ledger.append_rows` refuses overwrite; sha256 per file; `Ledger.verify()` before every push via `scripts/archive_push.sh`). `slates/latest/`, `app/latest/`, `STATUS_*`, `LEASE_*`, `EVIDENCE_HEALTH.json` are **replaced** pointers. `slates/dt=*/<run>/` are not in the manifest (12 files on disk not manifested). | 15 MB extracted (kalshi 6.0 MB, app 4.9 MB, slates 1.3 MB, context 1.2 MB) |
| `accounting-data` (`861932e`, orphan) | `data/accounting/` | `wagers.jsonl`, `settlements.jsonl` | append-only by contract (`routed_ledger`) | **both files 0 bytes** |

Manifest kinds on `data-archive` (from `manifest.jsonl`):

| kind | files | rows | first obs | last obs | distinct runs |
|---|---:|---:|---|---|---:|
| context/schedule | 47 | 422 | 2026-09-18T07:35Z | 2026-10-03T04:07Z | 44 |
| context/rosters | 47 | 26,769 | same | same | 44 |
| context/injuries | 47 | 3,389 | same | same | 44 |
| kalshi/markets (checkpoints) | 18 | 63,116 | 2026-09-18T07:36Z | 2026-10-03T06:04Z | 18 |
| kalshi/markets_delta | 25 | — | 2026-09-28T03:03Z | 2026-10-03T05:49Z | 3 |
| kalshi/orderbooks (checkpoints) | 18 | 5,100 | 2026-09-18 | 2026-10-03 | 18 |
| kalshi/orderbooks_delta | 25 | — | 2026-09-28 | 2026-10-03 | 3 |
| predictions | 3 | 219 | 2026-10-03T04:07Z | 2026-10-03T06:07Z | 1 |
| contracts | 3 | 219 | same | same | 1 |
| matchup/context | 5 | 5 | 2026-10-03T00:03Z | 2026-10-03T04:07Z | 2 |
| boxscores / settlements / evaluations | **0** | — | — | — | — (`EVIDENCE_HEALTH.json`: "settle has not run", "evaluate has not run") |

Capture cadence actually observed: one checkpoint per day 09-19→10-02 (off-season daily futures snapshot), 3 on 09-18 and 5 on 09-27
(delta benchmark), then on 10-03 a live worker (game inside the 8 h window) writing 15-minute deltas (24 in the chain, then a
checkpoint at the 24-delta limit — `STATUS_capture.json: "chain reached 24 deltas (limit 24)"`). Tiers in code:
`worker/plan.py:52-55` (15 min / 10 min / 7 min / 5 min inside T-30m).

Producing workflows (`.github/workflows/`):

| workflow | trigger | writes |
|---|---|---|
| `capture_worker.yml` | self-dispatching ~5 h chain; `*/5` cron bootstrap only | `data-archive`: context, capture (+deltas, order books cap 300), simulate, settle, evaluate, app-export |
| `conductor.yml` | manual / `.trigger/conductor` push (schedule removed) | same, recovery path |
| `history.yml` | manual / `.trigger/history` | `data/history/espn/{team,player}_games_*.parquet` → commit to triggering branch |
| `shot_events.yml` | manual | `data/history/espn/shot_events_*.parquet` |
| `kalshi_history.yml` | manual / `.trigger/kalshi_history` | `data/history/kalshi/*` |
| `probe.yml`, `source_probe.yml`, `shot_coordinate_probe.yml` | manual | `data/catalog`, `docs/probe` |
| `ci.yml` | push/PR (not `data-archive`) | ruff + pytest (629 tests in 46 files) |

Nothing that feeds the research tables runs on a schedule; the ESPN history was last pulled 2026-09-19 (box scores) and 2026-09-29
(shot events), Kalshi history 2026-09-19.

---

## 3. Identity model

| entity | canonical id | where defined | coverage | notes / collisions |
|---|---|---|---|---|
| Team | NBA.com `team_id` (1610612737…66), `tricode`, `alt_tricodes` | `data/identity/teams.csv` (30 rows); `identity/teams.py` (`by_tricode`, `by_id`, `by_name`) | 30/30 | **Shot events use ESPN team ids 1–30** (`shot_events_*.parquet.team_id`, `opponent_team_id`; `shotprofile/events.py:217` takes `t["id"]` raw). No committed ESPN→NBA team map; derivable from any `context/rosters` snapshot (`espn_team_id` → `team_abbreviation`, which resolves through `alt_tricodes`: NO, GS, NY, SA, UTAH, WSH). I derived the full 30-entry map from the 2026-10-03 roster snapshot. `shot_profile_pit_features.team_id` inherits the ESPN scheme. |
| Player | `nba_id` = **−ESPN athlete id** (provisional convention, `data/history.py:9`, `identity/players.py:3`) | `data/identity/players.jsonl` (175 records: `aliases.espn`, `aliases.kalshi`, `aliases.kalshi_uuid`; `team_id` null on all 175); `data/identity/kalshi_players.json` (175 Kalshi uuids → name, tricodes, n_markets) | History: 934 distinct `nba_id` (881 with ≥1 played game). Registry: 175 (all present in 2025-26 player_games; cover 40.6 % of 2025-26 played rows). 2026-10-03 rosters: 603 players, 172 in registry, 506 with ≥1 historical game. | No positive NBA person ids anywhere. Injury rows carry `nba_id = null` on all 61 latest rows (name-matched later in `simulate`). App export emits `nba_player_id = nba_id` and `espn_athlete_id` in `source_ids`. |
| Game | `game_id = "espn:<event id>"` | `data/history.py:177`; schedule rows; predictions | 4,160 historical + 45 scheduled | App export `event_id = espn_event_id(<numeric>)`. Kalshi event tickers (`KXNBAGAME-26OCT03MIATOR`) are joined to games by (ET date, home, away) in `workflows/simulate.py:179 markets_for_game`. |
| Kalshi market | `ticker`; `event_ticker`; `series_ticker`; `custom_strike.basketball_team/_player` uuids | `kalshi/ticker.py`, `kalshi/contracts.py`, `data/catalog/market_ontology.yaml` (63 families) | `kalshi_team_uuids.json`: 32 team uuids (note: its `name` field is the most frequent market title, e.g. "Paolo Banchero: 4+" for ORL — cosmetic) | Player uuid → `nba_id` via `players.jsonl.aliases.kalshi_uuid` (175). |
| Season | `"2023-24"` strings; `season_type ∈ {preseason, regular, playin, playoffs, other}` from ESPN `season.type` | `data/history.py`, `shotprofile/population.py` | all history rows | Research default population excludes preseason (`RESEARCH_SEASON_TYPES`). |

Keys used by `src/nba_edge/app_export.py` (`docs/APP_EXPORT.md` "Identity"): `nba_team_id` from `teams.csv`, `nba_player_id` =
registry `nba_id` (negative), `espn_event_id`. These match `team_games`/`player_games`/`market_table` exactly; only `shot_events`
(ESPN team ids) needs the derived map.

---

## 4. CAPABILITY MATRIX

Status vocabulary per spec: VERIFIED requires scheduled production generation + tests + committed history + stable semantics in code.
No research dataset here is refreshed on a schedule, so nothing historical can exceed PARTIAL; the prospective ledgers are scheduled but
have days, not seasons, of rows.

| capability | status | origin | coverage | cadence | tests | notes |
|---|---|---|---|---|---|---|
| Team metrics (pace, ORtg/DRtg, OREB%, TOV/poss, rest, b2b) | PARTIAL (computed, not stored) | `features/build.py:129-161`, `:93-126` from `team_games` | derivable for any cutoff 2023-10→2026-06 | on demand / per simulate run | `tests/test_features_pit.py` (5) | Only persisted per run in `slates/*/packet.json` `home/away` blocks; never as a series |
| Player metrics (min, FGA/min, 3PA share, FG2/FG3/FT%, FTA/FGA, ast/oreb/dreb/stl/blk/tov per-min weights, p_start, p_rotation) | PARTIAL (computed, not stored) | `features/build.py:180-275`, `sim/rotation.py:210` | all 881 played players | per simulate run | `test_features_pit.py`, `tests/test_rotation.py` (14) | stored only in packet `players[]` per run |
| Game logs — team per game | PARTIAL | `data/history/espn/team_games_{season}.parquet`; `data/history.py:317-350`; `history.yml` | 8,320 rows, 4,160 games, 2023-10-05→2026-06-13 | manual pull (last 2026-09-19) | `tests/test_history.py` (15), `tests/test_season_type.py` (17) | 25 cols incl. q1–q4, ot_pts, fga/fta/oreb/tov, possessions; 0 nulls |
| Game logs — player per game | PARTIAL | `player_games_{season}.parquet`; `data/history.py:291-315` | 112,427 rows; 934 players | manual | same | 33 cols incl. minutes, started, played, dnp_reason, plus_minus |
| Historical opponents / results | PARTIAL | `team_games.opp_team_id, won, margin, total, home` | every row | manual | `test_history.py` | opponent on every team-game row |
| Opponent adjustments | PARTIAL | `features/build.py:93-126` (iterative, EWM, shrunk) | computed on demand | per run | indirect (`test_features_pit.py:29`) — no test of the adjustment itself | see section 6 |
| Schedule strength | PARTIAL (implicit) | same function (`eff_n`, opponent terms) | — | — | none direct | no explicit SOS output |
| Recent-form windows | PARTIAL (parameterised EWM) | `BuildConfig`: team hl 25 g, player minutes hl 5, rates hl 20, role_window 5, rotation_window 20 | — | — | `test_features_pit.py:43` | L3/L5 not stored; trivially derivable from game logs |
| Usage — minutes, FGA, FTA | PARTIAL | `player_games.minutes/fga/fta` | 112,427 rows | manual | `test_history.py` | no possessions-per-player, no usage % column |
| Lineups / depth charts / rotations | UNAVAILABLE (lineups) / PARTIAL (rotation estimate) | rosters snapshots (`context/rosters`, 603/snapshot); `estimate_profile`; `matchup/context.expected_*_rotation` = full roster | 47 roster snapshots | capture cadence | `test_rotation.py`, `tests/test_matchup_v2.py` | `EVIDENCE_HEALTH.context.lineups: absent`, `confirmed_starters: absent`; stats.nba/cdn.nba blocked (`docs/research/MATCHUP_SOURCE_AUDIT.md:54-59`) |
| Injuries / availability | PARTIAL | `context/injuries` (ESPN `/injuries`), `data/injuries.py`; p_play map `build.py:34` | 47 snapshots, 3,389 rows, 16 days | capture cadence | `tests/test_injuries_parse.py` (4) | official NBA PDF **never captured** (`missing: true` on every snapshot, `slots_tried: 8`); `nba_id` null on all rows |
| Matchup metrics | RESEARCH (neutral) | `matchup/*` (`MATCHUP_AWARE_V2`, `effects_version = neutral-0`) | 5 context rows | per run | `test_matchup_v2.py` (48), `test_matchup_events.py` (18) | every adjustment is the identity by construction |
| Projection distributions | PARTIAL | `slates/dt=*/<run>/packet.json` → `games[].sim.{margin,total,home_pts,away_pts,first_half_total,first_quarter_total}.q5/q25/q50/q75/q95`, `margin_ladder`, `total_ladder`; per-player `sim.{min,pts,…}` quantiles | 1 game, 3 runs (40,000 draws) | per simulate run | `tests/test_sim_engine.py` (28), `test_pricing.py` (16) | raw draws not stored |
| Raw projections (point) | PARTIAL | packet `players[].sim.*.mean`, `slate.games[].margin_mean/total_mean/p_home_win` | 1 game | per run | same | — |
| Market prices (current) | PARTIAL→VERIFIED-track | `kalshi/markets` checkpoints + deltas; `archive/reconstruct.latest_board` | 4,289 markets (567 game/player-family tickers quoted) | 15/10/7/5 min in window, daily otherwise | `tests/test_capture.py`, `test_delta_archive.py` (30), `test_delta_reconstruct.py` (24) | scheduled, tested, 16 days of rows — but pre-season only |
| Market price history (per ticker) | PARTIAL | (a) `data/history/kalshi/candles_*.jsonl.gz` hourly; (b) archive checkpoints + deltas | (a) 653,308 rows / 23,881 tickers / 2025-10-21→2026-06-14; (b) 18 checkpoints + 25 deltas | (a) manual; (b) worker | `tests/test_kalshi_history.py` (13), `test_market_table.py` (7) | see section 7.3 |
| Advanced stats (EPA/xG etc.) | PARTIAL (basketball analogues only) | possessions (`data/history.py:278`), ppp, shot zones/distances (`shotprofile/court.py`), zone rates (`shot_profile_pit_features`) | 927k shots | manual | `tests/test_shot_profile.py` (60) | no on/off, RAPM rejected (`docs/research/INJURY_IMPACT.md`) |
| Situational splits | PARTIAL | columns `home`, `season_type`, `n_ot`, `started`, `is_home`, `period` | all rows | — | — | computed nowhere; derivable |
| Player props | PARTIAL (historical settled) / RESEARCH (model) | `markets_KXNBA{PTS,REB,AST,3PT}.jsonl.gz` (80,582 settled), `market_table` | 2025-11-19→2026-06-14 | manual | `test_market_table.py`, `tests/test_market_vs_model.py` | PRA series empty (0 rows) |
| Team props | PARTIAL | `markets_KXNBATEAMTOTAL` 9,486 (from 2026-02-10) | — | manual | — | — |
| Game-level markets | PARTIAL | `markets_KXNBA{GAME,SPREAD,TOTAL,1H*}` 46,721 settled; live board | — | manual / worker | `tests/test_contracts.py`, `test_contracts_real.py`, `test_ontology.py` | — |
| Play-by-play | PARTIAL (shots only) | `shot_events_*.parquet` (ESPN summary PBP, shooting plays only) | 927,349 events, 4,160 games | manual (2026-09-29) | `test_shot_profile.py`, `test_season_type.py` | non-shooting plays not ingested |
| Weather | UNAVAILABLE | — | — | — | — | indoor sport; nothing in repo |
| Venue / park effects | PARTIAL (field only) | `context/schedule.arena`, `neutral_site`; `home_ppp_edge` constant | 45 scheduled games | — | — | no venue table; history has `home` only |
| Calibration data | PARTIAL (research) | `docs/research/market_calibration.json`, `sim_calibration.json`, `market_table_scored.parquet` (16,091 scored rows) | 2025-26 | one-off | `test_market_vs_model.py` (4), `tests/test_metrics.py` (10) | — |
| Historical accuracy / postmortems | RESEARCH | `docs/research/*.md` + `wf_*.csv` (300 games), `prop_walk_forward_v3.csv` (7,666 rows) | late 2025-26 | one-off | — | all negative vs market |
| CLV | UNAVAILABLE (prospective) | `evaluation/clv.py` functions exist; `evaluate` never ran; app export `clv.available=false` | 0 rows | — | `tests/test_clv.py` (5) | — |
| Historical wager outcomes | UNAVAILABLE | `accounting-data` wagers/settlements 0 bytes | 0 | router-driven | `tests/test_routed_accounting.py` (12) | — |
| Identity tables | PARTIAL | section 3 | 30 teams / 175 players | manual (`scripts/build_player_identity.py`) | `tests/test_contracts_real.py` | — |
| Schedules | PARTIAL | `context/schedule` (10-day look-ahead) | 45 preseason games 2026-10-03→10-13 | capture cadence | `tests/test_workflows.py` | regular season (2026-10-20→) not yet visible |
| Seasons covered | — | 2023-24, 2024-25, 2025-26 (history); 2026-27 preseason (archive) | — | — | — | — |
| Season-long sim (win totals, seeds) | RESEARCH | `sim/season.py` (`season-sim-0.1.0`) | — | — | `tests/test_season_sim.py` (4) | not wired to any ledger output I could find |

---

## 5. Detailed findings per category

### 5.1 Team game logs — `data/history/espn/team_games_{2023-24,2024-25,2025-26}.parquet`
- Rows 2,766 / 2,780 / 2,774 (8,320); 25 columns: `game_id, game_date_et, start_time_utc, season, season_type, team_id, opp_team_id,
  home, pts, opp_pts, q1..q4, ot_pts, n_ot, won, margin, total, fga, fta, oreb, tov, possessions, totals_source`. Null % = 0 on every column.
- Season types per season (team rows): preseason 128/138/130, regular 2,462 each, play-in 12 each, playoffs 164/168/170.
- Possessions = `fga + 0.44·fta − oreb + tov` (`data/history.py:278`); `totals_source` records whether team totals came from
  ESPN team statistics or summed player rows.
- Producer: `nba history` (`data/history.py:459 run_history_pull`) via `history.yml`; `MANIFEST.json` `pulled_at 2026-09-19T00:37:29Z`,
  `n_errors 0`. Files are rewritten whole each pull (dedup by `(game_id, team_id)`); the prior version lives only in git history
  (`3c7b2f9`, `2806bb0` commits on `main`).
- Tests: `tests/test_history.py` (15: parsing, dedupe, possessions, non-NBA opponent skip), `tests/test_season_type.py` (17).

### 5.2 Player game logs — `player_games_{season}.parquet`
- Rows 37,445 / 37,836 / 37,146 (112,427); 33 columns (`nba_id, player_name, started, played, minutes, pts, reb, ast, fg3m, stl, blk,
  tov, fga, fta, fgm, oreb, dreb, fg3a, ftm, pf, plus_minus, dnp_reason, team_pts, opp_pts, n_ot` + game meta). `dnp_reason` null on
  80.2–82.3 % (the rows that played); everything else 0 % null.
- 934 distinct `nba_id`, 881 with ≥1 `played` row; 30 teams; `nba_id` are negative ESPN athlete ids.
- Semantics stable: columns defined at `data/history.py:55`; parser `parse_espn_summary_extended` (`:216`).

### 5.3 Shot events — `shot_events_{season}.parquet` (schema `shotevent/2`, ingest `espn-pbp/1`)
- Rows 305,420 / 308,339 / 313,590 (927,349); 29 columns; `event_id` unique; `period` 1–6; coordinates `x,y,distance_ft` null on
  19.7 / 19.7 / 21.0 % (free throws + unlocated); `shooter_name` 100 % null (by design, id only); `espn_season_type` 100 % null on
  backfilled rows (documented, `docs/research/SEASON_TYPE.md`).
- Zones: rim, paint_non_rim, midrange, corner_three, above_break_three, unknown (`shotprofile/court.py`); located 78.96–80.31 %
  per `SHOT_EVENTS_MANIFEST.json`.
- **Team ids are ESPN (1–30)**, players are −ESPN ids (consistent with player_games).
- Producer `nba shot-events` via `shot_events.yml` (manual); pulled 2026-09-29. Tests: `tests/test_shot_profile.py` (60),
  `tests/test_season_type.py`.

### 5.4 Kalshi history — `data/history/kalshi/`
Settled markets (`markets_<SERIES>.jsonl.gz`, status `finalized`, 46 raw Kalshi fields + `_source`):

| series | rows | events | close range | results |
|---|---:|---:|---|---|
| KXNBAGAME | 2,898 | 1,449 | 2025-04-16 → 2026-06-14 | yes 1,446 / no 1,446 / scalar 6 |
| KXNBASPREAD | 16,937 | 1,324 | 2025-10-22 → 2026-06-14 | yes 5,417 / no 11,488 / scalar 32 |
| KXNBATOTAL | 14,787 | 1,324 | 2025-10-22 → | yes 7,456 / no 7,298 / scalar 33 |
| KXNBATEAMTOTAL | 9,486 | 529 | 2026-02-10 → | yes 5,008 / no 4,478 |
| KXNBAPTS | 23,562 | 1,096 | 2025-11-19 → | yes 8,483 / no 14,074 / **scalar 1,005** (DNP) |
| KXNBAREB | 22,656 | 1,069 | 2025-11-19 → | scalar 960 |
| KXNBAAST | 17,745 | 1,056 | 2025-11-19 → | scalar 665 |
| KXNBA3PT | 16,619 | 1,029 | 2025-11-19 → | scalar 645 |
| KXNBA1HSPREAD / 1HTOTAL / 1HWINNER | 5,823 / 4,707 / 1,569 | 523 each | 2026-02-11 → | — |
| KXNBAWINS | 270 | 30 | 2026-03-19 → 04-13 | — |
| KXNBAPRA | **0** | — | — | — |

Candles (`candles_<SERIES>.jsonl.gz`, `period_interval: 60` minutes, keys `end_period_ts, price{open,high,low,close,mean,previous},
yes_bid{…}, yes_ask{…}, volume, open_interest`): GAME 174,132 rows / 2,898 tickers (median 58, max 266 per ticker);
SPREAD 134,052 / 5,574; TOTAL 58,656 / 2,228; TEAMTOTAL 32,020 / 1,403; PTS 79,458 / 3,519; REB 70,180 / 3,284; AST 54,071 / 2,599;
3PT 50,739 / 2,376 (median ≈ 20 candles per prop ticker). No candles for 1H series or WINS. `price.close` is frequently null
(no trade in the hour), so bid/ask is the usable series (`research/market_table.py` header). Pulled 2026-09-19 (`MANIFEST.json`:
3,290 requests, 137,059 markets). Tests: `tests/test_kalshi_history.py` (13).

### 5.5 Research tables — `data/research/`
- `market_table.parquet`: 68,235 rows × 38 cols; 1,387 games, 16,694 tickers, 8 families (game_winner 13,760; game_spread 9,983;
  game_total 8,515; team_total 2,866; player_points 10,443; rebounds 9,550; assists 6,982; threes 6,136); horizons T-24h 5,701,
  T-6h 14,503, T-90m 15,850, T-30m 16,090, final 16,091; `outcome` populated on 100 %; `player_id` on 33,111 rows, `team_id` on 59,720;
  executable `exec_yes_cents`/`exec_no_cents` + analysis-only mid. Dates 2025-04-19→2026-06-13 (season 2024-25: 791 rows).
- `market_table_scored.parquet`: 16,091 rows (horizon `final`), `p_data_only` 100 % populated, `model_version nba-sim-0.1.0`.
- `shot_profile_pit_features.parquet`: 85,846 rows (4,160 games, 872 players, 30 ESPN team ids), per player-game point-in-time
  zone rates `p_*`, opponent-allowed `o_*`, differences `d_*`, prior attempt counts; `_clean` = 80,404 (preseason removed).
- `minutes_validation.parquet`: 3,676 rows (150 games, 476 players, 2026-03-25→04-12): actual vs EWM5/old/new minutes with 80 % bands.
- Tests: `tests/test_market_table.py` (7), `tests/test_market_vs_model.py` (4), `tests/test_features_pit.py`.

### 5.6 Feature set the model computes (`src/nba_edge/features/build.py`, `FEATURE_VERSION = features-0.1.0`)
Team (`team_params`, `:129`): EWM pace per 48 (`hl=25` games, prior 4 games to league), off/def ppp (opponent-adjusted, see §6),
`rating_sd = clip(0.045/√(1+eff_n/10), 0.012, 0.045)`, OREB% (clip 0.15–0.40), TOV/poss (clip 0.08–0.20), `rest_days_for` (`:305`),
`b2b = rest ≤ 1`. Player (`player_params`, `:180`): rotation membership from the team's last 10 games; `p_play` from injury status
(OUT 0, DOUBTFUL 0.15, QUESTIONABLE 0.5, PROBABLE 0.85) or participation; minutes mean/sd (hl 5, last 5 games ×1.5);
`p_start`; rotation profile (`sim/rotation.py:210 estimate_profile`, 20-game window, hl 6, `ROTATION_MIN = 10`);
per-minute FGA/AST/OREB/DREB/STL/BLK/TOV (hl 20, 60 prior minutes); three_share, fg2/fg3/ft %, FTA/FGA (fixed pseudo-counts).
League rates (`league_rates`, `:82`): mean pace48 and ppp over prior team-games. Declared but unused/unestimated: `impact_ppp`
(always 0.0 — R1 blocker), `usage_elasticity`, `rest_days` in the engine (`docs/SIMULATION.md` L2, "Unused parameters").
Shot-profile features (`shotprofile/features.py`): zone rate/efficiency with 100/150 pseudo-attempt shrinkage and 45-day half-life —
**research only, neutral in production**.

### 5.7 Simulation inputs / priors
`sim/params.py LEAGUE` constants (27 values; `docs/SIMULATION.md §5`) are era priors retuned once from `research/calibrate_sim`
(`docs/research/RESEARCH_NOTES.md §1`: margin sd 16.0, total sd 20.0, OT 4.8 %). All frozen in
`docs/baseline/NBA_BASELINE_2026_PRESEASON_V1.json` (families: feature_builder, league_constants, player_defaults, pricing,
rotation_model; `tests/test_baseline_freeze.py` pins the digest `5a4cbda0…`). Historical seasons used for training/calibration:
2023-24 + 2024-25 tune, 2025-26 holdout for `rating_scale = 1.9` (`build.py:51-58`, `docs/research/RATING_SCALE.md`).

### 5.8 Context ledgers (prospective)
- `context/schedule`: 47 snapshots, 422 rows, **45 distinct games, all preseason, 2026-10-03→10-13** (10-day look-ahead,
  `data/context.py:55`). Fields: `game_id, season, season_type, game_date_et, start_time_utc, actual_tip_utc, home/away_team_id,
  tricodes, status, arena, neutral_site, source`.
- `context/rosters`: 47 snapshots × ~603 players (`espn_team_id, team_abbreviation, espn_athlete_id, full_name, position, jersey,
  status, injuries[]`).
- `context/injuries`: 47 snapshots; latest 61 rows (46 questionable, 15 out); `source espn_injuries` on every snapshot; official PDF
  `missing: true` on every snapshot since 2026-09-18 (`STATUS_context.json`); `nba_id` null on 61/61 (resolved by name at simulate time).

### 5.9 Market ledgers (prospective)
- Latest board (`kalshi/markets/dt=2026-10-03/…T060410Z`): 4,289 active markets, 264 series; by support BUILDABLE 38, MODELABLE 192,
  RESEARCH 1,484, UNMODELABLE 2,035, UNRESOLVED 540 (six series unknown to the ontology — alarm raised). Game/player families on
  the board: 567 tickers (game_spread 75, game_total 63, game_winner 54, period_* 31, game_unknown 30, player_unknown 314 — the
  314 are preseason player markets not yet resolvable). All 567 carried a quote in ≥1 checkpoint.
- Each row = raw Kalshi market dict + `_quote_cents{yes_bid,yes_ask,no_bid,no_ask,last_price}`, `_family`, `_support`,
  `_observed_at_utc`, `_run_id` (`archive/capture.py:63`).
- Deltas (`kalshi/markets_delta`, schema with `base_path, base_sha256, seq, added, changed, removed, cleared, board_sha256`): 25 files,
  1,792 tickers touched, median 2 touches; example `KXNBAGAME-26OCT03MIATOR-TOR`: 4 checkpoint observations + 24 delta touches on
  10-03. Reconstruction via `archive/reconstruct.py` (field-for-field verified, `docs/research/DELTA_ARCHIVE.md`).
- Order books: 18 checkpoints, 5,100 rows, 906 tickers, cap 300 books per capture prioritised by support/quote (`capture.py:80`);
  levels `yes/no: [[cents, qty], …]` + raw.

### 5.10 Predictions / contracts / slates (prospective)
- `predictions` rows (`schemas/prediction.py ContractPrediction`): `prediction_id, ticker, game_id, family, predicted_at_utc,
  data_cutoff_utc, model/sim/feature_version, n_sims, p_data_only, p_market, p_hybrid, p_production, p_data_only_se,
  market_{yes,no}_{bid,ask}, market_observed_at_utc, gate, gate_reasons, authority, support, pregame, thesis_group, input_snapshot_ids`.
  3 runs × 73 rows for `espn:401902644` (MIA @ TOR, preseason): gates CANNOT_TRUST_INPUTS 72 ("injury source is espn not official
  report"; "preseason game"), UNSUPPORTED 1; families game_spread 25, game_total 21, period_spread 11, period_total 11,
  period_winner 3, game_winner 2.
- `slates/<run>/packet.json`: per game `game, warnings, league_rates, home/away {team_id, tricode, pace, off_ppp, def_ppp, rest_days,
  b2b, rating_sd, players[15]{nba_id, name, p_play, p_start, minutes_mean/sd, fga_per_min, three_share, fg2/fg3/ft_pct, games_used,
  sim{min,pts,…: mean,q5,q25,q50,q75,q95}}}, sim{margin,total,home_pts,away_pts,first_half_total,first_quarter_total: quantiles;
  ot_rate; p_home_win; margin_ladder; total_ladder}, injury_report_rows, contracts`. This is the only place team/player metrics
  are persisted — one JSON per run, not in the manifest.

### 5.11 App export (`app/latest/`, `edge_finder.app.v1`)
Reads only `context/schedule` (latest row per game), the reconstructed latest board, `slates/latest/slate.json` joined to the latest
`predictions` partition, `STATUS_*`/`LEASE_*`, and optionally `accounting-data` (`app_export.py:132-176`). Output 2026-10-03T06:07Z:
events 45, markets 4,289 (4.5 MB), **model_prices 0, recommendations 0, theses 0**, wagers/settlements 0, health `RESEARCH_ONLY`.
It ignores everything on `main/data/` (history, research tables, identity beyond the registry), `context/rosters`, `context/injuries`,
order books, deltas (other than reconstruction), packet player/team blocks, and `matchup/context`.

### 5.12 Research results (all RESEARCH, all negative vs market)
`docs/research/RESEARCH_NOTES.md`, `HANDOFF_SESSION3.md §K-L`: market log loss 0.465 vs DATA_ONLY 0.522 (300 late-2025-26 games);
props Brier 0.165 (7,666 contracts); residual information beyond Kalshi ≈ 0 in every family after de-laddering; injury-impact
ridge APM **rejected** (holdout RMSE worse); shot-profile walk-forward `NO_CHANGE_IN_CONCLUSION`; matchup V2 effects `neutral-0`.
Research freeze in force (`docs/research/MODEL_FREEZE.md`).

### 5.13 Data sources reachable from production (`docs/NBA_DATA_SOURCE_AUDIT.md`, `docs/research/MATCHUP_SOURCE_AUDIT.md`,
`SOURCE_AUDIT_GRANULAR.md`)
Reachable: Kalshi live + historical, ESPN site API (scoreboard, summary/box, injuries, teams, rosters), NBA injury PDF host (200 in
probe, but no report found in any production slot so far — off-season). Blocked from GitHub runners: `stats.nba.com` (timeouts at
180 s), `cdn.nba.com` (403). Intermittent: pbpstats. hoopR stops at 2022-23. Consequence: no lineups, stints, on/off, tracking,
matchups, synergy — `EVIDENCE_HEALTH.stint_data.state = absent`.

### 5.14 Defect: slate market quotes are dropped (affects "market prices", "hybrid", app model prices)
`workflows/simulate.py:290` builds `snap = {k: m.get(k) for k in ("yes_bid","yes_ask","no_bid","no_ask","last_price","volume",
"open_interest","liquidity")}` from the captured market row, then `market_implied_probability(snap)` (`execution/economics.py:176`)
calls `market_to_cents`, which only knows how to fill those keys from `*_dollars` fields — which the projection has already discarded.
Captured rows carry `yes_bid_dollars = "0.4700"` and `_quote_cents.yes_bid = 47` (verified for `KXNBAGAME-26OCT03MIATOR-TOR`), yet
the slate row shows `yes_bid: null … flags: ["no_quote: missing yes_ask, no_ask"]`. Result: 73/73 slate contracts `p_market = null`,
`p_hybrid = null`, `p_production = null`; `app_export.build_model_prices` (`app_export.py:385-392`) skips rows with `p_production is
None`, hence 0 model prices. The unit tests pass because `tests/test_simulate_job.py:43-49` fixtures use legacy top-level cent keys.
Not fixed here (read-only audit).

---

## 6. Opponent-adjustment audit

- **Where:** `src/nba_edge/features/build.py:93-126 opponent_adjusted_ratings(team_games, cutoff_date, half_life, prior_games,
  league, n_iter=6, include_preseason=False)`; applied in `build_game_params` (`:283-286`) with `rating_scale`.
- **Formula (per iteration, per team t, over its games g before cutoff, EWM weights w_g = 0.5^(age/half_life), most recent = 1):**
  - `o_raw_g = pts_g/poss_g − hc_g − (def[opp_g] − L)`; `d_raw_g = opp_pts_g/poss_g + hc_g − (off[opp_g] − L)`;
    `hc_g = +home_ppp_edge/2` when home, `−home_ppp_edge/2` when away (`LEAGUE.home_ppp_edge = 0.015`).
  - `off[t] = (Σ w_g·o_raw_g + L·prior_games) / (Σ w_g + prior_games)`; same for `def[t]`; `L = league_rates(...)["ppp"]`
    (mean pts/poss of all prior team-games, `:82-90`).
  - Initialised at `off = def = L` for every team; **6 synchronous iterations** (new values applied after each full pass).
  - Post-hoc spread: `rating = L + (rating − L)·rating_scale` with `rating_scale = 1.9` (`:58`, `:284-286`).
- **Baseline:** league mean ppp computed point-in-time from the same filtered frame (strict `game_date_et < cutoff`, preseason
  excluded, `possessions > 0`).
- **Sample requirements:** none hard; shrinkage prior = 4 games (`team_prior_games`), half-life 25 games; `eff_n = Σw` reported.
  A team with 0 prior games gets league priors and `rating_sd = 0.04` (`:137-140`).
- **Recursive vs simple:** recursive (iterative SOS, 6 passes), not solved exactly; convergence is not checked.
- **History:** not stored anywhere. Computed per simulate run and surfaced only as `off_ppp/def_ppp` in `slate.games[].home_params`
  and `packet.games[].home/away` (3 runs today). The research walk-forward CSVs record the resulting `p_sim/m_sim/t_sim`, not ratings.
- **Tests:** `tests/test_features_pit.py::test_cutoff_is_strict_and_excludes_preseason` (asserts `off_ppp` within +0.05 of league),
  `test_rest_days_and_thin_roster_warning`, `test_no_nan_in_params`. No test pins the adjustment's values, iteration count or
  symmetry; `tests/test_impact.py:32` tests opponent control in the (rejected) ridge-APM design instead.
- **Limitations (documented):** shrinkage and iteration attenuate each other (`RESEARCH_NOTES.md §4d`), hence the `rating_scale`
  multiplier; ratings ignore roster/injury composition (`impact_ppp = 0`, SIMULATION.md L2); `rating_scale` fitted on 2023-25 and
  checked once on 2025-26, and is frozen under the baseline.

---

## 7. Inventories

### 7.1 Time-series-capable datasets

| dataset | x-axis | keys | rows | game id link | opponent id link |
|---|---|---|---|---|---|
| `team_games_*.parquet` | game (`game_date_et`, `start_time_utc`) | `(game_id, team_id)` | 8,320 | yes (`espn:`) | yes (`opp_team_id`, NBA id) |
| `player_games_*.parquet` | game | `(game_id, team_id, nba_id)` | 112,427 | yes | yes (`opp_team_id`) |
| `shot_events_*.parquet` | event within game (`period`, `clock_seconds_remaining`, `sequence`) | `event_id`; `(game_id, shooter_player_id)` | 927,349 | yes | yes, but **ESPN team id** (`opponent_team_id`) |
| `shot_profile_pit_features.parquet` | game (`tip_ts`, ms epoch) | `(game_id, player_id)` | 85,846 | yes | yes (ESPN id) |
| `candles_*.jsonl.gz` | hour (`end_period_ts`) | `ticker` | 653,308 | via `market_table` join (ticker → `game_id`) | via team/player on the market |
| `market_table.parquet` | horizon (T-24h…final) | `(ticker, horizon)` | 68,235 | yes (`game_id`) | `home`/`away` tricodes |
| archive `kalshi/markets` (+deltas) | capture instant (`_observed_at_utc`) | `ticker` | 63,116 checkpoint rows + 25 deltas | via `markets_for_game` (date+teams) | via `custom_strike.basketball_team` uuid |
| archive `kalshi/orderbooks` (+deltas) | capture instant | `ticker` | 5,100 + 25 deltas | same | same |
| archive `context/injuries` | snapshot (`_observed_at_utc`) | `(team_id, player_name_raw)` | 3,389 | `game_id` null | no |
| archive `context/rosters` | snapshot | `(espn_team_id, espn_athlete_id)` | 26,769 | no | no |
| archive `predictions` | run (`predicted_at_utc`) | `(prediction_id)`, `(ticker, run)` | 219 | yes | via contract `team_id` |
| `slates/*/packet.json` team/player params | run | `(game_id, team_id)`, `(game_id, nba_id)` | 3 runs × 1 game | yes | yes |
| `docs/research/wf_v5.csv` etc. | game | `game_id` | 300 | yes | no (home only) |
| `docs/research/prop_walk_forward_v3.csv` | game | `(ticker, date)` | 7,666 | via ticker | no |

### 7.2 Split-capable datasets (dimensions actually stored, with counts)

| dataset | dimension | values / counts |
|---|---|---|
| team_games / player_games | `home` | 50/50 by construction (4,160 each) |
| | `season_type` | team rows: preseason 396, regular 7,386, playin 36, playoffs 502 |
| | `season` | 2023-24 2,766; 2024-25 2,780; 2025-26 2,774 (team rows) |
| | `n_ot` | 0–3 (OT games present) |
| player_games | `started` / `played` / `dnp_reason` | ~19 % DNP rows carry a reason |
| shot_events | `period` (1–6), `zone` (6), `is_free_throw`, `shot_made`, `is_home`, `season_type`, `event_type` (free text) | 927,349 |
| market_table | `family` (8), `horizon` (5), `scope`, `period`, `comparator`, `season` | counts in §5.5 |
| settled markets | `result` yes/no/scalar | §5.4 |
| archive markets | `_family` (63 ontology families), `_support` (5), `status` | §5.9 |
| Not stored anywhere: handedness, game state (score-time), rest buckets (derivable), strength state, surface. |

### 7.3 Market-history inventory

- **Historical hourly candles (committed):** per-ticker series exist for 23,881 tickers in 8 series; granularity 60 min; retention =
  whole 2025-26 season (plus 2024-25 playoffs for KXNBAGAME); size 12.6 MB gz. Median length: ~58 candles for moneylines,
  ~20–24 for spreads/totals/props (markets open ~1 day before tip). Fields: bid/ask OHLC, trade OHLC (often null), volume, OI.
- **Pre-tip horizon table (committed):** `market_table.parquet` snaps the last candle strictly before each of T-24h, T-6h, T-90m,
  T-30m and "final" to the ESPN tip — 68,235 (ticker, horizon) rows, outcomes attached.
- **Prospective archive (data-archive):** checkpoints every 24 ticks + 15/10/7/5-minute deltas while a game is inside the 8 h window;
  daily otherwise. Today: 18 checkpoints (4,289 markets each at the end), 25 deltas, 2 verified chains; retention is append-only
  with no pruning policy in code; projected 0.22 GB/season for the board (`DELTA_ARCHIVE.md §4`). Per-ticker series require
  `archive/reconstruct.py` replay (checkpoint + deltas, ~0.3–0.5 s per chain).
- **Order books:** top-of-book depth for ≤300 markets per capture (5,100 rows across 906 tickers so far) — a time series only for
  the few game markets that were live (e.g. 2 KXNBAGAME tickers on 10-03).

### 7.4 Projection inventory

| output | per entity? | per market? | distribution or point | stored across runs? |
|---|---|---|---|---|
| `predictions` ledger | per contract (ticker) | yes | point `p_data_only` (+ MC se), `p_market`, `p_hybrid`, `p_production` | yes (append-only, 3 runs) |
| `packet.json games[].sim` | per game | — | quantiles q5–q95 of margin/total/home/away/1H total/1Q total; ladders by 1-point steps | one file per run dir (not manifested) |
| `packet.json players[].sim` | per player | — | mean + quantiles for min, pts (and other stats) | per run dir |
| `slate.json games[]` | per game | — | `p_home_win`, `margin_mean/sd`, `total_mean/sd`, `ot_rate` | per run dir + `latest` |
| research CSVs | per game / per prop | — | point `p_sim`, `m_sim`, `t_sim`, `mu_sim` | one-off |
| raw simulation draws | — | — | **not stored** (40,000 draws regenerated deterministically from seed) | no |

---

## 8. Existing ranking / percentile / league-average code

- `features/build.py:82 league_rates` — league mean pace48 and ppp (point-in-time).
- `features/build.py:93 opponent_adjusted_ratings` — team strength ratings relative to league (a de facto ranking input).
- `sim/rotation.py:47 ROTATION_SIZE_PMF`, `:69 classify_role`, `:210 estimate_profile` — empirical rotation-size distribution and
  role classification (STARTER / ROTATION / FRINGE / DEEP).
- `shotprofile/features.py:99 LeaguePrior.from_events` — league zone rates/efficiencies as shrinkage targets.
- `workflows/simulate.py:384` — quantile helper (`q5…q95`) for packet distributions.
- `evaluation/metrics.py` — `brier`, `log_loss`, `calibration_table`, `ece`, `reliability_slope_intercept`, `sharpness`,
  `brier_skill_score`, `metrics_by_group`; `evaluation/authority.py:48 bootstrap_ci`.
- `ops/capture_health.py:156 _percentile` — operational (capture-age p50/p90/p99), not research.
- `research/impact.py:173`, `research/market_calibration.py:62` — quantiles in one-off studies.
- No player/team percentile ranks, leaderboards, or league-relative z-scores exist anywhere in `src/`.

---

## 9. Size estimates (measured by serialising real rows to compact JSON)

| exposure | basis | estimate |
|---|---|---|
| (a) Team profile — one team's 3-season game log | 297 rows × ~397 B | ~118 KB per team; all 30 teams 3.3 MB (8,320 rows) |
| (b) Player profile — one heavy player's game log | 297 rows × ~487 B | ~145 KB per player; all 934 players 55 MB; registry's 175 ≈ 10–12 MB |
| (c) Per-game detail | 2 team rows (0.8 KB) + ~37 player rows (18 KB) + ~235 shot events (68 KB) | ~87 KB per game with shots, ~19 KB without; 4,160 games ≈ 360 MB with shots / 80 MB without |
| (d) Metric time series (derived L5/L10/season, ratings) | 1 row per team-game, ~10 numeric fields | ≈ 100–150 B/row → 1.2 MB for all teams; player-level ≈ 15 MB |
| (e) Market history — hourly candles | 653,308 rows × ~250 B JSON | ≈ 160 MB uncompressed (12.6 MB gz); per game-ticker ≈ 15 KB; per prop ticker ≈ 5 KB |
| (e') Pre-tip horizon table | 68,235 rows × ~400 B | ≈ 27 MB (1.4 MB parquet); per game ≈ 20 KB |
| (f) Live board (already exported) | `app/latest/markets.json` | 4.5 MB per snapshot, 4,289 markets |

Lazy-loading implication: team profiles and the horizon table are cheap; player profiles must be per-player; shot events per game.

---

## 10. Recommended research capabilities to expose in this pass

Supported by evidence (mark PARTIAL unless noted):
1. **Team game logs and results** (3 seasons; home/away, season_type, quarters, OT, possessions, margin/total) — from
   `team_games_*.parquet`; keys already match the app export.
2. **Player game logs** (minutes, box score, started/played/DNP, plus_minus) — `player_games_*.parquet`; expose by `nba_player_id`
   (negative ESPN id) and keep `espn_athlete_id`.
3. **Derived recent-form windows (L3/L5/L10/season) and splits (home/away, season_type, vs opponent)** — derivable deterministically
   from (1)–(2); mark DERIVED and state the window.
4. **Opponent-adjusted team ratings and pace** — expose by running `opponent_adjusted_ratings`/`team_params` per cutoff date and
   labelling them RESEARCH ("features-0.1.0, frozen baseline parameters"); there is no stored history, so compute on export.
5. **Shot profiles by zone** (player rates/efficiency, opponent-allowed rates) — from `shot_events` / `shot_profile_pit_features`,
   with the ESPN→NBA team-id map applied; label RESEARCH (walk-forward showed no market value).
6. **Settled market outcomes and pre-tip price horizons** per game/ticker — `market_table.parquet` (T-24h…final) and settled
   markets; label PARTIAL (2025-26 only; props from 2025-11-19; team totals/1H from Feb 2026).
7. **Hourly price history per ticker** — candles for the 8 series; PARTIAL (2025-26 only, bid/ask only).
8. **Live board, order-book depth and 15-minute price path** for games in window — from the archive (reconstruct); PARTIAL
   (16 days, preseason).
9. **Projection distributions per game/player** when a slate exists — packet quantiles; RESEARCH and gated
   (`CANNOT_TRUST_INPUTS` until official injury report + regular season), and note the §5.14 quote defect until fixed.
10. **Model-vs-market evidence** — the research JSON/CSV outputs (walk-forward, prop calibration, market calibration) as static
    RESEARCH exhibits with the "market beats model in all 8 families" verdict stated verbatim.

Mark RESEARCH: matchup/shot-profile adjustments (neutral), season simulator, injury-impact (rejected), minutes mixture validation.
Mark UNAVAILABLE: lineups/stints/on-off/tracking, confirmed starters, official injury report (never captured), CLV, wagers,
settlements/evaluations (never run), weather, venue effects, play-by-play beyond shooting plays, PRA markets (0 rows), positive NBA
person ids.

---

## 11. Open questions / UNKNOWN items and what I checked

1. **Force-update on `data-archive`.** `git fetch --depth=1` reported `+ 927c49b...4121d07 (forced update)`. The current tree's
   manifest (238 entries) is self-consistent (0 missing files) and `EVIDENCE_HEALTH.delta_chains.n_broken = 0`, but with a shallow
   fetch I cannot tell whether earlier partitions (e.g. captures between 09-23 and 09-26 beyond the single daily one) were dropped.
   No code in `scripts/archive_push.sh` or the workflows force-pushes; `conductor.yml:120`/`capture_worker.yml:84` re-create the
   orphan only "if missing". UNKNOWN whether a manual reset happened.
2. **Why the official injury PDF is never found.** `STATUS_context.injuries_official.missing = true, slots_tried 8` on every snapshot
   since 09-18; the probe (`docs/probe`) saw a 200 for the host. Off-season absence of reports is the likely cause; unverifiable until
   game days.
3. **ESPN team-id → NBA-id map** is not committed; I derived it from a roster snapshot (30 entries, all abbreviations resolvable via
   `teams.csv` `alt_tricodes`). A collision check between ESPN abbreviations and `alt_tricodes` found none.
4. **Whether `season_sim` output is wired anywhere** — `sim/season.py` exists with tests, but I found no ledger kind or slate field
   that carries win-total distributions (`grep` for `season-sim`/`SEASON_SIM_VERSION` outside `sim/` and the baseline manifest).
5. **Test suite not executed** (read-only rule; running pytest would write `.hypothesis/` and `.pytest_cache/` inside the repo).
   `.pytest_cache/v/cache/lastfailed` is `{}` from a 2026-10-02 local run; CI is green per `docs/HANDOFF_SESSION3.md`.
6. **Quote defect scope (§5.14):** verified on the 2026-10-03 slate and by reading `simulate.py:290` + `economics.py:176-190`;
   whether the same projection appears in `settle`/`evaluate` paths was not traced.
7. **Candle coverage per horizon** for props: `market_table` has 16,090 T-30m rows vs 16,091 final rows, so nearly every ticker with
   candles has all horizons; tickers *without* candles (1H series, WINS, ~2,574 SPREAD markets) are absent from the table.
8. **Registry growth:** `scripts/build_player_identity.py` is manual; 431 of 603 rostered players are outside the registry today,
   so player-linked markets for them will export by display name only (`APP_EXPORT.md` "Known gaps").
