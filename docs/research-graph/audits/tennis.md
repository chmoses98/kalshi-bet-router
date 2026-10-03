# Phase 2 research-data audit — TENNIS (`chmoses98/Tennis-Edge-Finder`)

Audited read-only on 2026-10-03 against `main` @ `0a17a7893` (66 commits, 2026-10-02), `origin/tennis-data` @ `23add39d9`
(fetched `--depth=1`; tip commit "kalshi tennis capture 2026-10-03T06:14:11Z (conductor pass 13)", 2026-10-03 06:16Z) and
`origin/accounting-data` @ `2b980d547`. Scratch copies under
`(local scratch, not committed)` (`td/` = partial `git archive`
of the data branch; `tennis_data_sizes.txt` = `git ls-tree -r` + `cat-file --batch-check` object sizes for all 92,252 files;
`scripts/measure_*.py` = the measurement scripts; `measure_sackmann.out` = their output). Older per-run copies of
`research/{clv,timing,horizons}` were deleted locally after measurement because the shared scratch disk filled; their counts
come from the ls-tree listing. All numbers below were computed with pandas/pyarrow/stdlib against the real files.

---

## 1. Summary

1. Tennis is a **player-rating + exchange-microstructure** repository, not a box-score one. Its genuine strengths are
   (a) a 1.63M-row canonical singles match table 1990-2026 (both tours, Slams to ITF, with Sackmann serve statistics on
   95-99% of tour/Challenger rows since 2016) rebuilt every 6 h from immutable source snapshots; (b) a production
   **per-player rating state** (overall + per-surface Elo, structural serve/return abilities, serve-point evidence) for
   25,313 ATP and 25,186 WTA ids; (c) the fleet's deepest **Kalshi market archive**: 23 days of 10-15 min quote/book/trade
   capture (2.03 GB, 1,445 open tickers on a typical day, mean 31 quote rows per ticker per day, every tennis trade on the
   exchange), 17.8 GB of daily discovery snapshots with 122,651 settled markets back to 2025-06, and lifetime candles for
   15,894 settled markets; (d) an append-only, hash-chained **prediction ledger** of 16,490 priced contracts over 132 runs
   (2026-09-11..10-03), settled against exchange truth with first-ball-anchored CLV on 2,380 strict rows.
2. The model layer is honest and tested (500 pytest functions in 39 files) but **has no evidence of edge**: Kalshi mid
   Brier 0.1776 vs model 0.2193 on 15,117 settled rows; strict executable CLV mean -0.049; every frozen edge candidate is
   INSUFFICIENT_N or FAIL_ACCURACY; the discrepancy audit attributes 97% of large model-market gaps to stale/settled
   quotes or thin data. Real-money authority is OFF everywhere; the app export already labels everything RESEARCH_ONLY.
3. **Opponent adjustment is real and explicit** in the structural serve/return model (`tennis_edge/models/serve_return.py`:
   `logit p = base + s_A - r_B`, sequential, precision-weighted, 365-day decay) and in Gen-2 (`models/gen2.py`, adds
   level offsets and surface deviations); Elo is opponent-adjusted by construction. All are chronological, walk-forward
   tested for leakage, and carried as *end-states* only (the as-of checkpoint artifact covers 2026-06-01 onward).
4. Weakest areas: **fundamental data freshness** (Sackmann forks froze at 2026-06-01 ATP / 2026-04-27 WTA; forward
   coverage is a TML mirror to 2026-09-29 for ATP and ESPN results-only for both tours since 2026-03-28, so WTA serve stats
   stop in April); **no sports truth independent of the exchange** (TENNIS-8 at 0%); **first-ball truth exists only for
   ATP/WTA main tour + Slams** (Challenger/ITF are 0 strict rows of 10,508 ledger rows); **no injuries, no draws, no
   weather/venue beyond surface, no rankings consumed, no form model**; the canonical `matches.parquet` is not on any
   branch (rebuilt per run; only its manifest, quarantine and crosswalk are published); the `clv/timing/horizons` tables are
   fully re-derived every run (86 copies, 4.5 GB of the branch) rather than appended.
5. The app export (`tennis_edge/app_export.py`) reads only the assisted slate, three health files and the (empty)
   accounting ledger. It ignores the ledger history, settlements/CLV/timing/horizons, the opportunity and producer stores,
   the external-venue scan, the first-ball store, the rating states, the canonical table and the entire market archive, and
   it keys players by **Kalshi display name**, not Sackmann id.

---

## 2. Data branches and layout

### 2.1 `main` (code + frozen research), 40,047 lines of Python

| path | what | size |
|---|---|---|
| `tennis_edge/` (34 modules) | data loaders, identity, models, DP engine, Kalshi parsing, ledger, first-ball, assisted lane, health gates | code |
| `scripts/` | `run_tennis.py` (projection), `kalshi/{capture,discover}_tennis.py`, `ops/{settle_ledger,shadow_board,model4_board}.py`, `data/*`, `firstball/*`, `external/*`, `research/*` (23 study scripts), `ci/publish_branch.py` | code |
| `contract/edge_finder_contract/` | vendored app contract v1 incl. `registry.json` (TENNIS -> `tennis-data` / `tennis-edge-finder/data/app/latest`) | code |
| `config/` | `formats.json` (scoring formats by year/level), `kalshi_tennis_series.json` (146 series), `kalshi_competitor_map.json` (277 UUID->id), `kalshi_settlement_rules.json`, `discrepancy_sanity.json` | 5 files |
| `data/identity/reviewed_aliases.json` | human-reviewed name aliases (schema v1) | 1 file |
| `data/research/` | **stale snapshot** of ledger/projections/opportunities/candidate_evidence from mid-September (29 files, ~4.7 MB) committed at migration; production writes go to the data branch | 4.7 MB |
| `research/` | 74 study artefacts: `elo_study/predictions_{ATP,WTA}.parquet` (23.8 + 21.6 MB walk-forward per-match predictions 1990-2026), `market_benchmark/` (Pinnacle-linked 13,323 ATP matches, 19 MB), `kalshi_backtest/linked.parquet` (1,869 settled markets), `selector/`, `coherence/`, `model_market_discrepancy/`, RESULTS.md per study | ~66 MB |
| `docs/` (26 files), `*_REPORT.md` (7), `MIGRATION_AUDIT.md`, `PRODUCTION_HANDOFF.md` | design/decision docs | text |

### 2.2 `tennis-data` (orphan evidence branch) — 92,252 files, **30.75 GB** of blobs, everything under `tennis-edge-finder/data/`

Publisher: `scripts/ci/publish_branch.py` (copy into a worktree, `git add -A`, rebase-retry push; `--no-overwrite` for
write-once trees; **files > 95 MB are silently skipped**, line 79-82). New files per run => append-only by convention;
`latest.json`-style files and `state.json` are overwritten in place.

| path | files | bytes | immutable? | producer / cadence |
|---|---|---|---|---|
| `kalshi/discovery/<run>/` (23 runs) | 51,740 | 17,842 MB | yes (new run dir per day) | `scripts/kalshi/discover_tennis.py --skip-phase2`, first conductor pass of each UTC day (`tennis-capture.yml:93-98`); initial run `20260911T063759Z` = 5,717 MB / 32,650 files incl. `candles/<S>/<T>.json` + `trades/<S>/<T>.json` for 15,894 settled markets; the 22 daily runs are 524-534 MB / 862 files each (`events/`, `markets/` (open/unopened/closed/settled), `historical_markets/`, `rules/*.pdf` 44.9 MB, `series_detail/`, `series_all.json` 19 MB, `summary.json`) |
| `kalshi/capture/<YYYY-MM-DD>/<run>.{quotes,books,trades,events,settlements,candles}.jsonl.gz` + `.manifest.json`; `capture/state.json` | 10,734 | 2,031 MB | yes except `state.json` | `scripts/kalshi/capture_tennis.py`, conductor loop every ~10 min, cron `3 */5 * * *` + self-dispatch (`tennis-capture.yml`). By kind: trades 1,738 MB (2,125 files), quotes 146 MB (2,131), candles 70 MB (458), books 45 MB (2,131), manifests 29 MB, settlements 1.2 MB (457), events 1.0 MB (1,300). 23 days 2026-09-11..10-03, 60-129 MB/day |
| `sources/<run>/` (Sackmann, TML, MCP, mirror) + `sources/espn/<run>/` (22 runs, ~8.7 MB each incl. raw scoreboard JSON) + `sources/external_odds_probe/`, `serve_probe/`, `smarkets_probe/` | 15,377 | 4,220 MB | yes | `tennis-bootstrap.yml` cron `40 5 * * *` (`scripts/data/bootstrap_sources.py`, `fetch_espn_results.py`, probes). Latest Sackmann snapshot `20261002T112052Z` (108 MB: tennis_atp 176 files, tennis_wta 126, MCP 14, TML 62), mirror `20261002T114432Z` (14 MB), ESPN `espn/20261002T113229Z` (9 MB) |
| `firstball/store/` (`poll_*.json` x4,824, `observations/<day>.jsonl` x23 = 267 MB, `truths/<day>.jsonl` x23 = 3.6 MB, `schedule/{plan_latest.json,NEXT_WINDOW.md,plan_log.jsonl,dispatch_log.jsonl}`) | 4,874 | 272 MB | append-only (hash-chained rows); `schedule/` overwritten | `scripts/firstball/poll_first_ball.py` + `plan_windows.py`, conductor cron `17 */5 * * *`, 10-min segments (`tennis-firstball.yml`) |
| `research/ledger/<day>.jsonl` | 23 | 42 MB | append-only, hash-chained (TENNIS-12) | `scripts/run_tennis.py` via `tennis-run.yml` cron `25 */6 * * *` |
| `research/projections/<run>.json` + `REPORT_<run>.md` + `latest.json` | 185 | 56 MB | per-run files; `latest.json` overwritten | same |
| `research/settlements/<run>.jsonl` (+`SCORECARD.md`) | 90 | 43 MB | per-run (new rows only) | `scripts/ops/settle_ledger.py` in `tennis-run.yml` |
| `research/clv/<run>.jsonl`, `timing/<run>.jsonl`, `horizons/<run>.jsonl` | 86 each | 1,074 / 538 / 2,899 MB | **each run re-derives all 16,490 ledger rows** (horizons = 131,920 rows/run); latest run 23.8 / 11.9 / 64.1 MB | same |
| `research/clv_physical_join/<run>.jsonl` | 22 | 24 MB | per-run (686 rows latest) | `tennis_edge/confirmation` |
| `research/opportunities/<day>.jsonl` | 7 | 27 MB | append-only | `scripts/ops/shadow_board.py` (frozen producer) |
| `research/frozen_producers/{shadow_board,model4}/<day>.jsonl`, `heartbeats.jsonl` | 12 | 37 MB | append-only, hash-chained | same + `model4_board.py`, since 2026-09-28 |
| `research/candidate_evidence/*.evidence.jsonl`, `harvest_runs.jsonl` (+ gz exclusion lists) | 31 | 46 MB | append-only, write-once candidate definitions | `scripts/research/harvest_candidate_evidence.py` |
| `research/external/{market,dislocations}/<day>.jsonl`, `scans/scan_<run>.json`, `raw/<run>.{bovada,smarkets}.json.gz` | 5,707 | 1,269 MB | append-only | `scripts/external/capture_and_scan.py` every capture pass |
| `research/assisted_slates/{latest.json,latest.md,slate_runs.jsonl}` | 3 | 3.4 MB | `latest.*` overwritten; `slate_runs` appended (23 rows since 2026-09-30) | `tennis-assisted-slate.yml` (dispatch from the first-ball planner) + `tennis-run.yml` |
| `research/{candidate_confirmation,edge_candidates,experiment_starts,assisted_decisions,assisted_handicapping,board_accounting,shadow_board,segments,model_market_discrepancy,health_latest.json}` | 44 | ~1 MB | mixed (write-once / overwritten reports) | `tennis-run.yml` |
| `processed/{ratings_ATP.json 5.4 MB, ratings_WTA.json 5.2 MB, asof/asof_ATP.json.gz 3.7 MB, asof_WTA.json.gz 2.3 MB, player_crosswalk.parquet, matches_quarantine.parquet, build_manifest.json}` | 7 | 17.7 MB | **overwritten every run** (`tennis-run.yml:139`, `|| true`) | `tennis_edge/data/build.py`, `models/state.py`, `research/build_asof_states.py` |
| `app/latest/` (12 documents + `event_detail/` x160) | 172 | 3.3 MB | overwritten | `scripts/app_export.py` |

Not on the branch: `processed/matches.parquet` (the canonical table). It is rebuilt from sources every run; the most
likely cause of its absence is the publisher's 95 MB skip (1.63M rows x 50 columns) — UNKNOWN, not verified (see §11).

### 2.3 `accounting-data` — `README.md` + `data/accounting/wagers.jsonl` and `settlements.jsonl`, **both 0 bytes**.

---

## 3. Identity model

| entity | canonical key | where | coverage / evidence |
|---|---|---|---|
| Player | **Sackmann numeric id as a string** (`'104925'`); `id_system` column per row (`sackmann` / `tml` / `espn`) | `tennis_edge/data/sackmann.py:200-213` (`_id_to_str`), `data/build.py:8-10,138-148` | 1,527,723 Sackmann rows; 97,561 TML rows (ATP-site ids like `B0BI`), 6,035 ESPN rows (`espn:<id>`, `data/espn_results.py:144-146`) |
| Cross-source player crosswalk | exact normalised full name, one-to-one in both systems, else refused | `tennis_edge/identity/crosswalk.py` (`MAPPED` / `AMBIGUOUS_NAME` / `NO_CANONICAL_MATCH`), published as `processed/player_crosswalk.parquet` (6,154 rows: `foreign_id_system, foreign_id, canonical_id, name_key, display_name, status, reason, n_foreign_rows, n_canonical_rows`) | tml 4,720 MAPPED / 52 AMBIGUOUS / 244 NO_CANONICAL; espn 1,021 / 7 / 110. Of 103,596 foreign rows 94,596 reach a canonical id (`build_manifest.json`) |
| Player registry | `identity/players.py:109-146` from `atp_players.csv.gz` (66,912 rows; dob 28% null, height 94% null, wikidata 93% null) and `wta_players.csv.gz` (70,498; dob 63% null) | alias table (`build_alias_table`) adds last+initial and match-spelling aliases | tests `tests/test_players.py` (15) |
| Kalshi competitor -> player | exact normalised full name against the tour's rating state; namesakes resolved only if exactly one active in 24 months (conf 1.0 / 0.9), human aliases 0.95, else `UNMAPPED`; cached by Kalshi competitor UUID | `identity/kalshi_map.py:1-60`, cache `config/kalshi_competitor_map.json` (277 UUIDs), `data/identity/reviewed_aliases.json` | TENNIS-7 PASS (0 ambiguous used); TENNIS-4 FAIL: 12 of 140 projectable open markets unmapped on 2026-10-03 (e.g. "Darya Khomutsianskaya: UNMAPPED") |
| Match (canonical table) | `match_key = <tour>:<tourney_id>:<match_num>`; cross-source dedupe key `tour|tourney_date|round|norm(winner)|norm(loser)` | `sackmann.py:298`, `build.py:132` | 142,833 cross-source duplicates dropped; 8,995 `DUPLICATE_MATCH_KEY` + 4,402 `IMPOSSIBLE_SCORE` + 490 `MISSING_PLAYER_ID` + 15 `SELF_MATCH` quarantined |
| Match (ledger / Kalshi) | `match_id` = Kalshi **event ticker** (`KXATPMATCH-26OCT02HURGEA`); `ticker` = market | `scripts/run_tennis.py:270-290` | 4,039 distinct events in the ledger |
| Physical match | `physical_match_id = <TOUR>:<id_a>:<id_b>:<YYYY-MM-DD>` (Sackmann ids) | shadow board, model4, opportunities, external dislocations, `clv_physical_join` | 1,286 distinct in opportunities; null when a side is unmapped |
| Slate match | `match_key = "<TOUR>:<match_code>:<discipline>"` (`WTA:26OCT03BOUFAL:singles`), `match_code` = Kalshi date+code | `tennis_edge/assisted/schema.py` | 135 matches on the latest slate |
| App ids | `evt_<sha256[:20]>` of (`TENNIS`,`kalshi_event_ticker`,event); `prt_` of (`PLAYER`,`kalshi_player_name`, lower-cased display name); `mkt_kalshi_<TICKER>` | `contract/edge_finder_contract/ids.py:56-72`, `docs/APP_EXPORT.md` "Identity" | **the app never sees a Sackmann id** ("`sackmann_id` will take over when [the slate carries one]") |
| Doubles team | `team_key = sorted(player ids)` | `sackmann.py:422` | doubles priced with a baseline prior only |
| Tournament | `tourney_id` (Sackmann `2025-9900`) / Kalshi `competition` text -> level, surface via `pricing/competition.py:55-66` (most-common surface over the two latest seasons) | | ledger `surface` null on 13.4% of rows (2,210) |

Known collisions / refusals: 59 `AMBIGUOUS_NAME` foreign ids; Kalshi same-surname pairings use a duplicate digit in the
ticker (`docs/KALSHI_MARKET_TAXONOMY.md` "Record grammar") and the parser fails closed; the first-ball mapper refused 9
`AMBIGUOUS` matches in the latest poll ("feed match 184314 claimed by 2 different physical matches"); `docs/IDENTITY.md`
still says "production ratings use `sackmann` only", which is outdated — `models/state.py` reports
`id_systems_used = {sackmann: 853,037, tml: 88,995, espn: 1,984}` for ATP.

---

## 4. CAPABILITY MATRIX

| capability | status | origin | coverage | cadence | tests | notes |
|---|---|---|---|---|---|---|
| Team metrics | UNAVAILABLE | — | — | — | — | individual sport; Davis/United Cup rubbers are priced as singles; no team model (`families.py:43` "team-tie model not built") |
| Player metrics (ratings) | PARTIAL | `models/state.py` -> `processed/ratings_{ATP,WTA}.json` | 25,313 ATP / 25,186 WTA ids; per player `elo, n, last_date, surfaces{Hard,Clay,Grass,Carpet:[elo,n]}, sr_s, sr_r, sr_points, name`; 2,530 ATP + 1,686 WTA active in 2026 | every 6 h (`tennis-run.yml`) | `test_elo_leakage.py`, `test_gen2.py`, `test_scoring_engine.py` | end-state only, overwritten each run; no W-L, no career aggregates |
| Player metrics (serve/return box stats) | PARTIAL | Sackmann `w_ace..l_bpFaced` in canonical table | tour main 51% overall, 91-99% 2016-2025; qual/chall 99.6% since 2019; ITF men 0% before 2025 (97% in 2025-26); WTA main 92-99% since 2016, WTA qual/ITF ~7% before 2025 | daily source refresh, but **frozen upstream** (ATP 2026-06-01 / WTA 2026-04-27); TML mirror ATP to 2026-09-29 with stats | `test_sackmann.py` (24) | table itself not published; rebuilt per run |
| Game logs (player per match) | PARTIAL | canonical `matches.parquet` (`CANONICAL_COLUMNS`, `sackmann.py:102-113`): date, tourney, surface, level, round, best_of, score, set_scores, minutes, ranks, serve stats both sides, outcome_type | 1,631,319 rows 1990-2026 (sources go back to 1968); 2026: 30,070 rows | 6 h rebuild | `test_sackmann.py`, `test_score_parser.py` (12) | reproducible from published snapshots (`build_manifest.json` lists the run ids + per-file counts); `minutes` 50% null on ATP main, 84% WTA |
| Historical opponents / results | PARTIAL | same table (winner_id/loser_id per row) | as above | as above | as above | H2H derivable, not stored |
| Opponent adjustments | PARTIAL (production formula, tested, end-state only) | `models/serve_return.py`, `models/gen2.py`, `models/elo.py` | all rated players | 6 h | `test_gen2.py` (13), `test_elo_leakage.py` (2) | see §6 |
| Schedule strength | UNAVAILABLE | — | — | — | — | Gen-2 has per-(tour,level) offsets (`gen2.py:72-134`), not an opponent-schedule metric |
| Recent-form windows | UNAVAILABLE as a metric; inputs PARTIAL | ledger `quality.inputs.{n_matches_a, days_since_last_a}` (`run_tennis.py:248-251`), slate `recent_form_inputs` ("not a win-loss form model") | every ledger row | 6 h | `test_quality_doubles.py` | L5/L10 W-L not computed anywhere |
| Usage | PARTIAL (match duration only) | `minutes` column | 50% null main tour | — | — | no serve-count "usage" beyond `w_svpt` per match |
| Lineups / draws | UNAVAILABLE | `tennis_edge/futures/draw.py` bracket DP exists, "draw feed not wired" (`KNOWN_LIMITATIONS.md`) | — | — | `test_draw.py` (3) | 228 tournament-scope markets excluded per run as `tournament_scope_not_priced_tonight` |
| Injuries / availability | UNAVAILABLE | — | — | — | — | retirements/walkovers recorded post hoc as `outcome_type` only |
| Matchup metrics | PARTIAL | ledger `inputs.{pa,pb,sr_pa,sr_pb,elo_a,elo_b}` per contract; model4 `gen2_pa/pb` | 16,490 rows | 6 h | `test_scoring_engine.py` (11) | point-win probabilities per matchup are the model's native matchup object |
| Projection distributions | PARTIAL | `sim/analytic.py` `match_distribution` (exact DP: set scores, total games, game diff, tiebreaks); stored only by `model4_board.py` (`fundamental_distribution`, `conditioned_distribution` per listed derivative) | model4: 5 daily files since 2026-09-28 (391 rows on 10-02) | 6 h | `test_scoring_engine.py`, `test_payoffs.py` | `run_tennis.py` prices derivatives from the distribution but stores only the per-ticker price |
| Raw projections | VERIFIED (as a data artifact; model authority RESEARCH_ONLY) | `scripts/run_tennis.py` -> `research/ledger/<day>.jsonl`, `projections/<run>.json` | 16,490 rows, 8,465 tickers, 4,039 events, 132 run timestamps, 2026-09-11..10-03; families MATCH_WINNER 12,778 / SET_WINNER 1,756 / TOTAL_GAMES 1,151 / GAME_SPREAD 429 / EXACT_SET_SCORE 352 / SET_SPREAD 20 / TOTAL_SETS 4; levels ITF 8,482 / TOUR 4,563 / CHALLENGER 2,026 / WTA_125 702 / TEAM 308 / GS 265 / M1000 144 | cron `25 */6 * * *` | `test_ledger.py`, `test_ops_audit_fixes.py`, `test_run_state.py` | hash-chained (`prev_hash`,`row_hash`), gate TENNIS-12 PASS |
| Market prices (current) | VERIFIED | `kalshi/capture/<day>/<run>.quotes.jsonl.gz` full Kalshi market record (bid/ask/sizes/last/volume/OI/times) | 1,445 tickers / 516 events / 30 series open on 2026-10-02 | ~10-15 min (85-105 passes/day), hourly full snapshot | `test_kalshi_markets.py` (11), `test_run_state.py`, `test_conductor_ci.py` (14) | TENNIS-5 was FAIL at audit time only because the pass was 30.1 min old |
| Market price history | VERIFIED | same, 23 days; plus `books` (depth-10, 801 tickers/day), `trades` (global tape filtered to tennis, 1.36M rows/day), hourly `candles` for newly settled markets, `settlements` | 2,031 MB; per ticker mean 31 / median 25 / max 85-104 quote rows per day | as above | as above | see §7.3 |
| Advanced stats | PARTIAL | Sackmann serve stats; Match Charting Project files downloaded (11 MB, `sources/<run>/sackmann/tennis_MatchChartingProject/`) **but never read by code** (grep: no consumer) | see player metrics | daily | — | no xG-equivalent beyond the structural model |
| Situational splits | PARTIAL | per-surface Elo in rating state; canonical columns `surface, level_canonical, round, best_of, is_qualifying, indoor` (TML only: O 143,990 / I 39,324); pre-registered research segments `tennis_edge/research/segments.py` | see §7.2 | 6 h | `test_gen2.py::test_segments_are_preregistered...` | no home/away, no handedness split stored (hand column exists) |
| Player props | UNAVAILABLE (pricing) / PARTIAL (capture) | `PLAYER_ACES` family parsed, `projectable: False` (`families.py:26`) | 110 markets live+hist at snapshot | — | — | |
| Team props | UNAVAILABLE | — | — | — | — | |
| Game-level markets | VERIFIED (captured + priced) | MATCH_WINNER, SET_WINNER, EXACT_SET_SCORE, TOTAL_GAMES, GAME_SPREAD, TOTAL_SETS, SET_SPREAD | 122,651 settled markets in `historical_markets/` (KXITFMATCH 27,126; KXITFWMATCH 24,900; KXATPCHALLENGERMATCH 14,286; KXATPSETWINNER 9,566; KXATPMATCH 8,166 with `close_time` 2025-06-18..2026-08-01) | daily | `test_kalshi_markets.py`, `test_payoffs.py`, `test_coherence.py` | |
| Play-by-play | UNAVAILABLE | MCP point-level files not ingested; Kalshi in-play game markets not priced | — | — | — | |
| Weather | UNAVAILABLE | `docs/DATA_SOURCES.md` "not wired" | — | — | — | |
| Venue / park effects | UNAVAILABLE (beyond surface) | surface lookup `pricing/competition.py`; `indoor` only from TML | — | — | — | altitude/court speed absent |
| Calibration data | PARTIAL | `settlements/SCORECARD.md` (Brier/log-loss/slope for market vs model on 15,117 rows), `eval/metrics.py` (brier, log_loss, calibration slope, ECE, reliability table, bootstrap), research RESULTS.md | prospective since 2026-09-11; research walk-forwards 1990-2026 | 6 h | `test_metrics.py` (3) | |
| Historical accuracy / postmortems | PARTIAL | `settlements/<run>.jsonl` (16,394 settled rows, 16,084 gradeable), `model_market_discrepancy/AUDIT.{json,md}` (10,910 comparisons, cause classes), `candidate_confirmation/*.json` (7 frozen candidates: 6 INSUFFICIENT_N, W3-001 FAIL_ACCURACY), `research/REJECTED_HYPOTHESES.md` | 23 days | 6 h | `test_prospective_confirmation.py`, `test_discrepancy_sanity.py` (529 lines), `test_frozen_producers.py` | **sports truth 0%** (TENNIS-8): only exchange `result` is used |
| CLV | PARTIAL | `scripts/ops/settle_ledger.py` -> `research/clv/<run>.jsonl` (`clv_executable = close_bid - entry_ask`, `clv_ask_to_ask`, `clv_midpoint`, fees separate) | 2,380 strict rows (A/B first-ball truth); 2,674 with an executable close; strict exec mean -0.0488, ask-to-ask -0.0038, mid +0.0034; by level strict: TOUR 1,812 / WTA_125 448 / GS 108 / M1000 12 / CHALLENGER, ITF, TEAM 0 | 6 h (full re-derivation) | `test_close_clv.py` (5), `test_settle_ledger.py` (3), `test_firstball.py` (16+) | TENNIS-10 FAIL (2,722 of 16,394 closes) |
| Historical wager outcomes | UNAVAILABLE | `accounting-data` ledgers empty; assisted track 0 decisions / 0 wagers (`assisted_decisions/PIPELINE_STATUS.json`) | 0 | — | `test_routed_accounting.py`, `test_assisted_track.py` | |
| Identity tables | PARTIAL | §3 | | | `test_players.py`, `test_names.py` (7), `test_competition_mapper.py` | |
| Schedules | PARTIAL | Kalshi `occurrence_datetime` (nominal, unreliable below tour level), ESPN scoreboard `source_event_timestamp`, `firstball/store/schedule/plan_latest.json` (expected start, court progression) | main tour + Slams only for live evidence | 10 min | `test_start_times.py` (16+) | |
| Seasons / competitions | PARTIAL | sources 1968-2026 (ATP main 1968-, qual/chall 1978-, futures 1991-, WTA 1968-); built from 1990 (`build.py:63 min_year=1990`) | 1990-2026; 2020 COVID gap (21,873 rows) | | | |
| External venue prices | VERIFIED (production capture) | `research/external/market/<day>.jsonl` (Bovada + Smarkets, de-vigged), `dislocations/` | 39,053 rows on 2026-10-03 (bovada 28,536 / smarkets 10,517); 22 days; mapping to Kalshi 69% | every capture pass | `test_wave4_external.py` (517 lines) | |
| First-ball truth | PARTIAL | `firstball/store/truths/` | 3,591 truth rows / 1,798 matches; confidence C 2,839 / B 744 / A 8; sources espn_atp, espn_wta only | 10 min | `test_firstball.py`, `test_start_times.py` | Challenger/ITF/qualifying have no source |
| Official rankings | PARTIAL (acquired, unused) | `sources/<run>/sackmann/tennis_{atp,wta}/{atp,wta}_rankings_*.csv.gz` | ATP 3,420,595 rows 1973-08-27..2026-06-08 (2,333 dates, 17,033 players); WTA 2,146,645 rows 1984-01-02..2026-05-04; `winner_rank/points` on match rows (18% null ATP main) | daily snapshot, frozen upstream | — | no code reads the rankings files (only `bootstrap_sources.py` downloads them) |
| Bookmaker closing odds (historical) | RESEARCH | mirror `tml-data/*_with_odds.csv.gz` (B365/PS/Max/Avg, 16,134 ATP rows 2020-2025), `research/market_benchmark/linked_ATP.parquet` (13,323 Pinnacle-linked) | ATP only | one-off | `test_tennis_data.py` (16) | used for the Pinnacle benchmark only |

---

## 5. Detailed findings per category

### 5.1 Fundamental sources (`tennis-data:tennis-edge-finder/data/sources/`)

Latest Sackmann snapshot `20261002T112052Z` (manifest: forks `Kadantte/tennis_atp` and `VictorSquidWei/tennis_wta`,
upstream 404; MCP upstream; TML `Tennismylife/TML-Database`; tennis-data.co.uk 403). Measured with
`scripts/measure_sackmann.py`:

| file group | rows | years | last tourney_date | serve stats non-null (overall / 2019+) |
|---|---|---|---|---|
| `atp_matches_<y>` (main) | 199,389 | 1968-2026 | 2026-05-25 | 51.1% / 94-98% (2026: 82%) |
| `atp_matches_qual_chall_<y>` | 240,471 | 1978-2026 | 2026-06-01 | 55.5% / 99.1-99.8% |
| `atp_matches_futures_<y>` | 523,355 | 1991-2026 | 2026-06-01 | 4.6% / 0% until 2024, 97.3% in 2025, 99.7% in 2026 |
| `wta_matches_<y>` (main) | 161,902 | 1968-2026 | **2026-04-21** | 29.4% / 91-99% |
| `wta_matches_qual_itf_<y>` | 621,757 | 1968-2026 | 2026-04-27 | 8.3% / ~7% until 2024, 98% in 2025 |
| `atp_matches_doubles_<y>` | 26,399 | 2000-2020 | — | column layout shifted in the fork (header mismatch), not used |
| TML `tml/<y>.csv.gz` | 198,063 | 1968-2026 | 2026-01-17 | 51.6%; carries `indoor` |
| mirror `tml-data/<y>_challenger.csv.gz` | 122,599 | 2000-2026 | **2026-09-21** | 64.2% (2026: 99.7%) |
| mirror `tml-data/<y>.csv.gz` (ATP main) | 80,373 | 2000-2026 | **2026-09-29** | — |
| ESPN `espn_matches_{ATP,WTA}_2026.csv.gz` | 2,292 + 3,891 | 2026-03-28..2026-10-02 | 2026-10-02 | none (results only; surface 54-74% null, best_of 100% null) |

Null rates on ATP main: `minutes` 50.4%, `winner_rank` 18.0%, `winner_ht` 8.4%, `winner_seed` 62.8%, `surface` 1.5%.
Surface vocabulary: Hard/Clay/Grass/Carpet (+ lowercase variants normalised in `sackmann.py:124`).

`build_manifest.json` (built 2026-10-03T05:38Z): 1,631,319 clean rows; by level ATP|ITF 518,952, WTA|ITF 467,977,
ATP|CHALLENGER 282,238, ATP|TOUR_500_250 86,061, WTA|TOUR_500_250 69,813, WTA|WTA_125 47,628, ATP|GRAND_SLAM 30,991,
WTA|GRAND_SLAM 31,067, WTA|OTHER 32,351, ATP|MASTERS_1000 25,862, WTA|MASTERS_1000 15,028, TEAM 11,471 + 10,176,
TOUR_FINALS 556 + 506, OLYMPICS 70 + 572. Seasons 2019: 62,517; 2020: 21,873; 2021: 52,913; 2022: 69,429; 2023: 73,151;
2024: 79,627; 2025: 56,342; 2026: 30,070. Gate TENNIS-13 (manifest sha == parquet sha) PASS.

### 5.2 Rating states and as-of checkpoints (`processed/`)

`ratings_ATP.json`: `model_version elo_surface_k_lo+sr_v0.1`, `n_matches 944,016`, `as_of_date 2026-10-02`,
`baselines {ATP|Hard 0.626, Clay 0.599, Grass 0.649, Carpet 0.646}`, 25,313 players, 8,467 with `sr_points > 0`, 8,587
with n >= 20. `ratings_WTA.json`: 671,176 matches, 25,186 players, 4,619 with serve evidence, baselines Hard 0.557 /
Clay 0.540 / Grass 0.584 / Carpet 0.560. Production Elo config `PROD_ELO = EloConfig(k0=180, use_surface, use_level_k,
use_level_prior)` (`models/state.py:512`); `K = k0/(n+5)^0.4` (`elo.py:89`), surface blend `w = 0.5*n_s/(n_s+20)`
(`elo.py:84`), level priors 1300 (ITF) .. 1600 (Finals) (`elo.py:54`), retirements half weight (`elo.py:131`).

`asof_ATP.json.gz`: `base_date 2026-06-01`, base snapshot for 25,297 players, 8,202 checkpoints for 1,102 players
(2026-06-01..2026-10-02), Gen-2 checkpoints 5,708 for 999 players; WTA 4,462 checkpoints / 555 players, Gen-2 0
(no WTA serve stats after April). Read rule: last checkpoint strictly before the date (`models/asof.py:1-15`).

### 5.3 Prediction ledger (`research/ledger/`)

Row schema (`run_tennis.py:270-291`): `prediction_id, match_id (event ticker), ticker, family, series_ticker, player_a/b
(+ `_id`), tour, level, competition, round, format, surface (+source), subject, line, set_index, exact_score,
model_version, ratings_as_of, git_sha, feature_snapshot_id, data_source_versions{discovery, ratings_built_at},
models{ELO, STRUCTURAL, ENSEMBLE, ELO_DP_FAIR, MARKET_MID, HYBRID_MARKET_MODEL}, inputs{elo_a, elo_b, pa, pb, sr_pa, sr_pb,
spw_baseline, p_*_bo3, structural_used}, quality{score, grade, pillars{experience, serve_return_evidence, recency,
level_familiarity, identity_confidence, format_known}, inputs}, market_quote{yes/no bid/ask, source, quote_ts, volume,
open_interest, liquidity}, scheduled_start, seconds_to_scheduled_start, start_basis, ev{best_side, price, raw_edge,
fee_per_contract, ev_after_fees, bet_up_to}, authority, prev_hash, row_hash`. Quality grades: A 7,298 / B 2,283 / C 2,683 /
D 2,030 / F 2,196. `start_basis`: NOMINAL_UNRELIABLE 10,826 / SCHEDULED 4,898 (doubles rows 766 have none). Rows per
day 100-1,709; rows per ticker median 2, max 8 (224 tickers with >= 5 re-pricings). All quotes came from capture.
Data-quality formula: `score = fmt * idc * exp^0.35 * (0.3+0.7*sr)^0.7 * rec^0.5 * (0.5+0.5*lvl)^0.5`
(`models/quality.py:483`), with `sr = sat(min serve points, 1500)`.

### 5.4 Settlement, timing, CLV, horizons

`settle_ledger.py` reads every capture quote/book/candle stream (`load_stream`, line 57-66) and writes one row per
ledger row per run: timing (`STRICT_PREGAME 2,426 / POST_START 297 / AMBIGUOUS 33 / START_UNKNOWN 13,734`,
`truth_confidence UNKNOWN 12,216 / B 2,722 / C 1,518 / A 34`), CLV (schema in §4), horizons (8 horizons T-6h..T-5m +
LAST_VALID_PREMATCH, `firstball/horizons.py:24-31`; 18,610 of 131,920 populated), settlements (`exchange{status,
result, settlement_value_dollars, settlement_ts, expiration_value}`, `sports: null`, `gradeable`, `y_yes`, `fair_yes`,
`market_mid_at_decision`, `close{basis, strict, n_quotes, n_executable,...}`). `SCORECARD.md`: ledger 16,490, settled
16,394, gradeable with mid 15,117; market mid Brier 0.1776 / LL 0.5257 / slope 1.128; model fair 0.2193 / 0.6323 / 0.816;
strict executable CLV n 2,376 mean -0.0486. `candidate_confirmation/CLV_SCORECARD.json` splits strict CLV by family with
bootstrap CIs (MATCH_WINNER n 956 mean -0.015 [-0.020,-0.011]; GAME_SPREAD n 154 -0.078; EXACT_SET_SCORE n 218 -0.011).

### 5.5 Frozen producers, opportunities, external venue

`opportunities/<day>.jsonl` (schema `tennis_edge/opportunity/schema.py`, 10,267 rows, 25 runs, 1,286 physical matches,
MATCH_WINNER only, lane MODEL_3): `fair_prob` + `uncertainty` + `[low, high]`, executable bid/ask/size, fee, raw /
fee-adjusted / uncertainty-adjusted / robust edge, `ev_curve` (+0..+5c), 12-check `qualification`, `selector_score`,
`decision` (PASS 8,691 / WATCH 1,140 / SHADOW_BET 436), `reason_for/against`, first-ball class, hash chain.
`frozen_producers/shadow_board/<day>.jsonl` (9,765 rows, 2,338 tickers, 5 days): per contract side `gen1_elo_probability,
gen1_sr_probability, gen2_probability, fair_v1_probability, blend_weight, serve_evidence_a/b, fair_envelope` (13 frozen
perturbations, `models/fair.py:333-347`), `model_uncertainty`, `qualification`, `selector_decision`, Kalshi quote.
`frozen_producers/model4/<day>.jsonl`: per listed derivative `gen2_pa/pb`, `conditioning{ask/bid both sides, devig,
p_market_a, service_level}`, `fundamental_distribution{p_match_a, set_score{...}, ...}`, `conditioned_distribution`,
both prices. `external/market/<day>.jsonl`: per venue side `decimal_odds, implied_probability, devigged_probability,
devig_method, source_margin, is_pregame, mapping_status`; `dislocations/`: Kalshi vs external vs model triangulation
(`MODEL_LONE_OUTLIER` etc.), `reference_kind SHARP_REFERENCE`, `n_independent_groups`.

### 5.6 Assisted slate and app export

`assisted_slates/latest.json` (3.1 MB, `assisted_slate_v2`, 135 matches, 481 markets, 178 model prices): per match
`players{a,b}`, `match_winner_ticker`, `first_ball{status, truth_confidence, current_expected_start}`, `model_context`
(gen1/gen2/fair_v1 + envelope, serve evidence, rating_state elo_a/b, serve_point_win_a/b, recent_form_inputs,
data_quality), `external_context` per ticker, `identity_checks`, `markets[]` with `discrepancy_band`,
`model_side_edges`, `model_preferred_side`. `app/latest/manifest.json` counts: board 135, events 135, markets 481,
model_prices 178, recommendations 55 (all `authority RESEARCH_ONLY`, `status RESEARCH_CANDIDATE`), wagers/settlements/
theses 0; `performance.json` entirely empty (`clv.available false`); `event_detail/` 160 files ~9.5 KB each.

---

## 6. Opponent-adjustment audit

**Where**: three production estimators, all chronological.

1. **Elo** (`tennis_edge/models/elo.py`): `expected(ra, rb) = 1/(1+10^((rb-ra)/400))` (line 59); update
   `r += K*(result - expected)` with `K = 180/(n+5)^0.4` (line 89, `state.py:512`), surface-pooled rating
   `r_eff = (1-w)*r_overall + w*r_surface`, `w = 0.5*n_s/(n_s+20)` (line 84); level priors at first appearance;
   walkovers skipped, retirements weight 0.5 (line 131). Opponent strength enters through `expected`; no explicit
   opponent-quality feature beyond that.
2. **Structural serve/return** (`models/serve_return.py:134-260`): abilities `s_i, r_i` as logit deviations from a
   running tour-by-surface baseline (`baseline(tour, surface)`, line 200, numerator/denominator of all serve points);
   prediction `logit p_A_serve = base + s_A - r_B` (line 145); update uses the opponent's **current** ability:
   `excess = logit(rate) - (base + s_p - r_o)` (line 247), accumulated point-weighted, with exponential decay
   `exp(-days/365)` (line 194) and shrinkage `ability = num/(den + 600)` (`n_prior_points`, lines 179, 211-212).
   Sequential (single pass, no iteration to convergence), i.e. "simple" opponent adjustment against the opponent's
   pre-match estimate; baseline is tour|surface, not level-aware.
3. **Gen-2** (`models/gen2.py`, `MODEL_VERSION gen2_dyn_hier_sr_v1`): same decomposition plus per-(tour, level)
   offsets with `level_prior_points 20,000` (line 73, 127-134), per-player surface deviations with
   `surface_prior_points 1,500`, player prior 500 points (line 66-71), precision carried as evidence; blended with the
   rating in `fair_v1` (`models/fair.py:323-347`: `blend_points 1,500` = evidence at which Gen-2 gets half weight).
   Players without serve stats are seeded from their Elo-implied point probability (`gen2.py:177-184`).

**Sample requirements**: none hard-coded as a cut-off; shrinkage does the work (600 / 500 / 1,500 / 20,000 points).
Data-quality grade requires `sr` evidence to reach B/A (`quality.py:475-484`); `selector_v1` requires
`data_quality >= 0.60` and `env_width <= 0.15` (`selector/decide.py:36-40`); `qualify` requires grade C+
(`opportunity/qualify.py:34`).

**History**: the production state is a single end-state (`ratings_<tour>.json`, overwritten every 6 h). Walk-forward
history exists only as (a) `processed/asof/` checkpoints from 2026-06-01 for players who played since (1,102 ATP / 555
WTA), (b) `research/elo_study/predictions_{ATP,WTA}.parquet` (per-match pre-match predictions 1990-2026, 45 MB, one-off),
(c) the ledger's `inputs.elo_a/elo_b/sr_*` at every pricing time (16,490 rows). No per-player rating-by-date table is
published.

**Tests**: `tests/test_elo_leakage.py` (prediction precedes update; walkover/retirement weights), `tests/test_gen2.py`
(13: no price input, point-probability convention, no decay-to-baseline under repeated evidence, thin-player shrinkage,
market conditioning, serve-point validation), `tests/test_scoring_engine.py` (DP invariants, MC agreement),
`tests/test_frozen_producers.py` (sha256 pins of every frozen model source). `serve_return.py` has **no dedicated unit
test**; its behaviour is covered indirectly by Gen-2's tests and by the research scripts (`scripts/research/sr_study.py`).

**Limitations** (`docs/KNOWN_LIMITATIONS.md`, `docs/MODELING.md`): worse than Pinnacle on every cut (Brier +0.0085 to
+0.0143) and worse than Kalshi (0.206 vs 0.187 on 1,869 settled markets); no rest/fatigue/travel/injury/court-speed
features; serve stats absent for most ITF and all WTA after April 2026; Elo over-confident at K0 >= 250.

---

## 7. Inventories

### 7.1 Time-series-capable datasets

| dataset | x-axis | key | rows | linked to match id? | linked to opponent id? |
|---|---|---|---|---|---|
| Canonical match table (per player per match) | `tourney_date` (+ round order) | `match_key`, `winner_id/loser_id` | 1,631,319 (1990-2026) | yes (`match_key`) | yes (the other id on the row) |
| Elo/SR pre-match predictions (research) | `tourney_date` | `match_key` | ~1.6M across `elo_study/predictions_*.parquet` | yes | yes |
| As-of rating checkpoints | match date | `player_id` | 8,202 ATP / 4,462 WTA (2026-06-01..10-02) | no (date only) | no |
| Prediction ledger | `generated_at_utc` (6-h runs) | `ticker`, `prediction_id` | 16,490; median 2 / max 8 points per ticker | `match_id` = event ticker; `physical_match_id` only via producers | `player_b_id` on the row |
| Shadow board companion | `predicted_at` | `ticker` | 9,765 (2,338 tickers, 5 days) | `physical_match_id` | both player ids |
| Model 4 board | `predicted_at` | `ticker` | ~400/day since 09-28 | yes | yes |
| Kalshi quotes | `captured_at` (10-15 min) | `ticker` | ~45,000 rows/day, 23 days | event ticker (both sides are separate tickers) | via event -> players (parser) |
| Kalshi books (depth 10) | `captured_at` | `ticker` | ~10-21k rows/day | yes | — |
| Kalshi trades | `created_time` | `trade_id` | ~1.4M rows/day (note 2026-09-20: 1,439,517 rows, only 670,864 distinct `trade_id` — backlog re-reads duplicate) | ticker | — |
| Kalshi candles (hourly sweep + initial discovery) | `end_period_ts` (60-min lifetime, 1-min last 3-6 h) | `ticker` | 645/day + 15,894 initial | ticker | — |
| Horizons | fixed offsets before first ball | `prediction_id x horizon` | 131,920/run (18,610 populated) | yes | — |
| External venue prices | `observed_at` (per pass) | `source_event_id, side, strike` | 39,053/day | `physical_match_id` when mapped (69%) | — |
| First-ball observations | `observed_at_utc` (10 min) | `match_id` (Kalshi event) | 7,892 rows on 2026-10-03 (267 MB over 23 days) | yes | — |

### 7.2 Split-capable datasets (dimensions actually stored, with counts)

* Canonical table: `surface` (ATP main: Hard 80,877 / Clay 70,920 / Grass 23,702 / Carpet 20,900 / null 2,990),
  `level_canonical` (10 values, counts in §5.1), `round` (R128..F, Q1-Q3, RR, BR), `best_of` (+`best_of_inferred`),
  `is_qualifying`, `indoor` (TML rows only), `source_kind` (main/qual_chall/futures/qual_itf), `season`, `outcome_type`
  (COMPLETED/RETIRED/WALKOVER/DEFAULT), `winner_hand/loser_hand` (0% null), `winner_ioc`, `winner_age`.
* Rating state: per-player per-surface Elo and surface match counts (4 surfaces); baselines per `tour|surface`.
* Ledger / CLV / settlements: `tour, level, family, surface, discipline, timing_class, truth_confidence, data_quality_grade`
  (`settle_ledger.py:43`), summarised in `research/segments/coverage.json` (e.g. level: TOUR_500_250 n 4,563 strict 1,812;
  ITF n 8,482 strict 0; family MATCH_WINNER 12,778).
* Pre-registered research dimensions (`research/segments.py`, 2026-09-12): tour, surface, market_family, favourite_prob,
  disagreement_pp, liquidity, spread_width, time_to_first_ball, data_quality, model_uncertainty, side, derivative_position.
* Discrepancy audit: gap bands x level x cause class (10,910 comparisons).
* Not stored anywhere: home/away (no concept), handedness matchups, game-state, strength state.

### 7.3 Market history

Per-ticker quote time series **exist** at ~10-15 min granularity (fingerprint-changed rows + hourly full snapshots,
`capture_tennis.py:105-108`), retained indefinitely on the branch (runner cleans only its working copy,
`tennis-capture.yml:109`). 2026-10-02: 85 passes, median gap 15.5 min, max 56.7 min; 45,438 quote rows over 1,445
tickers (mean 31.4, median 25, max 85 per ticker); 21,475 depth-10 books over 801 tickers; 1,364,610 tennis trades;
653 settlements; 645 settled markets candled (22 hourly + 176 one-minute candles each). 2026-09-20: 105 passes, median
gap 12.7 min, max 26.5. Size: 2.03 GB / 23 days; trades are 86% of it. Pre-2026-09-11 history: initial discovery
candles (60-min lifetime + 1-min last 6 h) and up to 5,000 trades per market for 15,894 settled markets across 21 series
(KXITFMATCH 3,202, KXITFWMATCH 2,538, KXATPCHALLENGERMATCH 1,145, KXATPSETWINNER 1,128, ..., KXATPMATCH 370, KXWTAMATCH
360); daily `historical_markets/` carries the full settled record (result, settlement_ts, final bid/ask, volume, OI) for
122,651 markets back to 2025-06-18. First 5.7 h of capture on 2026-09-11 were lost (KNOWN_LIMITATIONS).

### 7.4 Projection / simulation outputs

| output | per entity? | per market? | distribution or point? | stored across runs? |
|---|---|---|---|---|
| `run_tennis.py` ledger | per contract (ticker) | yes, all 7 priced families | point (6 model numbers) + inputs; the DP distribution is computed (`sim/analytic.py:171`) but not stored | yes, every 6 h, append-only |
| `projections/<run>.json` | same + coverage/excluded | yes | point | per run (92 files) |
| Shadow board (`fair_v1`) | per contract side | match winner only | point + 13-config envelope + uncertainty | yes (daily files) |
| Model 4 board | per listed derivative | EXACT_SET_SCORE / GAME_SPREAD / TOTAL_GAMES | **full set-score / games distribution** (fundamental + conditioned) | yes since 09-28 |
| Doubles (`doubles_baseline_prior_v0`) | per contract | match winner | point, grade capped C, suppressed on the slate | ledger (766 rows) |
| Tournament winner / draw | — | parsed, not priced | bracket DP exists (`futures/draw.py`) | no |
| Monte Carlo (`sim/montecarlo.py`, 100k paths) | validation only | — | samples | no |

---

## 8. Existing ranking / percentile / league-average code

* **No player ranking, percentile or leaderboard code** exists in `tennis_edge/` or `scripts/` (grep for
  `percentile|quantile|rank(` hits only bootstrap CIs and quote-age summaries). Elo per player makes a leaderboard
  trivial but none is produced.
* League/tour averages: serve-point baselines per `tour|surface` (`serve_return.py:200`, stored in `ratings_*.json`
  `baselines`), Gen-2 tour baseline + level offsets (`gen2.py:127-134`); `spw_baseline` on every ledger row.
* Official rankings: present in sources (3.4M ATP / 2.1M WTA rows) and as `winner_rank/points` columns; the "naive rank"
  model (`docs/MODELING.md`) used them in research only.
* Evaluation helpers: `eval/metrics.py` (`brier, log_loss, accuracy, calibration_slope_intercept, ece, reliability_table,
  bootstrap_diff, summary`), `confirmation/stats.py` (percentile bootstrap), `research/segments.py` bucketing.

---

## 9. Size estimates (bytes, if the research layer exposed them)

| surface | basis | estimate |
|---|---|---|
| (a) Player profiles | `ratings_ATP.json` = 5.4 MB / 25,313 players = ~214 B compact; a profile with overall + 4 surface Elo, SR abilities, evidence, last date, plus W-L by surface/level/season (derived from the canonical table) ~1.5-2 KB | ~8 MB for the 4,216 players active in 2026; ~100 MB for all 50,499 ids (lazy per player) |
| (b) Team profiles | n/a (doubles teams are `sorted(ids)` keys with a baseline prior) | 0 |
| (c) Per-match detail | canonical row ~50 columns ~400 B as JSON; 1.63M matches = ~650 MB total; a player's career = n x 400 B (median career tens of matches; a top player with 1,000+ matches = ~400 KB); a Kalshi event packet (`event_detail/*.json`) = ~9.5 KB today | per player 10-400 KB; per event ~10 KB |
| (d) Metric time series | as-of checkpoints 3.7 MB gz for 1,102 ATP players (~3.4 KB/player gz over 4 months); a full Elo trajectory from `elo_study` predictions ~2 x 24 MB parquet for both tours; ledger model-vs-mid series per ticker: ~2-8 points x ~1.2 KB | ~5-20 KB per player for a career Elo path if materialised |
| (e) Market history | raw quote record ~1.5 KB x 31 rows/ticker/day = ~45 KB per ticker-day raw; reduced to (ts, bid, ask, sizes, last) ~40 B/point = ~1.2 KB per ticker-day; whole open board ~1.8 MB/day reduced; candles ~100 KB per settled market raw; trades 1.7 GB / 23 days raw (not for the app) | reduced quote series: ~1-5 KB per ticker, ~2 MB/day board-wide |

---

## 10. Recommended research capabilities to expose in this pass

Supported by the evidence (production-generated, tested, with history):

1. **Player profile from the rating state**: overall and per-surface Elo (with match counts), structural serve/return
   abilities, serve-point evidence, last match date, data-quality pillars — `processed/ratings_{ATP,WTA}.json`
   (6-h cadence, 50k ids). Mark as model output, RESEARCH_ONLY authority, with `ratings_as_of`.
2. **Match-winner and derivative model prices with history**: the ledger's six model numbers + market mid per contract
   per run (16,490 rows), joined to settlement outcome and strict CLV where it exists. Expose as a per-ticker series
   (x = run time) with the row's `quality.grade` and `start_basis`.
3. **Kalshi quote history per ticker** at 10-15 min granularity plus depth-10 books and hourly candles, 23 days deep;
   settled results for 122,651 markets. This is the strongest asset and currently invisible to the app
   (`event_detail` carries no price history).
4. **External venue comparison** (Bovada/Smarkets de-vigged vs Kalshi vs model, with triangulation verdict) per
   contract per pass (`external/dislocations/`), 22 days.
5. **First-ball / start-time status** per match (verified start, bracket, confidence, planner window) for ATP/WTA/Slams.
6. **Match history per player** (opponent, score, surface, level, round, serve stats, outcome type) and H2H, built from
   the canonical table — expose as RESEARCH/PARTIAL because the table is rebuilt per run from frozen forks (ATP stats
   to 2026-06-01, mirror results to 2026-09-29; WTA stats to 2026-04-27, ESPN results to 2026-10-02) and is not itself
   published; publishing a per-player slice of it is the enabling step.
7. **Calibration / scorecard**: market-vs-model Brier/log-loss/slope on settled rows, strict CLV by family and level
   with CIs, candidate-confirmation statuses — all produced every 6 h.

Mark RESEARCH: Model 4 derivative distributions (5 days, listed derivatives only), fair_v1 envelopes/selector decisions
(research-only by definition), doubles prices (failed the no-skill test, suppressed), the Pinnacle benchmark and
elo_study parquet files (one-off), Match Charting Project files (downloaded, unread).

Mark UNAVAILABLE: injuries/availability, draws/lineups and tournament-winner pricing, weather/venue beyond surface,
official rankings in any product path, play-by-play, player props (aces), team metrics, recent-form W-L windows,
schedule strength, wager outcomes (ledgers empty), sports truth independent of the exchange.

---

## 11. Open questions / UNKNOWN items

* **Why `processed/matches.parquet` is absent from `tennis-data`**: `tennis-run.yml:139` publishes `data/processed`
  with `|| true`; `publish_branch.py:79-82` skips files > 95 MB with a warning. The table is 1.63M x 50 columns; I did
  not rebuild it to measure its parquet size (the scratch disk was at 72%). Checked: ls-tree of the branch, the
  workflow step, the publisher. UNKNOWN whether it is the size skip or a gitignore.
* **Commit count / full history of `tennis-data`**: fetched `--depth=1` only (66 code commits on `main`); the branch
  README states it carries pre-migration commits with original SHAs (`MIGRATION_AUDIT.md`).
* **Trade-tape duplication**: 2026-09-20 shows 1,439,517 trade rows but 670,864 distinct `trade_id` (2026-10-02: all
  1,364,610 distinct). Backlog re-reads (`kalshi/trade_tape.py`) appear to re-emit windows; whether consumers dedupe
  was not verified.
* **Sports truth**: TENNIS-8 reports 0 of 16,084 settled rows with gradeable sports truth; `settlements[].sports` is
  null everywhere. The ESPN results feed exists but is not joined to the ledger as truth — checked `settle_ledger.py`
  and `ledger/truth.py`; no join code found.
* **Kalshi capture gaps**: the 56.7-min maximum gap on 2026-10-02 and the 2026-09-11 5.7-h loss are documented; a
  per-day gap audit over all 23 days was not run.
* **Docs drift**: `docs/IDENTITY.md` ("production ratings use sackmann only") and `docs/KNOWN_LIMITATIONS.md`
  ("cross-source id systems are not linked") predate `identity/crosswalk.py`; `models/state.py` output shows three id
  systems in production ratings.
* **ESPN rankings API** is noted as reachable and "not yet ingested" (`docs/DATA_SOURCES.md`); nothing in the repo reads it.
* `data/research/` on `main` is a stale September copy (29 files) and should not be mistaken for production output.
