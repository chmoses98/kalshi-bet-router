# Phase 2 research-data audit — NHL (`chmoses98/NHL-edge-finder`)

Audited read-only on 2026-10-03 against `main` @ `06ebb78` (99 commits), `origin/data-archive` @ `1101a27`
(fetched `--depth=1`; GitHub API reports ~560 commits on the branch), `origin/accounting-data` @ `4a0e769`.
Scratch copies: `(local scratch, not committed)`
(the per-run `slates/dt=*` directories were dropped locally after measurement because the shared scratch disk filled;
counts for them come from `git ls-tree origin/data-archive`). All measurements below were run with pyarrow / stdlib
Python against the real files; commands and outputs are quoted where load-bearing.

---

## 1. Summary

1. NHL is the richest sport in the fleet for **player-level research data**: on `main` there are five full seasons
   (2021-22 .. 2025-26) of *official* per-player-game rows derived from NHL boxscore + play-by-play + shift charts
   (`data/history/players/`: 251,797 skater-games, 27,970 goalie-games, 43,110 goals with both teams' on-ice skaters,
   824,318 shot attempts with x/y, 1.49M co-ice pairs, TOI split EV/PP/SH/EA/EN/OT), and the production settle job
   appends the same tables for every finished live game (21 games so far, 756 skater rows). Semantics are tested
   (`tests/test_player_sim.py`, 21 tests) and reconcile 100% with the official stat line.
2. **Team data** is MoneyPuck game-by-game (120 columns incl. xG, Corsi, Fenwick, danger tiers, score/venue-adjusted
   xG, hits, giveaways, penalties) for 4 seasons on `main` (44,736 team-game-situation rows) plus a live copy refreshed
   on every context run (`context/team_games`, 2,820 rows incl. the 16 games played so far in 2026-27), plus official
   NHL team summaries (PP%, PK%, etc.) and 5 seasons of MoneyPuck shot-level xG (605,110 shots).
3. **Market data** is the strongest production asset: a self-chaining capture worker has written **520 board ticks**
   (median 10 min, max gap 23 min, no gap > 30 min) since 2026-09-29T12:42Z, delta-encoded with periodic checkpoints
   and reconstructable at any instant (24 reconstruction tests). 6,050 tickers show quote changes (210,981 change
   events); a game-winner ticker has ~300 price points over its life. Order-book depth for the top-300 markets is
   captured on every tick. Historical Kalshi candles (1-min and hourly) for 2025-26 exist for game/total/spread and
   player series (1.97M candle rows) with settled results joined to NHL game ids.
4. **Model outputs** are archived per contract per run (61,001 V1 rows, 55,713 V2 shadow rows, 27,810 player-sim
   rows over 100 slate runs in 5 days), so a *model-probability time series* per ticker exists (median 14 runs per
   ticker, up to 25). Full per-game distributions (ladders, PMFs, quantiles, saves ladders, 18 game scripts, 27 thesis
   events, player exp G/A/P/TOI) sit in each run's `packet.json`.
5. **Historical accuracy** is real but short prospectively: 58,302 settled V1 evaluation rows (21 games, 10,725 with a
   model probability) and 27,407 settled player rows (16 games). On identical rows DATA_ONLY_V1 Brier 0.1901 vs Kalshi
   mid 0.1899; PLAYER_SIM_V1 0.1361 vs 0.1366 (goals). Historical walk-forwards (2,624 games x 2 seasons, 94k
   skater-games) and a candle-based market benchmark are committed under `docs/research/`.
6. Weakest areas: **no opponent adjustment anywhere** (ratings are raw EW-shrunk rates; opponent enters only as a
   multiplicative factor at game time); **no stored recent-form windows, rankings or percentiles**; **lines, injuries and
   goalie-status history are only 4-5 days deep** (no historical source exists); **DATA_ONLY_V2 is never settled/evaluated
   prospectively**; the historical parquet pulls are one-off manual-dispatch workflows, not scheduled; the
   **accounting ledger is empty** (0 wagers).
7. The app export today reads only the current board, the latest slate/packet, STATUS files and the (empty) accounting
   ledger; it ignores every history file, every context kind except the schedule, all prediction/evaluation history, the
   market tick history (`event_detail.price_history` is always `[]`) and the thesis postmortems.

---

## 2. Data branches and layout

### 2.1 `main` (code + compact history), 61 MB under `data/`, 24 MB under `docs/`

| path | what | size | produced by |
|---|---|---|---|
| `data/identity/teams.csv` | 35 team rows (32 active + ARI 53, UHC 59 inactive) | 3 KB | hand-curated, `tests/test_identity.py` |
| `data/catalog/market_ontology.yaml`, `discovery_summary.{json,md}` | 27 Kalshi families, 75 NHL series, 14,468 series scanned 2026-09-29 | 200 KB | `nhl discover` (manual) |
| `data/history/moneypuck/team_games_{2022..2025}.parquet` | MoneyPuck team game-by-game, 4 situations, 120 cols; 11,152-11,200 rows/season | 1.6 MB each (zstd) | `history.yml` (manual dispatch / `.trigger/history`), 2026-09-29T12:46Z |
| `data/history/moneypuck/shots_{2021..2025}.parquet` | MoneyPuck shot-level xG, 24 cols; 119,271-122,472 rows/season | 1.73-1.77 MB each | `research_data.yml` (manual), 2026-09-29T15:05Z |
| `data/history/nhl/games_{2022..2025}.parquet` | official results, 19 cols, 1,417 rows/season (1,312 regular + 105 playoff), REG/OT/SO | 26 KB each | `history.yml` |
| `data/history/players/{players,goalies,goals,shots,coice,team_states}_{2021..2026}.parquet` + `MANIFEST_*.json` | official player events per game (see §5.2) | 20 MB total | `player_research_data.yml` (manual), 2026-09-30T05:32-05:35Z |
| `data/history/kalshi/` | settled markets jsonl.gz for 18 series (40,082 markets) + `candles_core.parquet` (1,468,183 rows) + `candles_ladders.parquet` (197,939 rows) | 15 MB | `research_data.yml`, 2026-09-29 |
| `data/history/players_kalshi/kalshi/` | settled player markets (133,767) + candles for GOAL/AST/PTS | 12 MB | `player_research_data.yml`, 2026-09-30 |
| `data/params/{nhl-features-2.0,nhl-sim-2.0,player-sim-1.0}.json` | fitted parameters with provenance (train seasons, counts) | 11 KB | `players/fit.py`, research scripts |
| `docs/research/walk_forward_*.{json,csv}`, `docs/research/player_sim_v1/*.parquet` | walk-forward outputs: 2,624 games x arms; per-skater-game predictions 2024/2025 (47,225 / 47,231 rows), per-goalie-start (2,624/season) | 4 MB | research scripts |
| `docs/shadow/`, `docs/rehearsal/`, `docs/probe/samples/` | one-off shadow runs, rehearsal outputs, raw source samples | ~18 MB | manual workflows |

### 2.2 `data-archive` (orphan, append-only ledger), 583 MB extracted, 2,724 files, manifest 2,326 entries

Root: `manifest.jsonl` (one line per partition: `path, kind, rows, sha256, observed_at_utc, written_at_utc, run_id, meta`;
`.gitattributes` sets `merge=union`), `STATUS_{capture,context,simulate,settle,evaluate,worker}.json`, `LEASE_capture.json`.
`Ledger.append_rows` refuses overwrites (`src/nhl_edge/archive/ledger.py`); `Ledger.verify()` re-hashes before every push.
`app/latest` and `slates/latest` are **replaced** on every cycle and are not in the manifest; everything else is immutable.

Measured per kind (`manifest.jsonl`, 2026-09-29T12:27Z .. 2026-10-03T06:02Z):

| kind | files | rows | dt partitions | notes |
|---|---:|---:|---|---|
| `context/schedule` | 72 | 4,394 | 09-29..10-03 (5) | 8-day window per snapshot, 2 KB each |
| `context/team_games` | 72 | 201,780 | 5 | full MoneyPuck copy every refresh (899 KB x 72 = 64.7 MB; only 32 rows are new-season) |
| `context/team_games_st` | 4 | 44,840 | 4 | 4 situations, content-hash deduplicated |
| `context/team_summary` | 72 | 3,270 | 5 | NHL official, cur + prev season |
| `context/goalie_stats` | 72 | 48,839 | 5 | NHL stats (135) + MoneyPuck (665) rows per refresh |
| `context/rosters` | 72 | 56,098 | 5 | 766 players, 32 teams per refresh |
| `context/goalie_observations` | 81 | 1,876 | 5 | DailyFaceoff + boxscore starters |
| `context/lines` | 54 | 24,612 | 09-30..10-03 (4) | DailyFaceoff line combos |
| `context/injuries` | 72 | 8,304 | 5 | ESPN, ~110 rows/refresh |
| `context/sportsbook_odds` | 45 | 2,254 | 5 | quarantined; 6 providers, 21 games |
| `context/dailyfaceoff_raw` | 72 | blobs | 5 | raw page JSON |
| `kalshi/markets` + `kalshi/markets_delta` | 24 + 496 | 69,527 + 496 | 5 | checkpoint + deltas, schema `kalshi.board.delta/1` |
| `kalshi/orderbooks` + `kalshi/orderbooks_delta` | 24 + 496 | 7,200 + 496 | 5 | 300 books per tick |
| `predictions` / `contracts` | 93 / 93 | 61,001 / 61,001 | 5 | DATA_ONLY_V1 + market quotes, frozen per run |
| `predictions_v2` | 87 | 55,713 | 5 | DATA_ONLY_V2 shadow |
| `predictions_player` | 52 | 27,810 | 4 | PLAYER_SIM_V1 shadow |
| `results` / `settlements` | 12 / 12 | 21 / 4,698 | 4 | 21 final games; 183 settlement rows per game |
| `player_events/{players,goalies,goals,shots,coice,team_states}` | 21 each | 756 / 84 / 136 / 2,360 / 4,539 / 42 | 4 | one partition per finished game |
| `evaluations` / `evaluations_player` | 11 / 9 | 58,302 / 27,407 | 4 / 3 | append-only; de-duplicated by (prediction_id, settlement_key) -> no duplicates |
| `thesis_games` / `thesis_decisions` / `thesis_postmortems` | 48 / 48 / 7 | 293 / 1,947 / 1,902 | 3 / 3 / 2 | thesis layer (RESEARCH_ONLY) |
| `slates/dt=*/<ts>_<run>/{slate.json,slate.md,packet.json,card.md}` | 100 runs (18/27/26/27/2 per day) | — | 5 | **465 MB** (packet.json ~3.1 MB each) — 80% of the branch |
| `slates/latest/` | 4 files | — | replaced | slate.json 1.9 MB, packet.json 3.2 MB |
| `app/latest/` | 13 + 13 event_detail | — | replaced | 3.0 MB (markets.json 2.6 MB) |
| `eval/report{,_player,_thesis}.{json,md}` | 6 | — | replaced | current evaluation reports |

Capture cadence (file timestamps of all 520 ticks): median gap 10.0 min, p90 15 min, max 23.1 min, min 2.1 min, zero
gaps > 30 min; ticks per day 79 / 132 / 130 / 141 / 38 (10-03 partial). `STATUS_worker.json` of the last retired worker:
48 cycles, 48 captures OK, 0 failures, context 5/5, simulate 6/6, settle 4/4, evaluate 4/4. GitHub: `capture_worker.yml`
run 37089249504 `in_progress`, successor 37089289881 `pending` at audit time; `ci.yml` last run on `main` success
(2026-10-02T23:20Z).

### 2.3 `accounting-data` (3 files, 16 KB)

`README.md` + `data/accounting/wagers.jsonl` (0 bytes) + `data/accounting/settlements.jsonl` (0 bytes). Empty since
creation 2026-09-29T20:06Z. `docs/APP_EXPORT.md` states 17 real wagers exist on an unmerged `kalshi-router/NHL` branch
of the router repo — not in this repository.

---

## 3. Identity model

| entity | canonical key | where | cross-source maps | coverage / collisions |
|---|---|---|---|---|
| Team | official NHL `team_id` (int) | `data/identity/teams.csv`, `src/nhl_edge/identity/teams.py` | `abbrev` (api-web), `ABBREV_ALIASES` (MoneyPuck `T.B`, `L.A`, `S.J`; Kalshi `SJ`, `TB`, `NJ`, `LA`, `VEG`, `WAS`...), free-text names via `resolve_name` (word-bounded, exactly-one-hit) | 32 active; ARI 53 (to 2024), UHC 59 (2024-25), UTA 68 (2025-) kept as separate rows; `SUCCESSOR` map is explicit only. MoneyPuck labels all Utah seasons `UTA` -> mapped to 68. Tests: `tests/test_identity.py` (6). |
| Player | official NHL `player_id` (8 digits, e.g. 8478403) | everywhere in `data/history/players/*`, `player_events/*`, `context/rosters`, `context/lines.player_id`, `predictions_player.player_id` | Kalshi: market suffix `<team><initial><LASTNAME><jersey>` resolved against the point-in-time roster by team+jersey+name, fallback team+name, **fail-closed** (`players/identity.py`); Kalshi `custom_strike.hockey_player` uuid retained as `contracts.kalshi_entity_uuid`; DailyFaceoff `dfo_player_id` kept beside `player_id` in `context/lines` (resolution 89.4% `jersey+name`, 7.0% `name_only`, 1.5% unresolved); ESPN injuries are **name-only** (`espn_id` null in every sampled row); MoneyPuck `shooterPlayerId`/`goalieIdForShot` are NHL ids (0 when unknown) | 940 skaters + 123 goalies in 2025-26 history; 73 of 832 opening-night Kalshi contracts had stale jersey numbers (doc), handled by name fallback. |
| Game | official NHL game id, string in context/predictions (`"2026020022"`), int in parquet (`2026020022`) | schedule, predictions, contracts, settlements, player_events, history | Kalshi contract -> game by (`game_date`, frozenset of two team ids) (`kalshi/contracts.game_key`), never by ticker order; MoneyPuck `gameId` == NHL id; MoneyPuck shots `game_id` is the short form (`20001`) with `nhl_game_id` added | 2026-27 ids 2026020001..2026020034 seen; ESPN event ids not used. |
| Market | Kalshi `ticker` | all kalshi/predictions kinds | `event_ticker`, `series_ticker`, family via ontology | 2,918 rows in last checkpoint, 2,554 active |
| App export ids | `edge_finder_contract.ids`: `event_id = digest(NHL, nhl_game_id, id)`, `participant_id = digest(NHL, TEAM|PLAYER, nhl_team_id|nhl_player_id, id)`, `market_id = mkt_kalshi_<TICKER>` | `contract/edge_finder_contract/ids.py`, `src/nhl_edge/app_export.py` | identical source keys to the research layer, so research entities join to app entities 1:1 | player participants only for markets the player shadow resolved (`packet.player_shadow.contracts[].player_id`) |

---

## 4. Capability matrix

Status per the spec vocabulary. "Prod" = produced by the scheduled capture worker (`capture_worker.yml` self-dispatch
+ conductor decisions); "manual" = `workflow_dispatch`/`.trigger` one-off.

| capability | status | origin | coverage | cadence | tests | notes |
|---|---|---|---|---|---|---|
| Team metrics (xGF/60, xGA/60, finish, stop, league rates) | PARTIAL | computed per run by `features/ratings.py` from `context/team_games`; stored in `packet.json` `games[].team_state` | 100 runs, 5 days; recomputable for any date from history parquet | hourly while a game is within 26 h (`conductor.decide`) | `test_walk_forward.py::test_ratings_are_point_in_time`, `test_build_gamelog_filters_and_canonical_teams` | no opponent adjustment; EW half-life 20 games, prior 20 games, prev season x0.6 |
| Team special-teams rates (EV/PP/PK xG/60, draw/take minutes) | RESEARCH | `features/special_teams.py` (V2 shadow) from `context/team_games_st`; `packet.v2_shadow.blocks[].detail.components` | 4 snapshots, 5 days | with simulate | `test_v2_model.py` (3 ST tests) | shadow arm only |
| Official team season summary (GF/GA, PP%, PK%, SF/SA, FO%) | VERIFIED (snapshot) | `context/team_summary` (NHL stats API) | 72 snapshots, cur + prev season, 62 rows each | every context refresh (<= 55 min in window) | parser `tests/test_history.py`? (summaries parsed in `data/nhl_api.py`; no dedicated test found) | not consumed by any model |
| Team game logs (per team-game, xG etc.) | PARTIAL (history one-off) / VERIFIED (live kind) | `data/history/moneypuck/team_games_*.parquet` (4 seasons); `context/team_games` live | 44,736 rows 2022-10-07..2026-06-14 + 2,820 live rows incl. 2026-27 | history: manual; live: every refresh | `test_history.py` (11), `test_v2_model.py` | opponent id present (`opp_team_id`), home flag, 4 situations |
| Player game logs (official) | VERIFIED (live) / PARTIAL (history one-off) | `data/history/players/*.parquet`; `player_events/*` written by settle job | 251,797 skater-games 2021-22..2025-26 + 756 live | settle every 45-90 min after game +3 h | `test_player_sim.py` (21), `test_settlement.py` | TOI by 6 strength states, SOG/attempts by state, on-ice GF/GA, A1/A2 |
| Goalie game logs | same as above | `goalies_*.parquet`, `player_events/goalies` | 27,970 + 84 rows | same | same | saves/SA split EV/PP/SH, starter flag, decision |
| Historical opponents / results | PARTIAL | `data/history/nhl/games_*.parquet` (REG/OT/SO), `results` kind live | 5,668 games 2022-23..2025-26 + 21 live | manual / settle | `test_history.py::test_parse_nhl_games*`, `test_settlement.py` | official scores, period count, last_period_type |
| Opponent adjustments | UNAVAILABLE | none (see §6) | — | — | — | MoneyPuck provides score/venue-adjusted xG columns, not opponent-adjusted |
| Schedule strength | UNAVAILABLE | not computed anywhere | — | — | — | derivable from ratings + schedule |
| Recent-form windows (L3/L5/season) | UNAVAILABLE as stored; RESEARCH as EW weights | `ratings._weights` (half-life 20), `players/features.PlayerBook` (share HL 6, rate HL 60), `coice_fractions` (last 8 games) | — | — | — | no fixed windows stored |
| Usage (TOI by state, shifts, PP TOI; expected TOI) | VERIFIED (actual) / PARTIAL (projected) | `players_*.parquet` `toi_{ev,pp,sh,ea,en,ot}_s`, `shifts`; projections in `predictions_player.meta_expected_toi_min`, `meta_toi_p10_p50_p90_min`, `packet.player_shadow.blocks[].top_players` | 5 seasons + live; projections 27,810 rows | — | `test_player_sim.py` | `shifts_ok` flag (57 of 1,398 games in 2024-25 lack shift charts) |
| Lineups / lines / PP-PK units | PARTIAL | `context/lines` (DailyFaceoff): category ev/pp/pk/g/ir, unit f1-f4/d1-d3/pp1-2/pk1-2, slot, source, updatedAt | 32 teams, 1-4 observation days each (54 partitions since 2026-09-30) | each context refresh, today's teams only | `test_player_sim.py::test_dailyfaceoff_line_page_parses_and_resolves_by_jersey_and_name` | no historical source; backtests use shift-derived deployment |
| Rosters | VERIFIED (snapshot) | `context/rosters` (NHL api-web) | 766 players / 32 teams per refresh, 72 snapshots | each refresh | identity tests | includes `shoots_catches`, `birth_date` |
| Injuries / availability | PARTIAL | `context/injuries` (ESPN) | ~110 rows x 72 snapshots, 31 teams; status IR/Out/DTD/Suspension, detail, return_date | each refresh | `data/context.parse_espn_injuries` untested directly | name-only; not modelled in V1; removes players from projected lineups |
| Goalie status (UNKNOWN/PROJECTED/PROBABLE/CONFIRMED) | VERIFIED | `context/goalie_observations` (DFO + boxscore post-start) | 1,876 obs, 78 game-team pairs, 44 CONFIRMED | each refresh + settle | `tests/test_goalies.py` (7) | point-in-time ladder tested |
| Matchup metrics | RESEARCH | `packet.games[].model.components` (lambda decomposition), `v2_shadow.detail`, `thesis_card.games[].scripts` (18 scripts with freq, win prob, goals, shots, saves, pp/en share, tags, players_most_involved), `thesis_events` (27) | per game per run, 100 runs | with simulate | `test_thesis_core.py` (13), `test_thesis_card.py` (8) | |
| Projection distributions | PARTIAL | `packet.games[].model.sim` (`total_quantiles`, `margin_quantiles`, `total_ladder`, puckline/team-total ladders, `margin_pmf`, `total_pmf`, `p_btts`), `player_shadow.blocks[].goalies[].saves_ladder` (15+..40+), per-game correlation matrix | 100 runs; 20,000 team draws / 10,000 player draws, draws not persisted | hourly | `test_sim.py` (9), `test_simulate_e2e.py` (5) | |
| Raw projections (per contract per run) | VERIFIED | `predictions` (V1), `predictions_v2`, `predictions_player` | 61,001 / 55,713 / 27,810 rows; 4,691 tickers; 34 games; median 14 runs/ticker, max 25 | hourly | `test_simulate_e2e.py`, `test_v2_market_and_shadow.py`, `test_player_shadow.py` | 81% of V1 rows are `UNSUPPORTED` (no `p_data_only`) |
| Market prices (current) | VERIFIED | `kalshi/markets` checkpoint + deltas; `reconstruct.latest_board` | 2,554 active markets, 75 series, 15 families | 5-15 min horizon-aware | `test_delta_archive.py` (30), `test_delta_reconstruct.py` (24) | `_quote_cents` {yes_bid, yes_ask, no_bid, no_ask, last_price} |
| Market price history (live ticks) | VERIFIED (4.5 days) | same; `reconstruct.iter_board_ticks` | 520 ticks; 6,050 tickers with >= 1 quote change; 210,981 change events | as above | as above | per-ticker series of ~40-300 points |
| Order-book depth history | VERIFIED (4.5 days) | `kalshi/orderbooks{,_delta}` | 300 markets per tick, full yes/no ladders | as above | as above | priority-sampled, not all markets |
| Market price history (historical candles) | PARTIAL (one-off) | `data/history/kalshi/candles_*.parquet`, `players_kalshi/.../candles_*.parquet` | 2025-04..2026-06; 1.97M rows; 1-min + hourly for GAME/TOTAL, hourly for SPREAD/GOAL/AST/PTS | manual | `test_market_benchmark_scores_only_two_sided_quotes` | best bid/ask at period close, no depth; ~1% fetch errors, player PTS/GOAL cut by deadline |
| Historical settled markets | PARTIAL (one-off) | `markets_*.jsonl.gz` | 40,082 core + 133,767 player markets with `result`, joined to NHL game id (99.6%) | manual | `research/kalshi_history.py` untested directly | |
| Advanced stats (xG, Corsi, Fenwick, danger tiers, shot xG) | PARTIAL (history one-off) / VERIFIED (live team rows) | MoneyPuck team_games (120 cols), MoneyPuck shots (xGoal per shot), own xG model `players/xg.py` on official coordinates | 4-5 seasons + live | manual / refresh | `test_v2_model.py`, `test_player_sim.py` | |
| Situational splits | VERIFIED (strength state) / PARTIAL (others) | see §7.2 | — | — | — | |
| Player props | VERIFIED (capture) / RESEARCH (pricing) | Kalshi KXNHLGOAL/PTS/AST/SAVE/FIRSTGOAL; `predictions_player` | 27,810 rows, 516 players evaluated | — | `test_player_sim.py::test_price_player_ladders_and_fail_closed` | shadow only |
| Team props | VERIFIED | KXNHLTEAMTOTAL (MODELABLE) | 4,550 prediction rows | — | `test_settlement.py` | |
| Game-level markets | VERIFIED | KXNHLGAME/SPREAD/TOTAL (MODELABLE), OT/early-goal/period (RESEARCH) | 910 / 1,820 / 4,095 rows | — | `test_settlement.py` (30) | |
| Play-by-play | PARTIAL | `shots_*.parquet` + `goals_*.parquet` (official attempts and goals only); live `player_events/{shots,goals}` | 824k attempts, 43k goals; no faceoff/hit/penalty events | settle | `test_player_sim.py` | |
| Weather | UNAVAILABLE | — | — | — | — | indoor sport |
| Venue / park effects | UNAVAILABLE (effects) / PARTIAL (venue name, `neutral_site`) | `context/schedule.venue`; `HOME_ADJ=1.045` constant | — | — | — | |
| Calibration data | VERIFIED (prospective, 4 days) / RESEARCH (historical) | `evaluations`, `evaluations_player`, `eval/report*.json` (10-bin tables per family x horizon); `docs/research/player_sim_v1/*.parquet` | 58,302 + 27,407 rows; 94,456 historical skater-games | after every settle | `test_metrics.py` (10), `test_workflows.py` | |
| Historical accuracy / postmortems | VERIFIED (4 days) | `eval/report_thesis.json`, `thesis_postmortems` | 2 COMPLETE slates (13 games), 1,902 rows | after settle | `test_repair_pass.py` (28) | |
| CLV | VERIFIED (4 days) | `evaluations.clv_{yes,no}_prob`, `clv_signed` (close = last tick strictly before start) | 10,725 V1 rows; 8.3% of rows lack a close | after settle | `test_clv.py` (5) | mean `clv_signed` -0.0105 |
| Historical wager outcomes | UNAVAILABLE | `accounting-data` empty | 0 | — | `test_accounting.py` (23) | |
| Identity tables | VERIFIED | §3 | — | — | `test_identity.py` | |
| Schedules | VERIFIED | `context/schedule` (8-day window), `data/history/nhl/games_*` | 65 games in window; 5,668 historical | each refresh | — | |
| Seasons covered | — | team: 2022-23..2025-26 (+2026-27 live); shots: 2021-22..2025-26; players: 2021-22..2026-27; Kalshi: 2025-26 | | | | |
| Sportsbook consensus (quarantined) | PARTIAL | `context/sportsbook_odds` from NHL schedule feed (FanDuel, DraftKings, Sportradar, Tipsport, Veikkaus, Doxxbet) | 2,254 rows, 21 games | each refresh | — | moneyline only, never read by DATA_ONLY |

---

## 5. Detailed findings per category

### 5.1 Team metrics and game logs

**Ratings** (`src/nhl_edge/features/ratings.py:120-150` `team_rating`): per team at `as_of`, rows strictly before the
date, last 120 games, weights `0.5 ** (age/20)` x 0.6 for previous-season rows; `off = L + shrink*(raw_off - L)` with
`shrink = n_eff/(n_eff+20)`; `finish = (gf + 60*r)/(xgf + 60)/r` where `r = league g60/xg60`. `expected_goals`
(`ratings.py:186-214`): `lam_h = g60 * (off_h/L) * (def_a/L) * finish_h * goalie_factor_a * 1.045`, back-to-back
x0.965/x1.025, ratio clamp 2.5. Values are stored per game per run in `packet.json` `games[].team_state.{home,away}`
(fields `off_xg60, def_xg60, finish, stop, games_used, raw_off_xg60, raw_def_xg60, gf60, ga60`) and `games[].model.components`.
Example from `slates/latest/packet.json` (2026-10-03, BUF): `off_xg60 3.2025, def_xg60 3.1062, finish 1.0216, games_used 17.38`,
league `xg60 3.0859, g60 3.0285, n_games 1410`.

**Live team game log** `context/team_games` (latest partition 2,820 rows, 899 KB): MoneyPuck `all`-situation rows with
all 120 MoneyPuck columns (`xGoalsFor/Against`, `corsiPercentage`, `fenwickPercentage`, `highDangerxGoalsFor`,
`scoreVenueAdjustedxGoalsFor`, `flurryAdjustedxGoalsFor`, `shotAttemptsFor`, `hitsFor`, `giveawaysFor`, `penaltiesFor`,
`faceOffsWonFor`, ...), keyed `gameId`, `team`/`team_abbrev`, `team_id`, `opposingTeam`/`opp_team_id`, `home_or_away`,
`gameDate` (int YYYYMMDD), `season`. Seasons: 2025 x 2,788 rows, 2026 x 32 rows (16 games of 2026-27 already present,
`max gameDate 20261001`). `context/team_games_st` adds 5on5/5on4/4on5 rows (15 slim columns), 11,280 rows.

**History** `data/history/moneypuck/team_games_2025.parquet`: 11,152 rows x 120 cols, 1,394 games, 32 teams, 4
situations, `game_date` 2025-10-07..2026-06-14, `game_type` {2,3}, 0% nulls on every column. Manifest
(`data/history/MANIFEST.json`): 4 seasons, source `all_teams.csv` sha256 recorded, `retrieved_at_utc 2026-09-29T12:46:09Z`,
`n_errors 0`.

**Official results** `data/history/nhl/games_2025.parquet`: 1,417 rows x 19 cols; `total`/`home_win`/`last_period_type`
1.6% null (23 unplayed playoff slots); `last_period_type` REG 1046 / OT 229 / SO 119.

### 5.2 Player metrics and game logs

`data/history/players/MANIFEST_{2021..2025}.json`: games_ok 1,401 / 1,400 / 1,400 / 1,398 / 1,394, `games_with_shifts`
1,401 / 1,400 / 1,400 / **1,341** / 1,394, `n_errors 0`. Per-season row counts (players / goalies / goals / shots / coice /
team_states): 2021 50,415 / 5,605 / 8,712 / 159,827 / 298,787 / 2,802; 2022 50,381 / 5,599 / 8,801 / 163,771 / 301,920 / 2,800;
2023 50,389 / 5,599 / 8,590 / 170,649 / 301,118 / 2,800; 2024 50,321 / 5,592 / 8,435 / 167,050 / 287,524 / 2,796; 2025
50,183 / 5,575 / 8,572 / 163,021 / 299,675 / 2,788; 2026 (3 opening games) 108 / 12 / 9 / 295 / 636 / 6.

Schemas measured on 2025 (`pq_inspect.py`), all 0% null unless noted:

- `players` (52 cols): `game_id, home/away_team_id, season, game_date, game_type, player_id (940 distinct), team_id,
  is_home, sweater, name, position {C,L,R,D}, goals, assists, points, sog, pp_goals, pim, shifts, toi_s, plus_minus,
  a1, a2, toi_{ev,pp,sh,ea,en,ot}_s, gf_/ga_/sog_/att_{ev,pp,sh,ea,en}, toi_shift_s, shifts_ok`.
- `goalies` (24): `starter, toi_s, saves, shots_against, goals_against, decision (50% null = non-decision), ev/pp/sh saves & sa`.
- `goals` (26): `period, period_type, t_s, scorer_id, a1_id (6.2% null), a2_id (24.1% null), goalie_in_net_id (6.4% null = empty net),
  situation_code (19 distinct), strength {EV,PP,EN,SH,EA}, score_for_before, score_against_before, shot_type, x, y,
  for_on_ice (list), against_on_ice (list), empty_net`.
- `shots` (20): `kind {BLOCK,SOG,MISS,GOAL}, x, y, shot_type (26.8% null — blocks), zone {O,D,N}, strength, since_prev_s, goalie_in_net_id (27.4% null)`.
- `coice` (14): `p1, p2, shared_s, shared_ev_s, shared_pp_s, shared_sh_s` (pairs sharing >= 60 s).
- `team_states` (16): `sec_{ev,pp,sh,ea,en,unknown}, n_seconds {3600,4800,6000}`.

Derivation (`src/nhl_edge/data/player_events.py:1-40`): strength timeline from play-by-play `situationCode` (penalty
expiry between events attributed to PP until the next event); TOI by state from shift charts; shootout dropped.
Doc claim verified by the manifests: 0 games with assist/goal disagreement vs the official line across 6,996 games.

**Derived player profile** (`src/nhl_edge/players/features.py:235-330` `PlayerBook.profile`): rows strictly before the
date (`searchsorted`), deployment shares (half-life 6 games, prior 0.5 pseudo-games toward the position mean),
`ixg60` per state (half-life 60 games, prior minutes EV 300 / PP 60 / SH 60 / EA 10), `finish` (prior 40 xG),
`a1/a2` (priors 25 / 45 teammate on-ice goals), `en60`, `onice_rel`, `sog60`, `toi_mean_s`, flags
{NO_HISTORY, SMALL_SAMPLE (<20 games)}; fringe (replacement-level) prior blended with weight `60/(60+n)`. Not persisted;
recomputed per run. Persisted outputs: `predictions_player.meta_*` (expected TOI / PP TOI / shots / goals / assists /
points / saves / shots faced, TOI p10-p50-p90, `projection_quality` {STANDARD, FULL, PRIOR_HEAVY, DEGRADED_ROLE},
`role_confidence`, `ev_slot`, `pp_unit`, `deployment_source`, `n_games_history`, `uncertainty_flags`) and
`packet.player_shadow.blocks[].top_players` (16 per game: `exp_goals, exp_points, p_point, p_goal, exp_toi_min, exp_pp_toi_min`).

Historical per-skater-game projections with outcomes exist as research artifacts:
`docs/research/player_sim_v1/skaters_2025.parquet` 47,231 rows x 33 cols (`p_first, exp_toi_ev, exp_toi_pp, p_g1..3, p_a1..2,
p_p1..3, e_g, e_a, e_p, y_g, y_a, y_p, toi_s, quality, role_conf, flags`), `goalies_2025.parquet` 2,624 rows (`e_saves, sd_saves,
p_s14..p_s40, y_saves, y_sa, p_replaced`), same for 2024.

### 5.3 Goalies

`context/goalie_stats` (800 rows/refresh): NHL stats `goalie/summary` cur+prev (135 rows: `savePct, saves, shotsAgainst,
goalsAgainstAverage, gamesStarted, wins, shutouts, timeOnIce`) + MoneyPuck goalies cur+prev (665 rows: `xGoals, goals,
highDanger*, icetime, situation`). `features/build.goalie_factor_table` regresses GA/xGA (prior 60 xG) or SV% (800-shot
prior). V2 `features/goalie_talent.py` uses MoneyPuck shots (half-life 120 appearances, prior 300 xG chosen on a
validation season, b2b multiplier 1.018). Goalie status ladder confidences 0 / 0.70 / 0.85 / 0.985
(`goalies/state.py:25`); sim uses a mixture factor. Live observations: 1,840 DailyFaceoff + 36 boxscore rows.

### 5.4 Lines, injuries, rosters

`context/lines` latest: 1,044 rows / 26 teams (today's teams only): units `pp1 130, pp2 130, pk1 104, pk2 104, f1-f4 78 each,
d1-d3 52 each, g 52, ir 56`; `lines_source` e.g. "Kristy Flannery", "Last Game (2026-10-01)", "Projected"; `lines_updated_at_utc`.
Across the 54 partitions: 32 teams, days per team {3: 19 teams, 2: 10, 4: 1, 1: 2}. `players/roster.Deployment` maps
to slot templates (`EV_SLOT_SHARE f1 .335 .. d3 .265`, `PP_SLOT_SHARE pp1 .62`). Confirmed only when `updatedAt`
within 12 h and source indicates warmups/morning skate (`docs/POINT_IN_TIME.md`).

`context/injuries`: 109 rows latest (IR 93, Out 8, DTD 7, Suspension 1), `team_id` resolved for 100% (31 teams listed),
`espn_id` null, `return_date`, `detail`, `long_comment` (<= 300 chars). Not an input to V1; used by the player arm to
drop players from projected lineups and shown in `packet.games[].context.injuries` and app `event_detail.context.injuries`.

### 5.5 Markets

Board row (checkpoint `kalshi/markets/dt=2026-10-03/kalshi_markets_20261003T030234Z_37089249504.jsonl.gz`, 2,918 rows,
253 KB): raw Kalshi fields (`yes_bid_dollars, yes_ask_dollars, no_*, last_price_dollars, volume_fp, open_interest_fp,
liquidity_dollars, status, result, rules_primary, close_time, ...`) + `_quote_cents` (5 ints) + `_family` + `_support` +
`_observed_at_utc` + `_run_id`. Family mix at the last capture (`STATUS_capture.by_family`): game_total 198, team_total 180,
period_winner 162, period_total 162, period_spread 108, game_winner 96, game_spread 88, game_overtime 18, game_early_goal 18,
season_awards 546, season_champion 481, season_player_stats 496 (player props for 10-03 had not opened at that tick;
earlier checkpoints carry player_goals 101 / player_points 92 / player_assists 76). Support: MODELABLE 562, RESEARCH 1,427,
UNMODELABLE 547, BUILDABLE 18.

Delta file (`kalshi.board.delta/1`): `added, changed {ticker: {field: new}}, removed, cleared, base_path, base_sha256,
parent_board_sha256, board_sha256, seq` — chain of 12 deltas per checkpoint; broken chains fail closed (tested).
Per-ticker quote-change counts across all 496 deltas (`quote_fields` = bid/ask/last): KXNHLTOTAL 389 tickers median 67
changes (max 191); KXNHLSPREAD 226 / 45; KXNHLGAME 138 / 42.5 (max 169); KXNHLTEAMTOTAL 360 / 35; KXNHL1P 110 / 35;
KXNHLGOAL 835 / 25; KXNHLPTS 729 / 29; KXNHLAST 564 / 26; KXNHLFIRSTGOAL 702 / 5. Reconstructed series for
`KXNHLGAME-26OCT01EDMVAN-EDM`: 309 points from 2026-09-29T12:42Z (0.63/0.66) to 2026-10-02T04:49Z (0.99/1.00).

Order books: 300 per tick, `yes`/`no` ladders as `[[cents, contracts]]` + raw dollars; e.g. KXNHLGAME-26OCT03BOSMIN-BOS
NO side 55c..64c with 85,006 contracts at 63c.

Historical (`data/history/kalshi/MANIFEST.json`): `n_schedule_games 5668`, join to NHL game by date + team pair; KXNHLGAME
3,076 markets (2,950 joined; results yes 1,537 / no 1,537), KXNHLTOTAL 7,813, KXNHLSPREAD 5,138, KXNHLFIRSTGOAL 24,006,
KXNHLOVERTIME 49; period/team-total/BTTS series had **0 historical markets** (new in 2026-27). Candles
`candles_core.parquet` 1,468,183 rows x 18 cols: `ticker (10,758), series_ticker {KXNHLGAME,KXNHLTOTAL}, interval {1,60},
game_id (1,477), end_period_ts, yes_bid/ask_{close,open}, yes_bid_high, yes_ask_low, price_close (58.5% null = no trade),
volume, open_interest`; windows hourly [start-48h, start+1h], minute [start-3h15m, start+5m]. Player candles: GOAL 125,297
rows / 33,408 tickers / 916 games; AST 98,668 / 27,236; PTS+SAVE 76,925 / 26,241 (11,191 PTS jobs deadline-skipped).

### 5.6 Projections and simulation

V1 (`nhl-sim-1.1`): 20,000 draws, Poisson 57-min window + 3-min empty-net window (x4.0 / x1.8) + OT/SO rules;
output arrays priced for every contract (`pricing/price.py`). V2 (`nhl-sim-2.0`): half-minute steps, score-state x time
hazards from `data/params/nhl-sim-2.0.json` (fit on 13,118 team-games, 39,431 goals), per-period goals. PLAYER_SIM_V1
(10,000 draws) allocates each V2 goal to scorer / A1 / A2 by strength state, saves via NegBin + pull hazard.
Per-run persisted: slate rows (`p_data_only, p_data_only_se, p_market, p_market_anchored (0.8 market weight),
executable_p_yes/no, edge_*_raw, edge_*_after_fee, gate, support, horizon_label, input_snapshot_ids`), packet `model.sim`
(`total_quantiles, margin_quantiles, total_ladder, home_puckline_ladder, home/away_team_total_ladder, margin_pmf, total_pmf,
p_home_win, p_overtime, p_shootout, p_btts`), `v2_shadow.blocks[].v2.periods`, `player_shadow.blocks[]` (goalies saves
ladders, top_players, lineups deployment_source / n_dressed / p_pp_share, correlation matrix of {home goals, away goals, home win}
+ priced player contracts), `thesis_card.games[]` (18 scripts, 27 thesis events, full_board with primary thesis / phi /
top script per contract, portfolios A/B/C/R, card entries).

Prediction history per ticker (all `predictions` partitions): 61,001 rows, 4,691 tickers, 34 games; runs per game 10-25
for completed games (e.g. 2026020021: 25), 2 for today's. Example V1 series `KXNHLGAME-26SEP30LACOL-COL`: 25 points,
`p_data_only` 0.6462 (T-21h) -> 0.6289 (T-10m) while `p_market` 0.645 -> 0.655.

### 5.7 Settlement, evaluation, CLV, postmortems

Settlement (`workflows/settle.py`): candidates = games started >= 3 h ago within a 72 h backlog; writes `results`
(home/away reg + final, `last_period_type`, starting goalie ids, `stat_correction_version`), `settlements` (`outcome`
YES/NO/VOID/PUSH/UNSETTLEABLE, `idempotency_key`, `engine_version` nhl-settle-1.0 / nhl-period-settle-1.0 /
nhl-player-settle-1.0), boxscore-CONFIRMED goalie observations, and `player_events/*` per game. 30 settlement tests.
Latest `STATUS_settle.json`: 16 games COMPLETE, `by_outcome YES 51 / NO 130 / UNSETTLEABLE 2` for the last game.

Evaluation rows (`evaluations`, 58,302 unique by `(prediction_id, settlement_key)`, 21 games, 3,946 tickers): fields
`y, p_data_only, p_market, p_market_anchored, entry_yes_ask, entry_no_ask, close_prob, close_observed_at_utc, clv_yes_prob,
clv_no_prob, clv_signed, hours_before_start, horizon_label, pregame, settlement_engine, home/away_goalie_status`.
`model_version` is **DATA_ONLY_V1 for 100% of rows** — V2 shadow predictions are never evaluated. Player rows
(`evaluations_player`, 27,407, 16 games, 516 players) add `p_player, projection_quality, role_confidence, deployment_source`.

Same-row scoring computed in this audit (pregame rows with both a model and a market probability):

| set | n | games | V1 Brier | market Brier | anchored Brier |
|---|---:|---:|---:|---:|---:|
| all V1-priced | 10,725 | 21 | 0.1901 | 0.1899 | 0.1897 |
| game_winner | 858 | 21 | 0.2482 | 0.2597 | — |
| game_spread | 1,716 | 21 | 0.1991 | 0.2013 | — |
| game_total | 3,861 | 21 | 0.1504 | 0.1457 | — |
| team_total | 4,290 | 21 | 0.2106 | 0.2112 | — |
| last pregame row per ticker | 525 | 21 | 0.1912 | 0.1914 | — |
| PLAYER_SIM_V1 player_goals | 7,141 | 16 | 0.1361 | 0.1366 | — |
| player_points | 6,597 | | 0.1722 | 0.1734 | |
| player_assists | 5,523 | | 0.1538 | 0.1562 | |
| first_goal | 2,813 | | 0.0256 | 0.0256 | |
| goalie_saves | 156 | | 0.2181 | 0.2327 | |

Mean `clv_signed` (V1 side vs last pre-start tick): -0.0105 over 10,725 rows. `eval/report.json` (production) reports
V1 n 10,725 Brier 0.1901 / log loss 0.5636 / ECE 0.0565 and 10-bin calibration tables per family x horizon;
`eval/report_player.json` PLAYER_SIM_V1 n 27,241 Brier 0.1186 / ECE 0.0111 (all rows incl. one-sided quotes).
`eval/report_thesis.json`: slate 2026-10-01 `COMPLETE 8/8`, 30 final-card bets, thesis hit rate 0.391, mean CLV -0.0062;
by fidelity class DIRECT 11 (8 won), FRAGILE 15 (2 won), STRUCTURAL 4 (4 won); 2026-10-02 `COMPLETE 5/5`.

Historical (research): `docs/research/WALK_FORWARD_V1.md` 2,624 games, moneyline Brier 0.2419 vs league_poisson 0.2483;
`WALK_FORWARD_V2.md` SIM2_ST 0.2417, P(OT) calibration fixed (0.2126 vs observed 0.2275); `MARKET_BENCHMARK.md` (1,312
games 2025-26, candle mids) Kalshi 0.2443 vs V1 0.2459 at T-60m, model coefficient given market z = 0.5 (V1) / 0.8 (V2)
— no significant information beyond the market on moneylines; `PLAYER_SIM_V1.md` §6 goals: model 0.1742 = market 0.1742,
coefficient z 2.2-3.7 (tiny but survives game bootstrap).

### 5.8 Schedules, seasons

`context/schedule` row: `game_id, season "2026-27", season_type, game_date_et, start_time_utc, home/away_team_id, abbrevs,
status {not_started, live, final, postponed, canceled}, venue, neutral_site, home/away_score, last_period_type,
source_game_state`. 65 games 2026-10-03..2026-10-11 in the latest snapshot. Conductor `SEASON_CALENDAR` has 2026-27 only.

---

## 6. Opponent-adjustment audit: **none**

- Team ratings (`features/ratings.py:team_rating`) are per-team exponentially weighted raw rates; there is no
  adjustment for the quality of opponents faced. The opponent enters only in `expected_goals` as a multiplicative
  factor `(off_A/L) * (def_B/L)` at prediction time (simple, non-recursive). Baseline = league xG/60 from rows before
  the date (`league_rates`, needs >= 60 team-games in the current season else blends the previous one). Sample
  requirement: `games_used < 5` flags `trusted=False` (`features/build.py:118`). Tests pin point-in-time only
  (`tests/test_walk_forward.py::test_ratings_are_point_in_time`, `test_predict_game_ignores_own_and_future_rows`).
- Special teams (`features/special_teams.py:team_st`) same construction with situation-specific priors
  (PP/PK prior 180 minutes, EV 20 games, penalties 25 games); tested for cutoff and regression
  (`tests/test_v2_model.py::test_special_teams_regress_small_samples_and_respect_the_cutoff`).
- Goalie talent (`features/goalie_talent.py`): shrunk GA/xGA, no shooter-quality adjustment beyond xG itself.
- Player profiles: shrunk toward position / fringe priors; no opponent or goalie-faced adjustment.
- MoneyPuck columns `scoreVenueAdjustedxGoals*`, `flurryScoreVenueAdjustedxGoals*` are score/venue/flurry adjusted,
  not opponent adjusted, and are not used by the model (only `xGoalsFor/Against`).
- The thesis governance "opponent-adjustment labels" (`docs/HANDOFF_REPAIR_PASS.md`) are text labels on bets, not a
  computation.

Limitation for the app: any "opponent-adjusted" claim would be UNAVAILABLE; "schedule-adjusted" metrics must be computed
fresh (all inputs exist: per-game xG for/against with `opp_team_id`).

---

## 7. Inventories

### 7.1 Time-series-capable datasets

| dataset | x-axis | keys | rows | game id | opponent id |
|---|---|---|---|---|---|
| MoneyPuck team game log (history + live) | game date / game | `team_id`, `gameId`, `situation` | 44,736 + 2,820 | yes | yes (`opp_team_id`) |
| Official player game log | game | `player_id`, `game_id`, `team_id` | 251,797 + 756 | yes | derivable (`home/away_team_id`) |
| Goalie game log | game | `player_id`, `game_id` | 27,970 + 84 | yes | derivable |
| Shots / goals (event level) | game time (`t_s`) | `game_id`, `shooter_id`/`scorer_id` | 824k / 43k | yes | derivable |
| Team ratings as computed | run (`generated_at_utc`) | `game_id`, side | 100 runs x games | yes | yes |
| Model probabilities per contract | run (`predicted_at_utc`) | `ticker`, `game_id` | 61,001 / 55,713 / 27,810 | yes | via contract `opponent_team_id` |
| Kalshi quotes (live) | capture tick (`captured_at_utc`) | `ticker` | 520 ticks x ~2,700 markets | via contracts join | via contracts |
| Kalshi order books (live) | tick | `ticker` | 520 x 300 | same | same |
| Kalshi candles (historical) | `end_period_ts` (1 min / 60 min) | `ticker`, `game_id` | 1.97M | yes | derivable from ticker |
| Evaluations / CLV | prediction instant, `hours_before_start` | `prediction_id`, `ticker` | 58,302 + 27,407 | yes | via contract |
| Goalie observations | `observed_at_utc` | `game_id`, `team_id` | 1,876 | yes | — |
| Lines | `observed_at_utc` / `lines_updated_at_utc` | `team_id`, `player_id` | 24,612 | no (team-day) | — |
| Injuries | snapshot | `team_id`, `player_name` | 8,304 | no | — |
| Historical walk-forward predictions | game date | `player_id`, `game_id` | 94,456 skater-games, 5,248 goalie starts | yes | derivable |

### 7.2 Split-capable datasets

| dataset | split dimensions stored | counts |
|---|---|---|
| MoneyPuck team games | `situation` {all, 5on5, 5on4, 4on5}; `home_or_away`; `game_type` {2, 3}; `playoffGame` | 11,152 rows/season = 4 x 2,788 |
| Official player games | strength state {EV, PP, SH, EA, EN, OT} for TOI / GF / GA / SOG / attempts; `is_home`; `game_type`; `position` | 50,183 rows x 6 states |
| Goalie games | {EV, PP, SH} saves / shots against; `starter`; `is_home` | 5,575 |
| Goals | `strength` {EV 6,xxx.., PP, EN, SH, EA}; `period` 1-5; `period_type` {REG, OT}; `score_for_before`/`score_against_before`; `shot_type` (11); `empty_net` | 8,572 (2025) |
| Shots | `kind` {BLOCK, SOG, MISS, GOAL}; `strength`; `zone`; `period`; `shot_type` | 163,021 |
| MoneyPuck shots | `period`; `homeSkatersOnIce`/`awaySkatersOnIce`; `homeEmptyNet`/`awayEmptyNet`; `isPlayoffGame`; score state (`homeTeamGoals`/`awayTeamGoals`) | 119,271 |
| Rosters | `shoots_catches` {L, R}; `position` {C 216, L 117, R 114, D 249, G 70} | 766 |
| Evaluations | `family` (12), `horizon_label` (8 buckets: T-12h 3,600 .. T-10m 125 for V1 rows), goalie statuses | 58,302 |
| Player evaluations | `family` (5), `projection_quality`, `deployment_source`, `role_confidence` | 27,407 |
| Kalshi candles | `interval` {1, 60}; series | 1.97M |
| Not stored: home/away splits of ratings, rest-day buckets (rest days computed per game: `home_rest_days`, `home_b2b` in packet only), handedness splits of performance, surface/venue effects. |

### 7.3 Market-history inventory

- **Live per-ticker series: yes.** Source `kalshi/markets` (24 checkpoints, 1 per ~12 ticks or new UTC day) +
  `kalshi/markets_delta` (496). Granularity 5-15 min horizon-aware (`worker/plan.py`: far 15 / near 10 / tight 7 /
  final 5 min). Retention: permanent (append-only branch). Size: 35 MB for 4.5 days (markets 6.0 + 8.9 MB, orderbooks
  1.1 + 19 MB) ~ 7.8 MB/day -> ~1.4 GB per 180-day season at current cadence. Reconstruction API:
  `archive/reconstruct.{reconstruct_at, latest_board, iter_board_ticks, iter_market_rows}`; `test_delta_reconstruct.py`
  pins "every tick visible", "no later delta leaks into an earlier reconstruction", fail-closed on corruption.
- **Order-book depth series: yes**, 300 markets per tick (priority-sampled), full ladders.
- **Historical candles: yes** (one-off pull), 1-min (GAME, TOTAL) and hourly (GAME, TOTAL, SPREAD, GOAL, AST, PTS) for
  2025-26, best bid/ask close per period, volume, open interest; no depth. 15 MB + 2.2 MB parquet.
- **Entry/close quotes per prediction**: `predictions.market_yes_bid/ask, market_no_bid/ask, market_observed_at_utc` and
  `evaluations.close_prob/close_observed_at_utc`.

### 7.4 Projection inventory

| output | per entity? | per market? | distribution or point? | stored across runs? |
|---|---|---|---|---|
| `predictions` (V1) | game via ticker | yes, every joined contract | point + SE (`p_data_only_se`) | yes, 93 partitions / 100 runs |
| `predictions_v2` | game | yes | point + SE | yes, 87 |
| `predictions_player` | player (`player_id`) | yes | point + SE + expected stats + TOI p10/p50/p90 | yes, 52 |
| `packet.games[].model.sim` | game | — | distribution: quantiles, ladders, PMFs | yes, inside each run's `packet.json` (not a ledger kind) |
| `packet.player_shadow.blocks[]` | player (top 16 / game) and goalie | — | exp G/A/P, p_goal, p_point, saves ladder 15+..40+ | yes (packet) |
| `thesis_games` | game | — | 18 script frequencies, 27 event probabilities, portfolios | yes, ledger kind (293 rows) |
| `thesis_decisions` | bet | yes | p_model, p_adjusted, p_kalshi_mid, EV, Kelly stake, script mapping | yes (1,947 rows) |
| Historical walk-forward (`docs/research/player_sim_v1/*.parquet`) | player-game, goalie-start | — | ladders p_g1..3, p_a1..2, p_p1..3, p_s14..40 | 2 seasons, one-off |

---

## 8. Existing ranking / percentile / league-average code

- League averages: `features/ratings.league_rates` (xG/60, G/60, `n_games`, `as_of`), `special_teams.league_st`
  (`ev_xg60, ev_conv, pp_xg60, pp_conv, sh_g60, ppmin, ev_min, other_goals, g60_all`), `players/features.league_priors`
  (position x state `ixg60`, `a1`, `a2`, `share_median`, `en60`, `finish`, `unassisted`, `no_a2`, fringe priors),
  `goalie_talent.GoalieBook.league_ratio` (730-day GA/xGA), `players/saves` point-in-time league shots per game
  (last 1,200 team-games).
- Percentiles: only on simulated outputs — `sim/engine.py:163` total/margin quantiles, `shadow_player.py:282` TOI
  p10/p50/p90, `thesis/portfolio.py:208` and `thesis/engine.py:360` P/L percentiles, `research_layer.py:102`.
- Rankings: none for teams or players. `thesis_card.games[].scripts[].players_most_involved` ranks players by
  `p_point` lift within a script (top few). `eval/report_thesis.json` has `realized_vs_projected_script.rank`.
- Calibration helpers: `evaluation/metrics.py` (`brier, log_loss, calibration_table, ece, reliability slope/intercept,
  brier_skill_score, metrics_by_group`), `evaluation/authority.eligibility`.

---

## 9. Size estimates (bytes, JSON unless noted)

| layer | basis | estimate |
|---|---|---|
| (a) Team profiles (32 teams) | rating block ~0.4 KB + official summary ~0.7 KB + situational rates ~1 KB + last-10 game rows (15 cols) ~3 KB | **~5 KB/team, ~160 KB total**; full MoneyPuck season log per team (82 x 120 cols ~1.5 KB/row) adds ~125 KB/team (4 MB all teams/season) |
| (b) Player profiles (~1,060 skaters+goalies) | profile ~1 KB (shares, ixg60, finish, a1/a2, flags) + season totals ~0.5 KB | **~1.5 MB** all players; per-game rows 50k x ~0.6 KB = **~30 MB/season** (parquet zstd 0.75 MB) |
| (c) Per-game detail | live `player_events` for one game: players 36 rows + goalies 4 + goals ~7 + shots ~115 + coice ~210 gz = 9 KB, JSON ~70 KB; packet game block ~240 KB (3.1 MB / 13 games) | **~70 KB/game detail, ~300 KB with full packet**; 1,312 games/season -> ~90 MB detail (12 MB gz) |
| (d) Metric time series | team rating per game-run ~0.2 KB x 32 x 82 = 0.5 MB/season; player rolling metrics 50k x 0.2 KB = 10 MB/season; model-probability series ~40 B/point x 14 runs x 4,700 tickers = 2.6 MB per 5 days | **~0.5-10 MB per series family per season** |
| (e) Market history | live: ~2,000 game-family tickers x ~50 points x 40 B = **4 MB per 5 days (~150 MB/season) JSON**; raw gz on branch 7.8 MB/day; order books 19 MB/4.5 days gz; historical candles 15 MB parquet (core) + 2.2 MB (player) | lazy-load per ticker (<= 12 KB each) |

---

## 10. Recommended research capabilities to expose in this pass (evidence-backed)

**Expose (VERIFIED / strong PARTIAL):**
1. Team game logs with xG / Corsi / Fenwick / danger / adjusted xG per game and situation, with opponent and home/away,
   4 seasons + current (`data/history/moneypuck/*`, `context/team_games`, `context/team_games_st`).
2. Official player game logs with TOI by strength state, SOG/attempts by state, on-ice GF/GA, A1/A2, PP goals; goalie
   logs with saves split EV/PP/SH; 5 seasons + live (`data/history/players/*`, `player_events/*`).
3. Event-level goals (scorer, A1, A2, strength, score state, on-ice skaters) and shot attempts with x/y and own xG
   (recompute via `players/xg.py` which is versioned in `data/params/player-sim-1.0.json`).
4. Point-in-time team ratings and lambda decomposition per game per run (packet `team_state`, `model.components`), and
   the full game distribution (ladders, PMFs, quantiles) for the latest run; model-probability history per ticker from
   `predictions*` kinds.
5. Market quote time series and order-book depth per ticker (reconstruct from checkpoints + deltas), plus entry/close
   quotes and CLV per prediction; historical candle series for 2025-26 markets.
6. Goalie status timeline per game (UNKNOWN -> PROJECTED -> PROBABLE -> CONFIRMED) and today's line combinations /
   PP units with source and freshness; injuries list per team (current snapshot + 5-day history).
7. Settled outcomes and calibration: per family x horizon Brier / log loss / ECE / reliability tables (V1 and
   PLAYER_SIM_V1 vs Kalshi mid), postmortems per bet.

**Mark RESEARCH:** DATA_ONLY_V2 outputs (never prospectively evaluated), special-teams decomposition, goalie true talent,
player profiles / expected TOI (shadow arm, `role_confidence` MEDIUM/LOW, `projection_quality` STANDARD for 98% of rows),
thesis scripts / events / portfolios, historical walk-forward datasets, sportsbook consensus (quarantined),
`docs/research/player_sim_v1` per-game projection parquets.

**Mark UNAVAILABLE:** opponent-adjusted metrics, schedule strength, fixed recent-form windows (unless computed in the
app from the game logs), rankings/percentiles (none exist), wager outcomes / P&L (ledger empty), weather, venue effects,
play-by-play beyond shots/goals (faceoffs, hits, penalties as events), historical lines / injuries before 2026-09-29,
player-level handedness or home/away performance splits (derivable, not stored).

---

## 11. Open questions / UNKNOWN items

1. **data-archive history integrity**: the shallow fetch reported a "forced update" from the previous phase's ref
   `94c5a95` to `1101a27`; with `--depth=1` the fast-forward relation cannot be checked locally. The manifest is
   append-only (2,326 entries, all present on disk, sha256 per file) and `Ledger.verify()` runs before every push, so
   this is almost certainly a shallow-fetch artifact, but a full fetch should confirm no rewrite.
2. **Refresh of history parquet**: `history.yml`, `research_data.yml`, `player_research_data.yml` are manual-dispatch
   only (last runs 2026-09-29 / 2026-09-30, all success). The 2026-27 season accumulates only via `context/team_games`
   (team) and `player_events/*` (player); nothing re-pulls MoneyPuck shots or Kalshi candles for the live season.
3. **`context/team_games` redundancy**: 72 near-identical 899 KB copies (64.7 MB) — only the 2026-27 rows change; a
   research layer should read the latest partition only.
4. **DATA_ONLY_V2 accuracy prospectively**: `predictions_v2` has 55,713 rows but zero `evaluations` rows; the thesis
   layer prices on the V2/player draw, so V2 is only assessed indirectly through `thesis_postmortems`.
5. **Player candle coverage** is incomplete (PTS 28,652 of 40,000; GOAL 33,408 of 37,957 fetched; ~1% 429 errors);
   `KXNHLSAVE` and all period / team-total series had no settled history before 2026-27.
6. **ESPN injuries** carry no player id (`espn_id` null in sampled rows); joining to NHL ids needs name matching.
7. **Games without shift charts** (57 of 1,398 in 2024-25) have `shifts_ok=False` and no TOI-by-state; these rows are
   excluded from `build_player_games`.
8. `context/goalie_stats` MoneyPuck rows for the *current* season are 175 rows at the latest refresh (Last-Modified
   2026-10-03); whether MoneyPuck goalie/team summaries stay available all season is a source risk noted in
   `docs/NHL_DATA_SOURCE_AUDIT.md`.
9. Not run in this audit: the full pytest suite (418 tests across 32 files; CI on `main` was green at
   2026-10-02T23:20Z per `gh run list`); pandas import takes ~34 s in this container on first load.

Scratch artifacts: `(local scratch, not committed)`
(parquet profiler used for every schema table above).
