# Phase 2 research-data audit — MLB (`chmoses98/edge-finder-api`)

Audited at `main` = `c5e3e68` (2026-10-03). Read-only. Working copy: `the repository checkout`
(shallow clone, 47 reachable commits — git-history cadence could NOT be measured from log; cadence
below comes from workflow crons + partition file dates). Research branches were shallow-fetched
(`origin/research/mlb-alpha-0002-prospective`, `origin/research/mlb-alpha-prospective`,
`origin/research/mrv-prospective-v1`) and inspected with `git ls-tree`/`git show` only.
Scratch: `(local scratch, not committed)`
(`partition_stats.py`, `obs_depth.txt`, `stats_*.txt`, `remote_branches.txt`).

---

## 1. Summary

- MLB is by far the richest of the Edge Finder repos: 3.3 GB checkout, `data/` = ~2.9 GB, 449 test
  files / 9,477 `def test_` functions (parametrised count is higher), ~50 workflows, 63k lines in `lib/`.
- **Strongest, production-grade**: (a) the full Kalshi MLB market universe captured as append-only
  EdgeLab JSONL partitions on `main` — 252,625 distinct tickers, 757,724 observations, 247,025
  settlements, 304,321 CLV quotes, 2026-08-01 → 2026-10-02; (b) the daily authoritative slate
  (`data/slates/<date>/authoritative.json`, 63 dates, 820 games, 2026-06-16 → 2026-10-01) that embeds
  every model input per game (starter Savant metrics, bullpen, team form, park, lineup confirmation,
  11-market model ledger with projections); (c) a canonical bet ledger with CLV (598 rows, 155 REAL).
- **Genuine research gold on `main`**: pitch-level Statcast for 643 completed games (190,107
  pitches, 2026-08-11 → 2026-09-27, 518 batters / 629 pitchers) and a 5-season (2022-2026) MLB
  Stats-API cache of team batting box lines, per-game pitcher lines (pitches/outs/K/BB) and per-team
  schedules with scores (11,723 games) in `data/research_cache/`.
- **Weakest**: market *price history per ticker is shallow* (median 3 observations per ticker; one
  ticker lives ~1 day); the model only prices 11 game-level markets (hitter/pitcher props are
  `NO_MODEL_SUPPORT`, 88% of model_evaluations carry no probability); player-level *season* stats are
  current-snapshot only (overwritten daily); no weather beyond dome flags, no play-by-play beyond the
  Statcast pitch log window, no injuries, no umpire data with values.
- Opponent adjustment exists and is traceable (`scripts/fetch_opp_quality.py`: rolling-15-game
  opposing-starter xFIP, `(avg-4.00)*0.08` capped ±0.2) but is simple, not recursive, and only its
  daily output is kept (history only inside each day's authoritative slate).
- Identity is MLB Stats API native: `gamePk` for games, MLBAM `playerId` for players, team
  abbreviations (with `ARI→AZ`, `OAK→ATH` normalisation) for teams; Kalshi tickers are verbatim.
  Known wart: 34.5% of observation rows carry the fallback `YYYY-MM-DD_AWAY_HOME_HHMM` gameId
  instead of the gamePk, and gameId is a string in some partitions and an int in others.

---

## 2. Data branches and layout

| Branch | Role | Root(s) | Immutable vs replaced | Size |
|---|---|---|---|---|
| `main` | Production. All workflows that run on a schedule commit here (`scripts/ci/git_data_commit.py`). | `data/` (2.9 GB), `app/latest/` (1.4 MB) | See table below | checkout 3.3 GB |
| `research/mlb-alpha-0002-prospective` | MLB-ALPHA-0002 prospective capture (cron `3,13,…,53 15-23,0-4 * * *`, `research-mlb-alpha-0002-capture.yml`); last commit 2026-10-03 05:01Z | adds `data/edgelab/research_artifacts/mlb_alpha_0002/prospective/{books,quotes,trades,odds,mlb_state,shadows,runs,*_unchanged,capture_state.json}` (310 files, 55.9 MB, 2026-09-02 → 2026-10-01) and retains 2026-08-13…09-03 registry snapshots that `main` pruned (21-day retention) | append-only | +403 MB vs main |
| `research/mlb-alpha-prospective` | C01-PIT shadow (`research-c01pit-shadow.yml`, cron `*/10`), last commit 2026-09-03 — stale | retained registry snapshots 2026-08-30…09-02 only; artifacts under `research_artifacts/mlb_alpha_0001/` are already on main | append-only | ≈ main + snapshots |
| `research/mrv-prospective-v1` | MRV market-structure collector (hourly `11 * * * *`), last commit 2026-09-24 | `data/edgelab/research_artifacts/mrv_prospective/v1/{kalshi_quotes,kalshi_books,kalshi_trades,kalshi_crosssection,sportsbook_odds,sportsbook_joins,mlb_state,runs,health,state}` — only 2 dates (2026-09-23, 09-24), 100 files, 44.3 MB; `kalshi_trades/2026-09-23.jsonl.gz` alone = 476,756 trade-tape rows | append-only | +142 MB vs main |
| Not in git | MLB-ALPHA-0002 recovered Kalshi 1-minute candlesticks + trade tape (~480 MB) — `.gitignore`d, published as a GitHub Release by `research-publish-raw-dataset.yml`; manifest at `data/edgelab/research_artifacts/mlb_alpha_0002/raw_data_manifest.json` | | | |

`main` `data/` inventory (`du -sh`, file counts):

| Path | Size | Files | Immutable? | Produced by |
|---|---|---|---|---|
| `data/slates/<date>/` | 691 MB | 289 (75 dates; 63 `authoritative.json`, 44 `official_*`, 64 `scheduled_refresh_*`, 36 `recheck_*`, 82 `rejected_contaminated_*`) | per-date files frozen once protected (`scripts/protect_slate.py`) | `fetch-slate.yml` (cron 16:00/20:00/22:00 UTC) + `lineup-recheck.yml` |
| `data/edgelab/` | 601 MB | 5,866 | JSONL partitions append-only; gz-compacted after a few days | many (below) |
| `data/edgelab/snapshots/<date>/{pre_game_decision,closing_line,post_game_settlement}/frozen/*.jsonl.gz` | 357 MB | 3,832 | immutable (manifest + content hash) | `create_snapshot.py` in fetch-slate / postgame |
| `data/kalshi/discovery/<date>*.json` | 520 MB | 274 (2026-07-30 → 2026-10-02) | per date, overwritten by re-run | `discover-kalshi-mlb-markets.yml` (workflow_run after fetch-slate) |
| `data/kalshi_registry_snapshots/kalshi_search_<date>[_HHMM].json` | 319 MB | 268 (daily file 2026-06-08 → 10-02; intraday copies kept 21 days) | dated file kept forever; timestamped pruned (`prune_kalshi_snapshots.py`) | `capture-snapshots-scheduled.yml` (`0,30 16-23,0-5 UTC`) + fetch-slate + lineup-recheck |
| `data/pipeline/<date>/` | 449 MB | 522 (61 dates, 2026-07-30 → 2026-10-01: `normalized_slate`, `projections`, `projection_board`, `recommendations`, `execution`, `validation`, `provenance`, `protection`, `full_market_coverage`) | overwritten per run of that date | fetch-slate |
| `data/statcast_raw/` | 161 MB | 645 (643 game files + 2 indexes) | append-only, idempotent | `statcast-postgame-archive.yml` (cron `0 10 * * *`) |
| `data/handicapping_card/<date>.json` + `latest.json` | 72 MB | 15 (2026-09-17 → 10-01) | per date | `build-handicapping-card.yml` (workflow_run after fetch-slate) |
| `data/research_cache/` | 46 MB | 346 | one-off research caches | `research-multiseason-*.yml` (manual; cache committed to main via PRs) |
| `data/research/` | 18 MB | 128 | `wagers.jsonl` rebuilt daily (`build-wager-research.yml` 07:15 UTC) | |
| `data/handicap_runtime/<date>/{manifest.json,games/<gamePk>.json.gz}` | 13 MB | 325 | per date | build-handicapping-card |
| `data/kalshi_research_night_before_snapshots/` | 2.8 MB | 7 (2026-09-27 → 10-03) | append | `research-night-before-capture.yml` (00-05 UTC hourly) |
| `data/kalshi_odds_history.json` | 1.8 MB | 1 (4,755 rows) | legacy, grows | legacy CLV capture |
| `data/clv/<date>/`, `data/clv_snapshots/<date>/` | 1.0 MB / 0.4 MB | 57 / 50 | per date | `clv_capture.yml`, `capture-closing-lines.yml` (`*/5`) |
| `data/*.json` (teamstats, savant_team, bullpen, oppquality, team_offense_form, weather, odds, kalshi_*, meta, pitchers, slate) | ~1.5 MB | ~20 | **overwritten every fetch — current snapshot only** | fetch-slate |
| `data/{f5_audit,lineup_audit,execution_slip}_<date>.{csv,json,txt}` | ~10 MB | ~330 (2026-06-16 → 10-02) | per date | fetch-slate |
| `data/edgelab/schema_v1/*.schema.json` | 216 KB | 21 | versioned | hand-maintained |

---

## 3. Identity model

| Entity | Canonical id | Where defined | Coverage / caveats |
|---|---|---|---|
| Game | MLB Stats API `gamePk` (int in slates/pipeline/model_evaluations; **string** in `edgelab/games`, `markets`, `settlements`, `clv_quotes`, `bets`) | `lib/edgelab/ids.py:30 build_game_id` — falls back to `YYYY-MM-DD_AWAY_HOME[_HHMM]` when gamePk unknown at ingest | `edgelab/games`: 1,467 rows, 61 dates, `mlbGamePk` null 3.6%, `supersededBy` set on 41.9% of rows (duplicate fallback rows kept, never deleted; readers must follow `canonicalGameId`). `observations`: 34.5% of rows carry the fallback id (261,284 / 757,724). Doubleheaders: fallback id is collision-prone (documented in `ids.py:34`). |
| Team | 30 abbreviations (`ATH, ATL, AZ, BAL, BOS, CHC, CIN, CLE, COL, CWS, DET, HOU, KC, LAA, LAD, MIA, MIL, MIN, NYM, NYY, PHI, PIT, SD, SEA, SF, STL, TB, TEX, TOR, WSH`) + MLB `teamId` (`awayTeamStats.teamId`, e.g. CHC=112) | `scripts/enrich_data.py:72 ABBR_NORMALIZE {'ARI':'AZ','OAK':'ATH'}`; `lib/edgelab/mlb_schedule.py:84` teamId→abbr; `scripts/fetch_opp_quality.py:35` abbr→teamId | `savant_team.json`/`oppquality.json` still use `ARI` (raw Savant/MLB), slates use `AZ`. Kalshi ticker suffix uses its own 2-3 letter codes (e.g. `AZSD`, `CHCBOS`). App export `TEAM_SOURCE = "mlb_team_abbr"` (`scripts/app_export.py:66`). |
| Player | MLBAM `playerId` (string) — pitchers in `away.pitcher.id`, `pitcherSavant` keyed by name, `savant_team.json.batters/pitchers` keyed by id, Statcast `batterId`/`pitcherId`, bullpen `recentUsage.relieversUsedLastGame[].playerId`, hitter snapshots `playerId` (null 4.9%) | Savant CSV + Stats API | No cross-source player map needed (all MLBAM). Kalshi player-prop tickers embed `TEAMNAMEnn` (e.g. `MINJBELL56`) — parsed by `lib/kalshi_mlb_contract_parser.py` / `lib/research/player_prop_parser.py` into `player` name; `markets.player` null 20.8% (non-player markets). |
| Market | Kalshi `marketTicker` verbatim; `eventTicker`, `seriesTicker` (17 series in `data/kalshi_market_registry.json.series_catalogue`) | `lib/edgelab/market_identity.py`, `lib/edgelab/market_universe.py` | 252,625 tickers; `marketFamily` = 15 canonical families (`hitter_hits_runs_rbis, hitter_total_bases, hitter_hits, hitter_rbis, team_total, pitcher_strikeouts, game_total, winning_margin, inning_result, hitter_stolen_bases, inning_total, pitcher_outs, game_result, first_inning_run, unknown`). model_evaluations/recommendations mix those with raw series names (`KXMLBTEAMTOTAL`) and ledger names (`TT_Home_Over`) — 32 distinct values. |
| Bet | `betId` sha1 (see `schema_v1/README.md`) | `ids.py:126` | 598 rows, unique. |
| Event (app contract) | `mlb_game_pk` (`EVENT_SOURCE`, `app_export.py:65`) | | Export falls back to `edgelab/games.mlbGamePk`. |

Cross-source maps: Odds-API `oddsApiEventId` per game in slate; Kalshi `kalshiKey` (`CHCBOS`) and
`kalshiEventTickerSuffix` per game; Pinnacle via Odds-API team names. No ESPN/nflverse.

---

## 4. CAPABILITY MATRIX

| Capability | Status | Origin | Coverage | Cadence | Tests | Notes |
|---|---|---|---|---|---|---|
| Team metrics (season) | VERIFIED (snapshot) / PARTIAL (history) | `data/teamstats.json` (record, RS/RA, L7/L15 RpG), `data/savant_team.json.teams` (xwOBA, FB%), `data/bullpen.json` (ERA/xFIP/WHIP/K9/BB9/HR9/HL-xFIP), `data/oppquality.json`; embedded per game in `slates/<date>/authoritative.json.awayTeamStats/homeTeamStats` + `away/home.bullpen` | 30 teams; current file overwritten daily; history = 63 slate dates (2026-06-16 → 10-01) | fetch-slate 3×/day | `tests/*enrich_data*` (14 files), `*savant*` (24), `*bullpen_usage*` (8) | wRC+ is a proxy (`wrcSource='ops_proxy'`, `rpgIndex = RpG/4.5*100`, `enrich_data.py:87`). |
| Player metrics (season) | PARTIAL | `data/savant_team.json` batters (630: xwOBA scalar + `battersDiscipline` K%/BB%/Whiff%/HardHit%/Barrel%/EV), pitchers (814: xERA/K%/BB%/FB%); starters' full Savant block only inside slates (`pitcherSavant`: xFIP, xERA, K%, BB%, Whiff%, HardHit%, EV, Barrel%, FB%, seasonFIP, recentFIP, avgIPperStart, seasonIP/Starts, TTO split, vsLHH/vsRHH, velocity) | current snapshot only; starters: ~1,640 starter-games across 820 slate games | fetch-slate | `tests/test_fetch_savant_pitchers*` etc. | `vsLHH/vsRHH/velocity*` null in sampled game; per-season history only recoverable from slate copies. |
| Game logs — team per game | VERIFIED (research cache) / PARTIAL (production) | `data/research_cache/batting_backtest/<yr>/boxscores.jsonl.gz` (team batting box: PA/AB/H/2B/3B/HR/BB/HBP/K/SF/R per side) + `bullpen_backtest/<yr>/schedules/<TEAM>.json` (dates, gamePk, scores, isWinner, records) for 2022–2026 | 11,723 games (2,430/season 2022–25, 2,004 for 2026 through late Aug) | one-off manual workflow (`research-multiseason-batting-backtest.yml`) | `tests/*research_cache*` (5), backtest tests | `data/team_offense_form.json.teams[].gameLog` = 30-day log (gamePk, opponent, side, RS/RA, TT line) rebuilt daily — current snapshot only. |
| Game logs — player per game | PARTIAL | `data/research_cache/starter_workload/<yr>/boxscores.jsonl.gz` (every pitcher line per game: pitches, outs, BF, H/R/ER/BB/K, holds/saves, orderIndex) 2022–2026; Statcast `data/statcast_raw/index/{batter,pitcher}_games.jsonl` (13,246 batter-games, 5,629 pitcher-games) | pitchers: 11,723 games; Statcast: 643 games 2026-08-11 → 09-27 | pitcher cache one-off; Statcast daily cron | `tests/test_statcast_pitch_store.py`, `test_fetch_statcast_pitch_log.py`, hitter feature tests | No per-game batter box lines in production; derive from Statcast pitches. |
| Historical opponents / results | VERIFIED (research cache) | schedules above (`teams.away/home.score`, `isWinner`, `seriesNumber`, `gameType`) | 5 seasons | one-off | as above | Also `edgelab/games` (1,467 rows) has no scores; settlements have YES/NO only. |
| Opponent adjustments | VERIFIED (simple) | `scripts/fetch_opp_quality.py` → `data/oppquality.json` → `enrich_data.py:111 compute_offense_baseline` | 30 teams daily; history inside slates (`awayTeamStats.oppQualityAdj/oppXFIPavg/oppQualityGames/oppQualityConf`) | daily | `tests/*oppquality*` (1), `*offense_baseline*` (3) | See §6. |
| Schedule strength | PARTIAL | only the above rolling opposing-starter xFIP; no team-strength SOS | | | | UNAVAILABLE as a team-level SOS. |
| Recent-form windows | VERIFIED | `teamstats.last7RpG/last15RpG`, `team_offense_form.json.windows[L5,L7,L10]` (mean, median, min/max, EWMA, trimmed mean, outlier dependence, `marketRelative` overs vs TT line, `formLabel`) | 30 teams; 30-day lookback | daily (`fetch_team_offense_form.py`, continue-on-error) | `tests/*team_offense_form*` (1) | Explicitly analysis-only (`RESEARCH_OFFENSIVE_FORM.md`). |
| Usage (pitches, PA, IP) | PARTIAL | `bullpen.json.bullpens[].recentUsage` (relievers used last game w/ pitch counts, back-to-back, HL arms); `pitcherSavant.avgIPperStart`; starter_workload cache (pitches/outs per appearance 2022–26); Statcast pitch counts | | daily / one-off | `tests/*bullpen_usage*` (8) | `bullpenUsageAvailableCount: 0` on off-days. |
| Lineups / rotations | PARTIAL | `scripts/fetch_lineups.py` → `awayTeamStats.lineup*` (confirmed flag, source, battersFound/Resolved, `lineupWOBADelta`, `lineupAdj`), `data/lineup_audit_<date>.{csv,json}` (2026-06-16 → 10-02); `handicapping_card/<date>.json.games[].lineups`; hitter feature board (`scripts/build_hitter_feature_board.py`) | 63+ dates | fetch-slate + lineup-recheck | `tests/*lineups*` (28) | The slate itself stores NO batting order (0 occurrences of `battingOrder`/`batters`); names only in lineup_audit files / card. |
| Injuries / availability | UNAVAILABLE | nothing beyond lineup confirmation + bullpen fatigue | | | | |
| Matchup metrics | PARTIAL | `marketLedger[].awayPlatoonContext/homePlatoonContext` (`lib/research/platoon_context.py`: lineup handedness vs starter hand, wOBA split components, `status` often `MISSING_DATA`), `pitcherSavant.ttoSplit/tto1/tto3`, first-inning context (`lib/research/first_inning_context.py`) | per game, per day | daily | `tests/*platoon*` (14) | sampled 2026-09-25 game: handedness `countUnknown: 9`, status `MISSING_DATA`. |
| Projection distributions | RESEARCH | Poisson pmf in `build_market_ledger.py:169-185` (no samples stored); NB shadow cells in `edgelab/mlb_rsch_0011_shadow_evaluations/<date>.jsonl` (33 dates, per-game probability cells for ML/totals/TT/margins under NB vs Poisson); `lib/edgelab/backtest/run_distributions.py`; hitter Monte Carlo (`monteCarloStderr` in hitter snapshots) | | daily (research scheduler) | yes (rsch tests) | No quantiles/samples persisted. |
| Raw projections (point) | VERIFIED | `pipeline/<date>/projections.json` (awayProjRuns, homeProjRuns, f5AwayProj, f5HomeProj, totalProj per gamePk) + `marketLedger[].awayProjRuns/homeProjRuns`, `teamTotals`, `totalEval`, `runLineEval`, `allEdges` | 61 dates / 820 games | 3×/day + rechecks | `*compute_projections*` (9), `*market_ledger*` (88), `*projection_board*` (13) | Formula §6/§7. |
| Market prices (current) | VERIFIED | `edgelab/observations` latest per ticker; `data/kalshi_search.json`; `kalshi_market_registry.json`; slate `kalshi.markets` + `odds.{fanduel,betmgm,draftkings,pinnacle}` | full universe | every 30 min (16–05 UTC) | 62 test files mention observations | |
| Market price history | PARTIAL | `edgelab/observations/<date>.jsonl.gz` | 757,724 rows; 252,625 tickers; **median 3 obs/ticker (p90 5, max 17)**; distinct capture-minutes/day median 6, max 21 | 30-min cron but effective ~6 captures/day | yes | Checkpoint-tagged (FIRST_DAILY, T-90…T-5, CLOSING, POST_START). Research branches hold much denser books/trades (§7). |
| Advanced stats | PARTIAL | xwOBA (team, batter), xERA/xFIP (pitchers), Statcast pitch-level (EV, LA, xBA, xwOBA per BIP, spin, break, release, plate location) | Statcast: 190,107 pitches | daily | yes | Only 48 game-days of Statcast. |
| Situational splits | PARTIAL | home/away via `side` in offense form gameLog & schedules; handedness (`batterHand`,`pitcherHand`) per pitch in Statcast; `pitcherSavant.vsLHH/vsRHH` (null in sample); TTO splits; park `parkFactor`, `dome` | | | | No game-state/leverage splits precomputed. |
| Player props | VERIFIED (prices/settlement) / RESEARCH (model) | observations/settlements for `hitter_*`, `pitcher_*` families (hitter_* ≈ 518k obs); `edgelab/hitter_projection_snapshots/<date>.jsonl` (15 dates 2026-08-19 → 09-23, 9,617 rows, 222 players, modelProbability + EV); `hitter_validation/` (calibration_by_market: hitter_hits n=454 CALIBRATED, error -6.1 pts) | | hitter scheduler `*/15` (when season live) | 6 files | Production says `NO_MODEL_SUPPORT` for props (184,641 eval rows). |
| Team props (team totals) | VERIFIED | ledger `TT_Away_Over/TT_Home_Over`; `KXMLBTEAMTOTAL` ladder | | | | |
| Game-level markets | VERIFIED | ML, RL/spread, total, F5 ML/spread/total, F3/F7 (research-only series), NRFI/YRFI, winning margin | | | | |
| Play-by-play | PARTIAL | Statcast pitch log only (643 games) — `events`, `description`, base-state, outs, inning | | daily | yes | No PBP for games before 2026-08-11. |
| Weather | UNAVAILABLE (effectively) | `data/weather.json` = 15 dome/notes only; `wagers.jsonl.weather` field exists; `api/weather.js` endpoint exists but slate stores 0 weather keys | | | | |
| Venue / park effects | VERIFIED | slate `park.{name,parkFactor,dome}`, `venue`; `park_adj = (pf-100)/100*0.5` in `compute_projections` | all games | daily | ledger tests | Single static factor per park (source `api/slate.js`). |
| Calibration data | VERIFIED (descriptive) | `data/research/calibration_bins.json`; `handicap_runtime/.../executionConstants.calibration` (tier factors High .187 / Medium .255 / Paper .18 from `config/rules.json`); `lib/edgelab/calibration.py` + `edgelab/analytics/latest_summary.json`; `scored_replay_runs/*/scored_replay_run.json.summary.calibrationBuckets/brier`; `hitter_validation/calibration_by_*.json` | 72 scored replay runs; bets n≈550 settled | daily (`edgelab-daily-report`, `scored_replay`) | 50 test files mention calibration | Factors are static since 2026-06-07. |
| Historical accuracy / postmortems | VERIFIED | `edgelab/postmortems/<date>/postmortem.{json,md}` (131 files, ~26 dates 2026-08-03 → 09-27; analyticalMisses/Wins, performanceByMarketFamily, linkedBetIds); `edgelab/reports/<date>.{json,md}` (340 files from 2026-07-31); `data/research/reports/daily/` | | daily report 08:00 UTC; postmortems manual import | yes | |
| CLV | VERIFIED | `edgelab/bets/bets.jsonl.clv` (POSITIVE_IS_GOOD_V1; null 20.9%), `edgelab/clv_quotes` (304,321 rows, 8 checkpoints, `isClosingQuote`), `settlements.hypotheticalReturnsByCheckpoint` (232,986 rows) → universe-wide hypothetical CLV/returns | 2026-08-01 → 10-01 | `edgelab-clv-collect` (workflow_run) + `clv-update.yml` 06:00 UTC | 11 + 43 files | Legacy convention differs in root `bets.json` (`LEGACY_ENTRY_MINUS_CLOSING`). |
| Historical wager outcomes | VERIFIED | `edgelab/bets/bets.jsonl` 598 rows (REAL 155, REAL_PROBE 2, PAPER 5, untyped 436), 550 settled (265 W / 284 L / 1 push / 1 void), gameDate 2026-06-12 → 10-01, `netProfitLoss`, `stake`; `data/research/wagers.jsonl` 6,046 rows (568 count toward bankroll; includes paper/hypothetical) | | record-placed-bet, import-manual-bets, postgame | app contract test, ledger tests | `modelFairProbability` null 81.6%, `entryTimestamp` null 78.4%. |
| Identity tables | VERIFIED | §3 | | | ids tests | |
| Schedules | VERIFIED | `edgelab/games` (1,467 rows / 61 dates; `scheduledStartTime` null 52.5%); slates `startTime`; research schedules 2022–26; `edgelab/schedule_evidence` | | daily | yes | |
| Seasons covered | — | Production: 2026 regular season from 2026-06-08 (registry) / 06-16 (slates) / 07-30 (EdgeLab); Research: 2022–2026 team/pitcher box data; Pinnacle historical ~37 dates/season 2022–26 (`research_cache/pinnacle_historical`) | | | | Post-season 2026 not yet captured (slate empty 10-02). |
| Replay / forward-test | VERIFIED (research-only) | `edgelab/replay_runs` (444 files), `scored_replay_runs` (72), frozen snapshots per date (3 stages) | 2026-07-30 → 10-02 | daily | 28 + 3 files | Enables re-pricing any past slate under a candidate model. |

---

## 5. Detailed findings per category (evidence)

### 5.1 Slate / model inputs (`data/slates/<date>/authoritative.json`)
- 63 authoritative dates, 820 games, 146 MB total; a game object has 33–36 keys; every game carries
  `marketLedger` (11 rows: `NRFI, YRFI, F5_ML_Away/Home, TT_Away/Home_Over, ML_Away/Home, Game_Total,
  RL_Away/Home`), each row 123 keys incl. `modelProb` (0–100), `kalshiVF`, `pinnacleVF`,
  `executablePriceUsed`, `edge`, `calibratedEdgeVsExecutable`, `calibrationFactor`, `confidenceTier`,
  `betUpToPriceNet`, `awayProjRuns/homeProjRuns`, `awayBullpenAvailability` (multiplier + components),
  `away/homePlatoonContext`, `closingPrice`, `clvVsSnapshot`, `reasonCodes`, `gatesFired`.
- Per-side blocks: `away.pitcher {id,name,pitchHand,note}`, `away.pitcherSavant` (29 keys),
  `away.bullpen` (18 keys incl. `recentUsage`), `awayTeamStats` (record, RpG windows, wRC+ proxy,
  `offenseBaselineRaw/Bayes/Adj`, `oppQuality*`, lineup fields, `offenseForm` windows).
- `odds` has FanDuel/BetMGM/DraftKings/Pinnacle ML/RL/total (+team totals) and Kalshi;
  `kalshi.markets[]` has live yes/no bid/ask, volume.
- Missing in slate: batting orders, weather, umpire, injuries.
- Earlier dates (2026-06-16) have 32–33 game keys vs 35–36 now → schema additive over time.

### 5.2 EdgeLab corpus (`data/edgelab/*`, schema `schema_v1/`)
Measured with `partition_stats.py` / `obs_depth.txt`:

| Partition | Files | Rows | Disk | Range | Key facts |
|---|---|---|---|---|---|
| games | 63 | 1,467 | 1.3 MB | 2026-08-01 → 10-02 | 30 teams both sides; `mlbGamePk` null 3.6%; `supersededBy` 41.9% |
| markets | 63 | 252,625 | 5.8 MB | same | 1 row per ticker (first seen); `player` 719 distinct; `threshold` null 7.2% |
| observations | 63 | 757,724 | 55.2 MB gz | 2026-08-01T17:08Z → 10-02T03:30Z | bid/ask/last/volume/OI null 0%; checkpoint null 4.4%; `scheduledStart` null 34.8%; 450 runIds |
| model_evaluations | 62 | 234,984 | 18.6 MB | 2026-07-30 → 10-01 | `modelFairProbability` non-null **12%** (28,196 rows); `evaluationStatus`: NO_MODEL_SUPPORT 184,641 / EVALUATED 26,683 / NOT_EVALUATED 14,302 / DATA_QUALITY_BLOCK 4,029 / MISSING_MARKET_PRICE 3,449 / PARTIAL 1,495; `artifactSource`: prospective_snapshot 7,854, recommendations 7,150; checkpoints LINEUP_CONFIRMATION 5,456, MODEL_CLOSING_WINDOW 759, T-30 704, T-60 517, T-90 418; evals/ticker median 1, max 12 |
| recommendations | 57 | 226,996 | 16.3 MB | 2026-07-30 → 10-01 | status: INSUFFICIENT_MODEL_SUPPORT 195,248 / NOT_EVALUATED 24,171 / PASS_NO_EDGE 4,344 / PASS_DATA_QUALITY 2,318 / RECOMMENDED 604 / BET_PLACED 289 |
| settlements | 60 | 247,025 | 24.3 MB | settledAt 2026-08-10 → 10-02 | result YES 69,200 / NO 168,549 / unresolved 9,276; `hypotheticalReturnsByCheckpoint` on 232,986; `wasPlaced` 385 |
| clv_quotes | 64 | 304,321 | 13.5 MB | 2026-08-01 → 10-01 | 239,375 tickers; 113 distinct betIds linked |
| hitter_projection_snapshots | 15 | 9,617 | 17.4 MB | 2026-08-19 → 09-23 | 70 games, 222 players, 6,315 tickers; `modelProbability`, `monteCarloStderr`, EV |
| research_runs | 66 | 2,738 | 3.6 MB | 2026-07-31 → 10-03 | 9 runTypes |
| postmortems | 131 files (dirs per date) | | 1.4 MB | 2026-08-03 → 09-27 | structured JSON + md |
| snapshots | 67 dirs | 3,832 files | 357 MB | 2026-07-30 → 10-02 | frozen `market_observations.jsonl.gz` etc. per stage |
| replay_runs / scored_replay_runs | 444 / 72 | | 28 / 6.8 MB | | LEVEL_1_APPROXIMATE fidelity |
| experiments / experiment_reports / control_models / candidate_variants | 36 / 8 / 29 / 3 | | | MLB-ALPHA-0001/0002, MLB-RSCH-0001…0036 | preregistered research registry |
| mlb_rsch_0011_shadow_evaluations / team_total_nb_shadow_evaluations / uncertainty_capture_snapshots | 33 / 32 / 33 | 1 row/game/checkpoint | | 2026-08-29 → 10-01 | NB vs Poisson shadow; recent rows `FAILED_ISOLATED` (uncertainty capture: `unsupported operand type(s)` bug; TT NB: `NO_CANONICAL_TEAM_TOTAL_TICKER`) |
| analytics / reports / health / operational_health | 65 / 340 / 41 / 8 | | 13 / 12 / 0.2 / 1.1 MB | | `analytics/latest_summary.json` ROI/CLV by family with sample-status tiers |
| bets | 1 | 598 | 1.7 MB | 2026-06-12 → 10-01 | 94-key rows |
| bankroll | 1 | 1 | | | single STARTING_BALANCE $350 (2026-08-03) |

Row samples (keys) are in §5 of this doc's source output; e.g. observation row = Kalshi book
(`yesBid/yesAsk/noBid/noAsk/lastPrice/volume/openInterest/spreadCents/marketStatus`) + parsed
identity (`marketFamily/marketHorizon/player/team/threshold/comparisonOperator`) + `checkpoint`,
`provenance.sourceFile` → exact registry snapshot.

### 5.3 Statcast (`data/statcast_raw/`)
- `games/<gamePk>.jsonl`: 643 games, 190,107 pitch rows (avg 295/game), 159 MB. 41 fields per
  pitch: `pitchId (gamePk:abNN:pN)`, `atBatIndex`, `inning`, `balls/strikes`, `outsWhenUp`,
  `onFirst/Second/Third`, `batterId/batterHand`, `pitcherId/pitcherHand`, `pitchType/pitchName`,
  `releaseSpeed`, `spinRate`, `horizontalBreak/inducedVertBreak`, `releaseHeight/Side`, `extension`,
  `plateX/plateZ`, `szTop/szBot`, `pitchCallType`, `description`, `events`, `battedBallType`,
  `launchSpeed/launchAngle`, `hitCoordX/Y`, `estimatedBA/estimatedWOBA`, `wobaValue`, `armAngle`
  (100% null in sample). Missingness on 40-game sample: pitch physics 0.4% null; batted-ball fields
  ~67% null (non-BIP pitches, expected); `events` 74.5% null (non-terminal pitches).
- Index: `batter_games.jsonl` 13,246 (518 batters), `pitcher_games.jsonl` 5,629 (629 pitchers),
  dates 2026-08-11 → 2026-09-27 (48 game-days, 9–17 games/day); index ⟷ files fully consistent.
- Producer: `scripts/statcast_completed_game_catchup.py` via `statcast-postgame-archive.yml` (cron
  `0 10 * * *`), idempotent, final-status verified; store `lib/research/statcast_pitch_store.py`.
- Tests: `tests/test_statcast_pitch_store.py`, `test_fetch_statcast_pitch_log.py`,
  `test_hitter_feature_context*.py`, `test_hitter_phase5_orchestration.py`.
- Consumers: `lib/research/hitter_feature_context.py`, `hitter_pitch_derivation.py`,
  `pitch_taxonomy.py`, `hitter_pa_outcome_model.py` (research-only hitter engine).

### 5.4 Multi-season research cache (`data/research_cache/`, 46 MB, on `main`)
- `batting_backtest/<2022..2026>/boxscores.jsonl.gz`: 2,430/2,430/2,429/2,430/2,004 games
  (`gamePk`, `awayBatting`, `homeBatting` with PA/AB/H/2B/3B/HR/BB/HBP/K/SF/R). No date in row —
  join to schedules by gamePk.
- `starter_workload/<yr>/boxscores.jsonl.gz`: same games; every pitcher appearance (`playerId`,
  `name`, `orderIndex`, `numberOfPitches`, `outs`, `battersFaced`, H/R/ER/BB/K, holds, saves;
  `throwsHand` null).
- `bullpen_backtest/<yr>/schedules/<TEAM>.json` (30 × 5 = 150 files, ~250 KB each): MLB Stats
  API schedule incl. `gamePk`, `officialDate`, `gameType`, `doubleHeader`, `teams.away/home.score`,
  `isWinner`, `leagueRecord`, `venue`, `seriesGameNumber`; + `boxscores.jsonl.gz` per year.
- `pinnacle_historical/<yr>/<date>.json`: 178 dates (37/season 2022–25, 30 in 2026), Odds-API
  Pinnacle h2h/totals snapshots.
- `sharp_market_probe/*.json`: feasibility probes.
- Produced by manual workflows `research-multiseason-{batting,bullpen,starter-workload}-backtest.yml`
  (write to research branch; cache was merged to main). Tests: 5 files reference `research_cache`.

### 5.5 Pipeline artifacts (`data/pipeline/<date>/`)
- `projections.json.data.games[]`: `{gameId, away, home, awayProjRuns, homeProjRuns, f5AwayProj,
  f5HomeProj, totalProj, missingFields, excludedFromSlate}` — 17 games on 2026-09-25.
- `projection_board.json.data.rows[]` (1,618 rows on 09-25; summary by family: team_total 420,
  pitcher_strikeouts 354, game_total 304, inning_total 182, winning_margin 151, inning_result 117,
  pitcher_outs 48, game_result 30, first_inning_run 12): per-ticker `modelProbability`,
  `executableMarketPriceCents`, `executableEdgePct`, `automatedRecommendation`.
- `execution.json`: 187 candidates, `decision PAPER_ONLY`.
- `normalized_slate.json` / `recommendations.json` = immutable copies of the slate with `meta`.

### 5.6 Handicapping card / runtime
- `handicapping_card/<date>.json` (2026-09-17 → 10-01; 0.4–10 MB): per game `lineups`, `markets`,
  `marketsByFamily`, eligibility axes, `contractAccounting`; bankroll redacted.
- `handicap_runtime/<date>/games/<gamePk>.json.gz` (~40 KB gz/game): `context`, `markets` (by
  family), `eligibility`, `executionConstants` in manifest (tier sizes, calibration factors, edge
  thresholds, multipliers).

### 5.7 Bets / CLV / calibration
- See matrix. `data/identity_audit.json` (568 legacy bets: clvStatus/identityStatus),
  `data/rule71_report.json` (flagged vs non-flagged ROI/CLV), `data/backfill_report.json`.
- `data/research/wagers.jsonl` (6,046 rows, 2026-05-26 → 10-01, 568 bankroll-counting; fields incl.
  `park, umpire, weather, bullpenState, lineupConfirmationStatus, modelProbPct, clvMidPct/clvAskPct`)
  rebuilt daily by `build-wager-research.yml`; `data/research/reports/{summary.json,daily/}`.

### 5.8 Kalshi raw layers
- `kalshi_registry_snapshots/kalshi_search_<date>[_HHMM].json`: 268 files; one dated file per day
  2026-06-08 → 10-02 kept forever, intraday files for last ~21 days (3–21/day). Each = full
  `kalshi_search.json` payload (markets with book fields, `series_counts`, unknown-series discovery).
- `kalshi/discovery/<date>.json` (`contracts` list; 2026-09-25 file had 0 — off-pattern), plus
  `_coverage`, `_series_catalogue`, `_f3_f7_search`, `_summary` companions.
- `kalshi_research_night_before_snapshots/night_before_<date>_<ts>.json`: 7 files (2026-09-27 → 10-03).
- `kalshi_odds_history.json`: 4,755 legacy rows (`snapshot_ts, market_ticker, yes_bid/ask, mid,
  implied_pct, last_price, volume`).

---

## 6. Opponent-adjustment audit

- **Formula** (`scripts/fetch_opp_quality.py:208-258`): for each team, take the last 15 completed games
  within a 21-day window (`WINDOW_DAYS=21`, `fetch_recent_games` → `games[-15:]`), resolve the
  *opposing starting pitcher* for each (`fetch_actual_starter`), look up his Savant `xera`
  (`fetch_savant_pitcher_xfips`, field named xFIP but sourced from xERA; falls back to season FIP via
  Stats API), average → `oppXFIPavg`; `raw_adj = (oppXFIPavg − 4.00) × 0.08`;
  `oppQualityAdj = clamp(raw_adj, −0.2, +0.2)`. `MIN_GAMES_FOR_SIGNAL=5` → adj null below 5 resolved
  games; `confidence ∈ {full, partial, low}`.
- **Baseline**: `LEAGUE_AVG_XFIP = 4.00` (constant, not computed from data).
- **Applied** (`scripts/enrich_data.py:111-132`): `raw = 0.30·L7 + 0.30·L15 + 0.40·Season RpG`;
  `bayes = (15·raw + 20·4.5)/35` (shrink toward `LEAGUE_AVG_RPG=4.5`); `offenseBaselineAdj = bayes +
  oppQualityAdj` (+ `lineupAdj` capped ±0.25 when lineup confirmed, `enrich_data.py:229-236`).
- **Recursion**: none — single pass, opponent quality is pitcher-level xERA, not team strength.
- **History**: only inside each date's authoritative slate (`awayTeamStats.oppQualityAdj,
  oppXFIPavg, oppQualityGames, oppQualityConf, oppQualityNote`) — 63 dates. `data/oppquality.json`
  overwritten daily (sample 2026-10-02: `{oppXFIPavg: 3.97, oppQualityAdj: -0.002, gamesResolved: 15,
  confidence: 'full'}`).
- **Tests**: `tests/*oppquality*` (1 file), `*offense_baseline*` (3), `*enrich_data*` (14).
- **Limitations**: not applied to pitching/defense; magnitude ≤ 0.2 R/G; xERA/xFIP naming
  inconsistency; no park or handedness interaction. Research experiment `MLB-RSCH-0015_OPPONENT_STRENGTH`
  (`docs/EDGELAB_MLB_RSCH_0015_OPPONENT_STRENGTH.md`, `edgelab/experiments/MLB-RSCH-0015.json`)
  evaluated richer opponent strength on the 2022–26 cache — research only.

---

## 7. Time-series / Splits / Market-history / Projection inventories

### 7.1 Time-series-capable datasets
| Dataset | x-axis | Keys | Rows | Linkable to gameId? | Opponent id? |
|---|---|---|---|---|---|
| `research_cache/bullpen_backtest/<yr>/schedules/<TEAM>.json` | game date / gamePk | teamId, gamePk | ~2,430 games × 2 sides × 5 seasons | yes (gamePk) | yes (`teams.away/home.team.id`) |
| `research_cache/batting_backtest` | gamePk | gamePk | 11,723 | yes | via schedules |
| `research_cache/starter_workload` | gamePk | gamePk, playerId | 11,723 games (~100k appearances) | yes | via schedules |
| `statcast_raw/index/*_games.jsonl` + games | gameDate / gamePk | batterId/pitcherId, gamePk | 13,246 + 5,629 player-games | yes | via slate/games partition |
| `team_offense_form.json.teams[].gameLog` | date | team abbr, gamePk | 30 × ≤30 | yes | yes (`opponent`) |
| `slates/<date>/authoritative.json` | slate date | gamePk | 820 games | yes | yes |
| `pipeline/<date>/projections.json` | slate date (3+ runs/day, last wins) | gamePk | 61 dates | yes | yes |
| `edgelab/model_evaluations` | capturedAt / checkpoint | marketTicker, gameId, runId | 28k priced rows | yes (int/str mix) | via games |
| `edgelab/observations` / `clv_quotes` | capturedAt / checkpoint | marketTicker | 757k / 304k | yes (34.5% fallback ids) | via games |
| `edgelab/bets` | gameDate | betId, marketTicker | 598 | 74% | via ticker |
| `edgelab/mlb_rsch_0011_shadow_evaluations` | date × checkpoint | gameId | 33 dates | yes | — |
| `edgelab/reports/<date>.json`, `analytics/` | date | — | 340 files | — | — |
| `kalshi_odds_history.json` | snapshot_ts | market_ticker | 4,755 | via ticker | — |

### 7.2 Split dimensions actually stored
- Home/away: schedules (`teams.away/home`), offense form `side`, slate `away/home` blocks, batting
  box `awayBatting/homeBatting` — full coverage.
- Handedness: Statcast per pitch (`batterHand`, `pitcherHand`) — 190k rows; `pitcherSavant.vsLHH/vsRHH`
  (null in sample), `platoonContext.handedness.countL/R/S` (often unknown); `fetch_batter_platoon_splits.py`
  output in `teamstats.batterWOBA`/platoon fields.
- Times-through-order: `pitcherSavant.tto1/tto3/ttoSplit` per starter per slate.
- Horizon: F3/F5/F7/FULL_GAME on markets (`marketHorizon`), projections for F5 and full.
- Game state: Statcast base/outs/count per pitch only.
- Park/dome: per game. Day/night: schedules `dayNight`. Doubleheader: `doubleheaderGameNumber`.
- High-leverage bullpen: `hlXFIP/hlGrade/hlSamplePA`.
- No strength-state/surface (N/A for MLB), no monthly/season splits precomputed.

### 7.3 Market history
- Per-ticker quote series exist (`edgelab/observations`, `clv_quotes`) but are shallow: median 3,
  p90 5, max 17 observations per ticker across its ~1-day life; distinct capture minutes per day
  median 6 (cron is 2/hour but many wakes produce no new rows/are off-window). Retention: forever
  on main (gz). Size 55 MB gz for 757k rows (~73 B/row gz).
- Raw registry snapshots: 268 files / 319 MB; intraday density retained only 21 days on main, but
  research branches retain their windows (e.g. 2026-08-13: 11 snapshots that day on the alpha-0002 branch).
- Dense microstructure only on research branches: `mlb_alpha_0002/prospective/{books,quotes,trades}`
  (every 10 min, 2026-09-02 → 10-01, 8+3+8 MB gz; `orderbook.yes_dollars/no_dollars` ladders) and
  `mrv_prospective/v1/kalshi_trades` (476,756 trade rows for 2026-09-23 alone). Candlesticks (1-min)
  exist only as a GitHub Release, not in git.
- Closing lines: `clv_quotes.isClosingQuote`, `snapshots/<date>/closing_line/frozen/`, legacy
  `data/clv/<date>/closing_capture_log.json`.

### 7.4 Projection / simulation outputs
- Per game: point estimates (away/home runs, F5 runs, total) — `pipeline/<date>/projections.json`,
  recomputed at each fetch/recheck (3–5 runs/date; only last-run file kept per date; the authoritative
  slate also frozen). Per market: `modelProb` for 11 ledger markets + `projection_board` rows for
  ~1,600 tickers/day; EdgeLab `model_evaluations` stores per-ticker `modelFairProbability` at
  checkpoints (prospective snapshots, 5,456 LINEUP_CONFIRMATION rows, 759 closing-window) → a true
  projection-over-time history for ~28k ticker-checkpoints.
- Distribution: Poisson implied (`p_team_wins`, `p_over_total` in `build_market_ledger.py:169-185`);
  NB shadow cells per game (rsch_0011); hitter Monte Carlo (stderr stored). No samples/quantiles persisted.
- Hitter: 9,617 prospective snapshot rows with `modelProbability`, `fairAmericanOdds`,
  `expectedValuePerDollar`, `sampleSizeDiagnostics`.

---

## 8. Existing ranking / percentile / league-average code
- League constants: `scripts/enrich_data.py:15-16` (`LEAGUE_AVG_RPG=4.5`, `LEAGUE_AVG_XFIP=4.00`),
  `scripts/fetch_opp_quality.py:26`, `lib/research/platoon_context.py:107` (`LEAGUE_AVG_WOBA=0.318`),
  `scripts/fetch_lineups.py:98`, `lib/research/pitcher_workload_projection.py:134-136` (BB% 8.5,
  K% 22.0, opponent wRC+ 100), `lib/research/pitch_taxonomy.py:178-179`.
- Index-style metrics: `rpgIndex = RpG/4.5·100` (`enrich_data.py:87`), `wrcPlus` = same proxy;
  bullpen `grade ∈ {ELITE, …}` / `hlGrade` (`scripts/fetch_savant_bullpen_hl.py`); `formLabel`
  HOT/… (`fetch_team_offense_form.py`); confidence tiers.
- Percentile/rank code: research only — `lib/edgelab/research/market_structure/stats.py`,
  `lib/edgelab/backtest/team_offense_recency_stats.py`, `lib/edgelab/evidence_levels.py`,
  `scripts/research/mlb_alpha_000{1,2}/*`, `lib/edgelab/analytics.py` (DuckDB aggregations by family
  with sample-size tiers n<20 / 20–100 / ≥100 in `lib/edgelab/calibration.py`).
- No league-wide percentile tables for players/teams are persisted.

---

## 9. Size estimates (if exposed by the research layer)
| Layer | Basis | Approx. bytes |
|---|---|---|
| (a) Team profiles (30) | teamstats (~230 B) + bullpen (~900 B) + oppquality (100 B) + savant team (100 B) + offense form windows (~3 KB) + 5-season W/L & RS/RA summary (~1 KB) | ~6 KB/team → **~0.2 MB** total; add per-season game logs (~160 games × ~120 B × 5 seasons) ≈ 100 KB/team → **~3 MB** |
| (b) Player profiles | savant batters/pitchers current (143 KB total); starters' Savant block from slates (~700 B × ~500 starters); Statcast-derived per-player aggregates (518 batters + 629 pitchers × ~1 KB) | **~1.5 MB** for all players; ~1–3 KB each |
| (c) Per-game detail | authoritative slate game ≈ 146 MB / 820 ≈ 180 KB raw (ledger-heavy; ~25 KB if ledger trimmed to model/edge fields); runtime bundle ≈ 40 KB gz; Statcast pitch log ≈ 250 KB raw / ~50 KB gz per game | **20–50 KB/game** trimmed; 300 KB with pitches |
| (d) Metric time series | team game logs 5 seasons ≈ 11,723 × 2 × 150 B ≈ 3.5 MB raw (~0.6 MB gz); pitcher appearance log ≈ 100k × 200 B ≈ 20 MB raw (~2.5 MB gz); Statcast player-game index 1.9 MB; model_evaluations priced rows 28k × ~1 KB ≈ 28 MB raw (~3 MB gz) | **~6–8 MB gz** total; per team ≈ 100 KB |
| (e) Market history | observations 55 MB gz for 63 days (≈ 0.9 MB gz/day, ≈ 525 rows/game); per ticker ≈ 3 rows × 700 B | per game ≈ 60 KB gz; full season ≈ 150 MB gz |

---

## 10. Recommended research capabilities to expose in this pass

**Expose (evidence supports):**
1. Team profile + 5-season results/game log (schedules + batting box + pitcher lines) — VERIFIED
   research cache; join on gamePk; opponent id present.
2. Current-slate model inputs per game (starter Savant block, bullpen incl. recent usage, team form
   windows, park, lineup status, opp-quality adj, projections, 11-market model vs Kalshi vs Pinnacle)
   from `authoritative.json` — 63 dates of history.
3. Projection-over-time per ticker (checkpoint series from `model_evaluations` prospective snapshots)
   and recommendation/settlement outcome joins (hypothetical returns by checkpoint).
4. Market price series per ticker with checkpoint tags (shallow, label as "snapshot series, median 3
   points") and closing quotes; universe-wide settlement outcomes by family.
5. Wager outcomes + CLV + calibration bins + daily postmortems (REAL only; show sample-size tier).
6. Statcast pitch-level explorer for 2026-08-11 → 09-27 (per batter/pitcher per game: pitch mix,
   velocity, EV/LA, xwOBA) — label window explicitly.
7. Team recent-form distributions (L5/L7/L10 with market-relative overs) — analysis-only, labelled.

**Mark RESEARCH:** hitter/pitcher prop probabilities (hitter engine snapshots, calibration says
over-confident by ~6 pts), NB distribution shadows, MRV/alpha-0002 microstructure (branch-only),
Pinnacle historical (sparse 37 days/season), opponent-strength experiment variants, replay scoring.

**Mark UNAVAILABLE:** injuries, weather values, umpires, play-by-play before 2026-08-11, player
season-stat history (only daily overwrite), batting orders in slate (names only in lineup_audit /
card), team schedule strength beyond opposing-starter xERA, postseason 2026.

---

## 11. Open questions / UNKNOWN
- Git history is shallow (47 commits) so actual commit cadence/mutation history of overwritten files
  (`data/*.json`) could not be verified; relied on crons + partition dates.
- `edgelab/model_evaluations.capturedAt` absent (uses `createdAt`/`pipelineRunId`); exact snapshot
  timing semantics per row are in `lib/edgelab/prospective_snapshot.py` (not fully traced).
- Why observation capture yields median 6 distinct minutes/day against a 2/hour cron — likely
  dedupe of unchanged books (`*_unchanged` pattern on research branch) or off-window wakes; not proven.
- `kalshi/discovery/2026-09-25.json` had 0 contracts while 520 MB of discovery exists — per-date
  layout varies (`_coverage`, `_series_catalogue` files); not fully characterised.
- `pitcherSavant.vsLHH/vsRHH/velocity*` null in sampled game — coverage rate across all slates not measured.
- Research-cache `boxscores` rows have no date field; joins verified structurally, not executed.
- MLB-ALPHA-0002 raw candlestick dataset lives only in a GitHub Release (not checked).
- `uncertainty_capture_snapshots` and `team_total_nb_shadow_evaluations` latest rows are
  `FAILED_ISOLATED` (bug / missing ticker) — experiment liveness unknown.
