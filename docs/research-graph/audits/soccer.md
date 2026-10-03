# Phase 2 research-data audit — SOCCER (`chmoses98/soccer-edge-finder`)

Audited 2026-10-03 against `main` @ `82f1daa` and `origin/data-archive` @ `cd427e0` (shallow fetch, depth 1; branch extracted with `git archive` to `scratchpad/phase2/soccer/archive/`) plus `origin/accounting-data` @ `299dc88`. Read-only; nothing was written inside the repository. All counts below were measured with Python against the extracted files; commands are reproducible from the paths cited.

---

## 1. Summary

1. **What soccer genuinely has:** a 7-day-old, production-scheduled Kalshi evidence pipeline (discovery → change-suppressed quote snapshots → Dixon-Coles team-strength fit → correlated-world simulation → per-contract fair probabilities with 80 % intervals → append-only prediction ledger → universal settlement → calibration/CLV reports). Every one of those stages has a cron/chain workflow, tests, a manifest-verified archive, and real rows (15,557 predictions, 10,874 settlements, 321,621 market quotes, 4,849 ESPN results, 3,717 post-hoc XIs).
2. **Strongest areas:** market price history (per-ticker time series, median 16 observations/ticker, 7,577 tickers with ≥10 points); projection history (per fixture×ticker, median 4 run snapshots, up to 10); settlement/calibration machinery (219 model×family×horizon health cells, close_v2 and fee-aware CLV on every settled record); the identity layer (slug ids, explicit ESPN map of 486 teams, fixture ids parsable into competition/season/home/away); a deep international results archive on `main` (48,249 matches 1872→2026 with point-in-time Elo).
3. **Weakest areas:** (a) **team-level ratings are never persisted** — only a `parameter_hash` of the posterior is archived; attack/defence/home-advantage must be refit from results to be shown; (b) **no player metrics at all** — only ESPN lineup sheets (ids, names, positions, starter/sub flags), no minutes, goals, xG or a player registry (`data/registry/*.json` has `players: 0`); (c) **ESPN results carry goals only** (HT split 3 %, shots/corners/cards/xG 0 %); (d) **club top-5 league history is not in the repo** — it is fetched at run time from a GitHub CSV redistribution into a gitignored cache; (e) history is **7 days** on `data-archive` (2026-09-27 → 2026-10-03), so every "trend" is a week long.
4. **The model is RESEARCH_ONLY everywhere** (walk-forward log loss 1.0010 vs Bet365 0.9732; hybrid weight 1.0 in every season). The international pool, which is 95 % of the archived predictions this week, is explicitly flagged "not validated" (`docs/KNOWN_LIMITATIONS.md` #26) and gated out of shadows.
5. **The app export today** (`src/soccer_edge/app_export.py`) reads only the latest slate/run/board/status/heartbeat; it ignores the prediction ledger, settlements, snapshots, ESPN results, lineups, weather, reference odds and every evaluation report. `price_history` in the contract is always `[]`.
6. **Opponent adjustment** is intrinsic (Dixon-Coles attack_i − defence_j solved jointly by MAP), with time decay (ξ = 0.0065/day, half-life ≈ 107 d), 730-day lookback, ≥ 50 results per competition, new-team priors below 8 effective matches. Production still runs `dc_laplace_v1` (100 % of ledger rows), not the audited v2.

---

## 2. Data branches and layout

| Branch | Tip | Root / purpose | Mutability | Size |
|---|---|---|---|---|
| `main` | `82f1daa` (129 commits since 2026-09-27) | code, docs, `config/`, `data/registry`, `data/mappings`, `data/international`, `data/research`, `data/catalog/latest_index.json`, `data/diagnostics/*` | registry/research frozen; `data/catalog`, `data/diagnostics` **overwritten** by `kalshi-discover`, `espn-lineups`, `diagnostics` workflows | small (research JSON ≈ 2.2 MB; `results_v1.csv.gz` 1.1 MB) |
| `data-archive` (orphan) | `cd427e0` | append-only evidence; manifest-verified (`manifest/files.json`, `manifest/records/<day>.jsonl`, 31,012 record rows) | ledgers append-only; `runs/latest.*`, `STATUS.json`, `snapshots/STATUS.json`, `app/*`, `dispatch/schedule.json`, `*/last_fingerprints.json` are **replaced**; closed days gzipped monthly (`archive-compact.yml`, cron `37 3 2 * *`) | 784 files, 378 MB tree (363 MB extracted) |
| `accounting-data` | `299dc88` | `data/accounting/wagers.jsonl`, `settlements.jsonl` (router-filed manual bets) | append-only | **both files empty** (0 rows) |
| `claude/edge-finder-app-readiness-x5li03` | — | prior-phase work branch | — | — |

`data-archive` directory sizes (extracted): `snapshots/` 214 MB · `predictions/` 51 MB · `lineups/` 30 MB · `settlements/` 25 MB · `runs/` 21 MB · `manifest/` 9.2 MB · `app/` 4.7 MB · `results/` 3.2 MB · `fixtures/` 3.2 MB · `evaluation/` 2.7 MB · `dispatch/` 532 KB · `weather/` 472 KB · `claims/` 312 KB · `odds_api/` 172 KB · `reference/` 148 KB.

Full tree (day-sharded unless noted):

```
STATUS.json                      ESPN sync status (replaced; health, unmapped ids, map proposals)
app/latest/*.json, event_detail/ unified app payload (derived view; 43 events, 1,203 markets)
claims/odds_api/<hh>/<hash>.json, claims/odds_api_calls/<day>/   spend claims (dispatch)
dispatch/{chain_lease,diagnostics,first_seen,reliability,schedule}.json, horizons.jsonl (345 rows),
         settle_runs.jsonl, heartbeats/<day>.jsonl (72 rows), slate_log/<day>.jsonl
evaluation/*.json, SUMMARY.md    settle-evaluate outputs (replaced daily)
fixtures/<day>/espn-HHMMSS.json  per-batch ESPN fixture windows (105 on 10-02; many empty)
fixtures/espn/<day>/espn-*.json  change-suppressed full ESPN fixture dumps (13 files; 235 fixtures latest)
lineups/<day>/<league>.jsonl     prospective XI snapshots (847 rows / 234 events / 22 leagues)
lineups/history/<league>.jsonl   post-hoc XI backfill, top-5, 2024-08 → 2026-09 (3,717 rows)
lineups/last_hashes.json         change-suppression state
odds_api/raw/<day>/*.json.gz     17 raw Odds API responses; odds_api/budget/<day>.jsonl; STATUS.json
predictions/<day>/predictions.jsonl[.gz] + index.json   prediction ledger (15,557 rows, 6 days)
recovery/*.json                  one-time recovery records
reference/<day>/oddsapi-*.jsonl  Pinnacle reference rows (112 rows / 9 fixtures); STATUS.json
results/espn/<league>.jsonl      ESPN results archive, 14 leagues (4,849 rows, 2024-07 → 2026-10-03)
runs/<day>/<run_id>/run_output.v1.json (+coverage.json, RUN_SOCCER.md; ≤09-28 also priced_contracts.json 5.7 MB)  43 runs
runs/latest.{actionable_slate,model_board,run_output}.v1.json (replaced)
settlements/<day>/predictions.jsonl[.gz] + index.json   settlement ledger (10,874 rows, 5 days)
snapshots/<day>/cap-<ts>.jsonl   Kalshi quote captures (177 files, 321,621 rows); STATUS.json; reconcile.json
weather/<day>.jsonl[.gz], geocode_cache.json   Open-Meteo kickoff-hour forecasts (1,017 rows / 189 events)
tick.log, backfill.log, README.md
```

Writers and cadence (from `.github/workflows/*.yml`):

| Workflow | Schedule | Writes |
|---|---|---|
| `kalshi-capture.yml` | `23,53 8-22 * * 5,6,0,1` and `37 */2 * * 2,3,4` | `snapshots/`, `reference/`, slate reprice, `app/latest` |
| `kickoff-dispatch.yml` | self-chaining bounded job (re-plans every 30 min, slate refresh every 15 min, lineup/Kalshi/reference captures at T-120/60/30/15/5) + hourly backup cron `7 * * * *` | `snapshots/`, `lineups/<day>`, `fixtures/<day>`, `reference/`, `runs/<day>/<run>`, `predictions/`, `dispatch/*`, `app/latest` |
| `run-soccer.yml` | `11 7,11,15,18 * * *` (skips when no fixture in window) | `runs/`, `predictions/`, `app/latest` |
| `settle-evaluate.yml` | `49 4 * * *` + dispatch from the chain | `settlements/`, `evaluation/*`, `dispatch/settle_runs.jsonl` |
| `espn-lineups.yml` | `17 */2 * * *` | `lineups/<day>`, `fixtures/espn/`, `results/espn/`, `weather/`, `STATUS.json`; `data/diagnostics/latest_espn_status.json` on main |
| `kalshi-discover.yml` | `17 5 * * *` | `data/catalog/latest_index.json` on main |
| `archive-compact.yml` | `37 3 2 * *` | gzips closed days |
| `espn-backfill.yml`, `lineup-backfill.yml`, `research-*.yml`, `odds-api-probe.yml`, `probe-*.yml` | manual (`workflow_dispatch`) | `results/espn/`, `lineups/history/`, `data/research/*` |

`docs/SCHEDULER.md` records that GitHub cron delivered ≈ 4 % of slots; the chain (`dispatch/reliability.json`: 28 heartbeats in the last 24 h, 18 from `workflow-run`) is the real timing authority. `kickoff-dispatch` is the only reason near-kickoff captures exist.

---

## 3. Identity model

| Entity | Canonical id | Where defined | Coverage | Notes |
|---|---|---|---|---|
| Team | lowercase slug `eng.arsenal`, `nat.sco`, `arg.boca_juniors`; `Team(team_id, name, country, gender, kind, parent_team_id, aliases, provider_ids)` (`src/soccer_edge/identity/models.py:94-118`) | `data/registry/{seed,uefa_clubs,americas_and_nations,espn_probe_additions}.json` | **622 teams** (238+131+104+149): 487 club, 135 national, 2 reserve; 43 women's | aliases scoped by (country, gender, kind); ambiguity raises (`registry.py:100-125`); `provider_ids` carry `football_data_couk` / `openfootball` names (seed only) |
| Competition | slug `uefa.nations_league` | `data/registry/seed.json` (38) | 38 | includes `fifa.world_cup_qualifiers` (one id for all 5 confederation qualifiers) |
| Season | `2026-27` or `2026` | derived (`run/inputs.py:current_season_id`, ESPN provider) | — | Liga MX Apertura/Clausura share `2024` → **fixture-id collisions** (see below) |
| Fixture | `fx:<comp>:<season>:<home>:<away>[:<stage>][:legN][:occN]` (`identity/models.py:Fixture.make_id`) | minted by each provider | all ledgers key on it | openfootball adds matchday as stage; ESPN adds nothing; reference rows use the match date as stage → cross-source joins are by (comp, home, away, date ±1 d) (`settlement/resolve.py::ResultIndex`) |
| ESPN event | `espn_event_id` (string) | `fixtures/espn/*`, `results/espn/*`, `lineups/*`, `weather/*` | 100 % of ESPN rows | app export puts it in `event.source_ids` |
| ESPN ↔ canonical | `data/mappings/espn_map.json` v4: 41 league slugs → competition, **486 ESPN team ids → team_id**, 68 verified league slugs, 5 unmapped-from-probe; unresolved ids reported in `STATUS.json` (9 today: Burkina Faso, Mali, India, Russia, Fiji, Vanuatu, Djibouti, Sri Lanka, Namibia) | explicit, exact alias resolution, never fuzzy | — | the `espn-backfill` diagnostics list dozens of unmapped AFC/CAF national ids |
| Player | **none canonical** — `Player` schema exists (`identity/models.py:121`) but every registry file has `players: 0`; lineup rows use ESPN `athlete_id` + display `name` | `lineups/**` | 5,067 athlete ids in history, 3,787 in daily snapshots | no cross-source player map; no FBref/Transfermarkt ids |
| Kalshi | `ticker`, `event_ticker`, `series_ticker`; association to fixtures via event titles + registry (`kalshi/association.py`); team codes inside tickers are not identity (`KNOWN_LIMITATIONS` #16) | snapshots/predictions/settlements | 11,868 tickers seen | `data/catalog/latest_index.json` has `contracts_by_competition_code` incl. 449 `?` |
| International dataset | `intl:<team-slug>` (e.g. `intl:vietnam`), 231 current FIFA members | `data/international/results_v1.csv.gz` | 48,249 rows | **different id scheme from `nat.xxx`**; mapped only inside research (`research/intl_hier.py`); not joinable to the ledger without a map |
| xG history | plain team names (`Bastia`, `Paris Saint-Germain`) + division code | `data/research/xg_history_v1.csv.gz` | 12,676 rows | no ids |

Known collisions / gaps measured:
* `results/espn/*.jsonl`: **207 duplicated `fixture_id`s** (e.g. `fx:mex.liga_mx:2024:mex.tigres:mex.necaxa` — Apertura and Clausura both in season `2024`, no `occurrence` suffix) and **85 duplicated `espn_event_id`s** (intended: evidence-upgrade rows, "last row wins", `docs/SETTLEMENT.md`). A research layer must dedupe by `espn_event_id` (last) and treat fixture ids as non-unique for Liga MX/Argentina.
* `weather/*.jsonl` rows carry `espn_event_id` and `league` but **no `fixture_id`** — joinable only through `fixtures/espn` dumps.
* App export keys: `participant.source_ids.team_id` = the slug, `event.source_ids.{fixture_id, espn_event_id}` — the same keys every archive file uses, so a research layer can key on `team_id`/`fixture_id` with no translation.

---

## 4. CAPABILITY MATRIX

Status vocabulary per spec. "Cadence" is the workflow that produces the data.

| Capability | Status | Origin | Coverage | Cadence | Tests | Notes |
|---|---|---|---|---|---|---|
| Team metrics (fitted attack/defence/home adv.) | **PARTIAL** (computed every run, **not stored**) | `run/modeling.py::fit_competition` → `model/strength.py` (v1) | 11 competitions priced this week; only `parameter_hash` archived | run-soccer 4×/day + chain refresh | `test_simulation.py`, `test_model_v2.py` | `ParameterPosterior.team_summary()` exists (`strength.py:107-117`) but nothing writes it |
| Team metrics (goals for/against, form) | **PARTIAL** | derivable from `results/espn/*.jsonl` (ESPN leagues) and at run time from football-data CSV (top-5) | 4,849 ESPN results; top-5 history not in repo | espn-lineups 2 h (results appended); backfill manual | `test_espn_provider.py::test_results_from_events_and_archive_round_trip` | no precomputed table |
| Player metrics | **UNAVAILABLE** | — | — | — | — | `PlayerMatchStats` protocol only (`providers/interfaces.py:69`) |
| Game logs — team | **PARTIAL** | `results/espn/<league>.jsonl` (goals, HT 3 %, neutral, ET/pens flags) | 14 leagues, 2024-07-01 → 2026-10-03 | 2 h | as above | shots/corners/cards/xG 0 % non-null |
| Game logs — player (appearances) | **PARTIAL** | `lineups/history/*.jsonl` (post-hoc XI + bench, subbed flags), `lineups/<day>/*` | 3,717 top-5 matches 2024-08-15 → 2026-09-20; 847 daily rows | history manual backfill; daily 2 h + chain | `test_espn_provider.py`, `test_lineups.py`, `test_lineup_report.py` | no minutes/goals per player; 0 overlap with `results/espn` |
| Historical opponents / results | **PARTIAL** | `results/espn` + `data/international/results_v1.csv.gz` (48,249 intl matches, Elo) | see above | 2 h / frozen | `test_international.py` | top-5 club history is runtime-only |
| Opponent adjustment | **VERIFIED (code)** / **PARTIAL (data)** | Dixon-Coles MAP (`model/strength.py`, `strength_v2.py`) | every priced fixture | each run | `test_simulation.py::test_fit_recovers_structure`, `test_point_in_time_fit_ignores_future`, `test_sparse_team_has_wider_posterior`; `test_model_v2.py` (5) | see §6; outputs not persisted |
| Schedule strength | **UNAVAILABLE** (as a metric) | implicit in DC | — | — | — | could be derived from stored ratings if persisted |
| Recent-form windows | **PARTIAL** | `run/context_features.py` (rest days, matches in 14/28 d) on 99.2 % of ledger rows (`context` field) | 15,440 rows | each run | `test_context_features.py` | no L3/L5 goal form |
| Usage (minutes) | **UNAVAILABLE** | priors only (`model/lineups.py`: 80/15 min) | — | — | — | ESPN summary has no minutes |
| Lineups / formations | **PARTIAL** | `lineups/<day>/<league>.jsonl` (`espn_lineup_snapshot_v1`) | 234 events / 22 leagues; 53 events confirmed pre-kickoff; 50 % of passed fixtures had a pre-kickoff XI, median lead 37 min (`evaluation/lineup_lead_time.v1.json`) | chain T-60/30/15/5 + 2 h | 4 test files | not used by pricing (`lineup_state=unknown` on 100 % of predictions) |
| Injuries / availability | **UNAVAILABLE** | — | — | — | — | `InjuryProvider` unimplemented |
| Matchup metrics | **UNAVAILABLE** | — | — | — | — | — |
| Projection distributions | **PARTIAL** | `runs/latest.model_board.v1.json`: per contract `p, p_low, p_high, param_sd, q[100]` (percentiles 0.5…99.5); per fixture `summary` (mean goals, p_home/draw/away, btts, over 2.5) | 33 fixtures, current board only | each run; **overwritten** | `test_run_pipeline.py`, `test_actionable_slate.py` | quantiles not archived across runs; realisations never stored |
| Raw projections (point) | **VERIFIED** | `predictions/<day>/predictions.jsonl`: `probability.{fair_probability_mean, median, low, high, parameter_sd, n_worlds}` per ticker per run | 15,557 rows, 134 fixtures, 3,814 fixture×ticker, 6 days | each run, append-only, manifest-hashed | `test_archive_settlement_eval.py::test_ledger_append_only/_detects_tampering` | `model_version` = `dc_laplace_v1` on 100 % |
| Market prices (current) | **VERIFIED** | `runs/latest.actionable_slate.v1.json` (2,406 sides / 33 fixtures), `app/latest/markets.json` (1,203) | every open Kalshi soccer contract in 48 h | ≤ 15–30 min | `test_actionable_slate.py`, `test_app_contract_v1.py` | — |
| Market price history | **VERIFIED** | `snapshots/<day>/cap-*.jsonl` | 321,621 rows, 11,868 tickers, 2,341 events, 2026-09-27 23:45 → 10-03 06:12; 177 batches | 30 min (chain) + crons; change-suppressed | `test_kalshi_discovery.py`, `test_reconcile_discovery.py`, `test_microstructure.py` | `orderbook` always null; `horizon`/`minutes_to_kickoff` fields unreliable (§7) |
| Advanced stats (xG) | **RESEARCH** | `data/research/xg_history_v1.csv.gz` (12,676 top-5 matches 2016-17→2022-23, xG + nsxG); live `HxG/AxG` via `football_data_couk.results_with_xg` (not archived) | historical only | frozen | `test_xg_family.py`, `test_xg_strength_eval.py` | team names, no ids |
| Situational splits | **PARTIAL** | home/away implicit in DC; `neutral_site` flag (16 of 4,849 ESPN rows; dataset flag in intl CSV); `competitive` flag intl | — | — | `test_international.py::test_espn_neutral_inference_rules` | no stored split tables |
| Player props | **UNAVAILABLE** | taxonomy has `player_goals/assists/shots/cards` families; dispositioned `UNSUPPORTED_FAMILY` | observed in catalog, never priced | — | — | — |
| Team props (team total, clean sheet) | **VERIFIED** (team_total) | priced families incl. `team_total`, `first_half_*`, `btts`, `exact_score`, `first_to_score` | 630 team_total rows 10-02 | each run | `test_pricing.py` | — |
| Game-level markets | **VERIFIED** | `match_result_3way`, `handicap`, `total_goals`, … | 57-value `MarketFamily` enum (`kalshi/taxonomy.py:26-69`) | — | — | season futures/specials observed, unpriced |
| Play-by-play | **UNAVAILABLE** | ESPN timed scoring plays used only for settlement evidence (`providers/espn.py::result_evidence`) | 161 rows with regulation split | — | `test_settlement_universal.py` (4 tests) | not archived as events |
| Weather | **PARTIAL** | `weather/<day>.jsonl` Open-Meteo kickoff-hour forecast (temp, precip, wind, humidity, WMO code) | 1,017 rows / 189 ESPN events | 2 h, every revision kept | `test_open_meteo.py` | no `fixture_id`; `STATUS.json` health DEGRADED (geocode failures) |
| Venue / park effects | **UNAVAILABLE** | venue name/city/country only inside weather rows; `venue_id` null on fixtures | — | — | — | — |
| Calibration data | **VERIFIED** | `evaluation/model_health.v1.json` (219 cells: log loss, Brier, ECE, 80 % coverage, CLV, market LL), `uncertainty_coverage.v1.json` (238 cells), `SUMMARY.md` | n up to 2,490 per cell | daily | `test_archive_settlement_eval.py::test_metrics_basic/_interval_calibration`, `test_uncertainty_v2.py` | 95 % of mass is the unvalidated intl pool |
| Historical accuracy / postmortems | **VERIFIED** | `settlements/<day>/predictions.jsonl` (outcome, fair mean/low/high, entry/close prices, close_v2, CLV) | 10,874 rows, 59 fixtures, 5 days | daily + chain | `test_settlement_universal.py` (21), `test_settle_loop.py` | per-record join to prediction via `prediction_record_id` |
| CLV | **PARTIAL** | `clv_yes_points` etc. non-null on 7,872 / 10,874; `close_v2.kalshi` KALSHI_CLOSE share 86 %; reference (Pinnacle) TRUE_CLOSE 3.95 % | — | daily | `test_reference_clv.py` | sharp reference almost never present |
| Historical wager outcomes | **UNAVAILABLE** | `accounting-data` ledgers empty | 0 | — | `test_routed_accounting.py` | — |
| Identity tables | **VERIFIED** | §3 | 622 teams, 486 ESPN ids | manual | `test_identity.py` (13) | no player ids |
| Schedules | **VERIFIED** | `fixtures/espn/<day>/*.json` (235 fixtures, 68 leagues), `dispatch/schedule.json`, openfootball at run time | 72 h ahead | 2 h | `test_espn_provider.py` | — |
| Seasons / competitions | — | 2026-27 (Europe), 2026 (Americas); 38 registered comps; priced this week: 11 | — | — | — | — |

---

## 5. Detailed findings per category

### 5.1 Team strength: source, formula, storage
* **Live club path** (`src/soccer_edge/run/inputs.py::assemble`): historical results for E0/E1/SP1/D1/I1/F1 are downloaded at run time from the xgabora *Club-Football-Match-Data* GitHub CSV (`providers/club_football_data.py`, 238k matches, Bet365 pre-match odds + ClubElo pre-match, shots/corners parsed at lines 155-160) for `seasons_back=2` (+60 d), merged with current-season openfootball results (`providers/openfootball.py`, 9 leagues in `COMPETITION_FILES`). **These are cached in `data/cache/` (gitignored) and never committed.** The only committed club results are the ESPN archives for MLS/Liga MX/Brasileirão/Argentina.
* **Live international/Americas path**: `EspnArchive.results()` reads `results/espn/<league>.jsonl`; `ESPN_POOLS` (`providers/espn.py:769-779`) pools `uefa.nations, fifa.friendly, concacaf.nations.league, fifa.worldq.*, uefa.euroq` into one fit for every international competition (`INTL_POOL_COMPETITIONS`, `run/pipeline.py:61-72`).
* **Fit**: `fit_competition` (`run/modeling.py:48-71`): rows with `date < as_of` and age ≤ 730 d → `DixonColesFitter(config).fit(rows, as_of, teams, strict_point_in_time=True)`. Competitions with < 50 results are skipped (`inputs.py` "insufficient results to fit"). Production config is v1 defaults (`strength.py:40-55`): decay 0.0065/day, prior sd 0.35, new-team prior mean −0.15 / sd 0.45 below 8 effective matches, home-advantage prior N(0.25, 0.15²), ρ prior sd 0.08. Every ledger row says `model_version: dc_laplace_v1` (15,557/15,557) even though `dc_laplace_v2` (intercept + hard sum-to-zero + neutral-site switch, `strength_v2.py`) passed its holdout (`data/research/dc_v2_holdout.json`).
* **Storage**: the posterior is hashed (`parameter_hash`) into every prediction and into `model_board.inputs`; `grep attack|defence runs/` → nothing. `expected_goals`, `team_summary` are computed but never written. The board's per-fixture `summary` (mean_home_goals, mean_away_goals, p_home/draw/away, p_btts, p_over_2_5) is the closest persisted "team-level" output and exists only for the current 33 fixtures.
* **xG / xGA**: no production xG. `football_data_couk.results_with_xg` (`football_data_couk.py:188`) ingests `HxG/AxG` for 2026-27 files; `model/xg_family.py` blends 0.7·xG + 0.3·goals; status NOT wired (`docs/XG_DATA_AUDIT.md`). Historical xG (FiveThirtyEight archive) lives only in `data/research/xg_history_v1.csv.gz`.
* **Competition strength**: `model/multi_league.py` (RESEARCH_ONLY) estimates per-league offsets; frozen values in `data/research/multi_league_v1.json["league_offsets"]` (e.g. core fit 2026-08-28: E0 +0.218, D1 +0.095, D2 −0.135, with sds) — usable as a static table for research display. `model/intl_hier.py` (confederation offsets + Elo slope) is RESEARCH_ONLY and failed 3/8 acceptance criteria (`docs/INTERNATIONAL_MODEL.md`).

### 5.2 Results / match history
* `results/espn/*.jsonl` schema `espn_result_v1`: `fixture_id, competition_id, season_id, match_date, home/away_team_id, home/away_goals, *_ht, *_shots, *_shots_on_target, *_corners, *_yellow, *_red, *_xg, neutral_site, decided_on_penalties, extra_time_played, espn_event_id, league` (+ on 161 upgraded rows: `kickoff_utc, status_name, *_goals_regulation, *_goals_et, *_shootout, winner_after_penalties, first_scorer_team, goal_events_source, result_source`). Measured non-null: goals 100 %, HT 3.0 %, shots/SoT/corners/cards/xG **0 %**, kickoff_utc 3.3 %, first scorer 2.8 %.
* By competition: usa.mls 1,187 · arg.primera 1,142 · mex.liga_mx 701 · bra.serie_a 658 · fifa.friendly 346 · fifa.world_cup_qualifiers 334 · uefa.nations_league 284 · concacaf.nations_league 165 · usa.nwsl 18 · eng.wsl 14. Seasons: 2024 975, 2025 1,665, 2026 1,412, 2024-25 356, 2025-26 204, 2026-27 173, 2023-24 16, `2026-wo` 48 (NWSL/WSL season id oddity).
* `data/international/results_v1.csv.gz` (on main, CC0, sha-pinned `MANIFEST.json`): 48,249 rows 1872-11-30 → 2026-08-26, 231 teams, columns `date, home_team, away_team, home_id, away_id, goals, tournament, competitive, neutral, home/away_conf, city, country, home/away_elo_pre`; 6,080 rows since 2020. Point-in-time Elo tested (`test_international.py::test_point_in_time_elo_uses_only_earlier_days`).

### 5.3 Lineups and players
* `lineups/history/<league>.jsonl` (`espn_lineup_snapshot_v1`, `backfill: true`, `lineup_state: post_hoc`): 3,717 matches (eng.1 809, esp.1 829, fra.1 657, ger.1 612, ita.1 810), 2024-08-15 → 2026-09-20, 5,067 distinct `athlete_id`, 40,886 starter rows; per player `athlete_id, name, position (G, CD-L, RM, F, SUB…), jersey, starter, formation_place, subbed_in, subbed_out, active`; per side `formation` (4-2-3-1 36 %). `BACKFILL_STATUS.json`: ger.1 38 fetch failures; budget 247 left.
* Daily prospective snapshots: 847 rows, 234 events, event_state pre 593 / in 143 / post 111, lineup_state unconfirmed 585 / post_hoc 195 / confirmed 67. `evaluation/lineup_lead_time.v1.json`: 170 fixtures tracked, 82 kicked off, 41 pre-kickoff XI (50 %), 36 ≥ 20 min (43.9 %), median lead 37 min, p10 12 min — below the 90 % audit target.
* No minutes, goals, assists, ratings, injuries, or a player registry. The oracle study (`data/research/lineup_oracle_v1.json`) found no detectable 1X2 gain from perfect XI knowledge (−0.0009 [−0.0048, +0.0035]) and applied the stop rule.
* `lineups/history` events have **zero overlap** with `results/espn` (top-5 results are not archived); the oracle joined 2,648/3,717 to the runtime football-data CSV.

### 5.4 Market data
* Snapshot row: `ticker, event_ticker, series_ticker, captured_at, batch_id, discovery_run_id, status, yes/no_bid/ask (+sizes), last_price, volume, open_interest, orderbook (null on 321,621/321,621), close_time, expected_expiration_time, fee_type, fee_multiplier, rules_primary_hash, minutes_to_kickoff, horizon, price_unit`.
* Catalog (`data/catalog/latest_index.json`, 2026-09-27): 6,530 markets, 1,523 series; `snapshots/STATUS.json` 2026-10-03: 6,664 contracts across ~70 competition codes (UEFANL 1,292, EPL 385, UCL 354, CONCACAFNL 312, INTLFRIENDLY 303, …).
* Series with most quote rows: KXUEFANLSCORE 40,293, KXUEFANLTOTAL 25,388, KXUEFANLGAME 16,980, KXUEFANLSPREAD 14,034, KXEPLTEAMPOINTS 5,088 (season futures are captured too).
* Reference odds: 112 Pinnacle rows (`reference/<day>/oddsapi-*.jsonl`, markets 1x2 48 / ah 32 / ou 32, 9 fixtures, `devig_method: power`, `source_quality: SHARP_REFERENCE`); the football-data `fixtures.csv` capture wrote 0 rows (all 46 rows skipped: unmapped divisions E2/E3/EC/SC1-3/SP2). `odds_api/STATUS.json`: last batch `NOTHING_ELIGIBLE`, `no_sport_key: concacaf.nations_league`.

### 5.5 Predictions and settlements
* Prediction row (`prediction_record_v1`, ~3.3 KB): `record_id, run_id, as_of, data_as_of, model_as_of, market_as_of, fixture_id, competition_id, kickoff_utc, ticker, event_ticker, family, semantics{side, line, period, k, player_slot, description}, probability{mean, median, low, high, parameter_sd, n_worlds, n_draws, mc_standard_error, interval_level}, market{yes/no bid/ask + sizes, status}, reference{kalshi_mid_yes, bookmaker, probability_yes}, reference_quality, edge{yes,no}(edge_v1), edge_v2{yes,no}, expression, recommendation, context{rest/congestion}, lineup_state, model_family, model_version, engine_version, worlds_version, parameter_hash, world_hash, sim_seed, fee_regime, selection_policy, discovery_run_id, schema`.
* Per day rows: 09-27 117 · 09-28 2,148 · 09-29 2,359 · 10-01 2,863 · 10-02 8,019 · 10-03 51. Families: exact_score 5,730, total_goals 2,244, handicap 1,496, team_total 1,218, match_result_3way 1,214, first_half_total 918, first_half_result 918, first_half_handicap 612, btts/first_half_btts (10-03 only).
* Settlement row (`settlement_record_v1`, ~2.5 KB): `record_id, prediction_record_id, fixture_id, ticker, family, model_family, horizon, outcome (yes/no), fair_probability_{mean,low,high}, entry_yes_ask/entry_no_ask/entry_yes_mid, close_yes_mid, close_class, close_minutes_before_kickoff, close_v2{entry, kalshi, reference, reference_attempt, complete}, clv{yes,no}{price, probability, fee_aware points}, clv_yes_points, clv_model_signed_points, reference_* fields, recommended{yes,no}, evidence{home, away, period, status, source}, settled_at`.
* `evaluation/settlement_coverage.v1.json`: 10,933 predictions due/known, 6,624 settled (97.4 % of due), 4,133 pending kickoff, 176 pending evidence, 0 unaccounted. `evaluation/settlements_written.v1.json` 1,520 rows (latest run).
* Health (`SUMMARY.md`): e.g. `world_sim_v2.intl_pool | match_result_3way | any`: n 528, log loss 0.600 vs market 0.602, ECE 0.067, 80 % coverage 0.962, CLV −0.0015; every cell RESEARCH_ONLY; 16 SHADOW proposals (exact_score cells) pending human review.

### 5.6 Context and weather
* Rest/congestion (`run/context_features.py`): `rest_days_home/away, matches_14d_*, matches_28d_*, rest_gap_days`, point-in-time, on 15,440 ledger rows. Research verdict (`data/research/context_features_v1.json`): congestion ≈ −0.001 log loss, rest nothing.
* Weather schema `weather_snapshot_v1`: `espn_event_id, league, kickoff_utc, captured_at, hours_before_kickoff, venue, venue_city, venue_country, latitude, longitude, temperature_2m, precipitation, precipitation_probability, wind_speed_10m, wind_gusts_10m, relative_humidity_2m, weather_code`; 1,017 rows / 189 events; every forecast revision kept.

### 5.7 App export today (`src/soccer_edge/app_export.py`)
Reads (constants lines 60-70): `runs/latest.actionable_slate.v1.json` (required), `runs/latest.run_output.v1.json`, `runs/latest.model_board.v1.json` (only `p, p_low, p_high, period, line, n_worlds, interval_level, param_sd` — **ignores `q[100]` and `summary`**), `STATUS.json`, `snapshots/STATUS.json`, newest `dispatch/heartbeats/*.jsonl`, `fixtures/espn/<7 days>` (for `espn_event_id`), `config/authority.json`, optional `accounting-data` ledgers. **Ignores**: `predictions/`, `settlements/`, `snapshots/<day>/`, `results/espn/`, `lineups/`, `weather/`, `reference/`, `evaluation/*` (model_health is read but exported only as a note), `runs/<day>/<run>/`, `data/international`, `data/research`. Output: 43 events, 1,203 markets, 1,203 model prices, 1 recommendation, 2 theses, 0 wagers; `event_detail.price_history = []` for every event although the contract defines it (`contract/edge_finder_contract/schema_defs.py:412`).

---

## 6. Opponent-adjustment audit

* **Formula (production, `dc_laplace_v1`, `model/strength.py`):** `λ_home = exp(attack_h − defence_a + γ)`, `μ_away = exp(attack_a − defence_h)`, joint `P(x,y) = τ(x,y|λ,μ,ρ)·Pois(x|λ)·Pois(y|μ)` with the Dixon-Coles low-score correction τ. Solved as a penalised likelihood (MAP, L-BFGS-B) over all teams simultaneously → **recursive/simultaneous** opponent adjustment (each team's rating is conditional on every opponent's rating), not a simple "vs average" adjustment. Laplace covariance = inverse observed information → per-team sds.
* **v2 (`strength_v2.py`, not in production):** adds intercept κ ~ N(log mean away goals, 0.3²), hard Σa = Σd = 0 (fit in the (n−1)-dim centred space), γ switched off on neutral rows, γ ~ N(0.20, 0.10²). Reason: v1 under-predicts away goals by ≈ 11 % (`docs/MODEL_FAMILIES.md`).
* **Baseline:** v1 — team priors centred at 0 (new teams −0.15); v2 — league average via κ, teams sum to zero. Home advantage: one γ per competition fit (no team-specific term, `KNOWN_LIMITATIONS` #11).
* **Weighting / samples:** recency `e^(−0.0065·days)` (half-life ≈ 107 d); lookback 730 d; competition skipped below 50 results; team treated as "new" below 8 effective (decayed) matches; `MatchRow.weight` exists for competition weighting but is 1.0 everywhere in production.
* **Point-in-time:** `assert_no_future_dates` + `strict_point_in_time=True`; `temporal_guard_for_inputs` (`run/pipeline.py:965`) records `latest_observed_at` per input in every run output.
* **History:** not preserved — refit from scratch every run; only `parameter_hash`, `model_fitted_at`, `results_used`-equivalent (`model_board.inputs`) survive. To expose rating trends the research layer must refit per day from `results/espn` (feasible: ~1–3 s per fit) — but **club top-5 cannot be refit from committed data** (history lives in the runtime cache).
* **Tests:** `tests/test_simulation.py::test_fit_recovers_structure`, `::test_point_in_time_fit_ignores_future`, `::test_sparse_team_has_wider_posterior`; `tests/test_model_v2.py` (levels, centring, neutral, intercept LR, config); `tests/test_multi_league.py` (5); `tests/test_international.py::test_intl_hier_*` (3); `tests/test_temporal_guard.py`.
* **Limitations:** international pool uses club priors/decay for ~200 nations pooled (San Marino 25 % vs Albania, `KNOWN_LIMITATIONS` #26); friendlies full weight; game-state/red-card multipliers are configured priors; worlds independent across fixtures; walk-forward shows the model is 0.028 log loss worse than Bet365 (`docs/RESEARCH_RESULTS.md`).

---

## 7. Inventories

### 7.1 Time-series inventory

| Dataset | x-axis | Keys | Rows | Links to game id? | Links to opponent? |
|---|---|---|---|---|---|
| `snapshots/<day>/cap-*.jsonl` | `captured_at` (≈ 30-min batches; 177 batches over 7 d; 78 on 10-02) | `ticker` (→ `event_ticker`) | 321,621; 11,868 tickers; median 16 obs/ticker, p90 70, max 173 | only via prediction/slate rows (ticker → `fixture_id`); 2,341 event tickers | via fixture id |
| `predictions/<day>` | `as_of` per run (`run_id`) | `fixture_id × ticker` | 15,557; 3,814 pairs; median 4 points/pair, max 10; 134 fixtures, median 2 runs/fixture (max 10) | yes (`fixture_id`) | yes (parse id) |
| `settlements/<day>` | `horizon` label (T-24h … close) per prediction | `prediction_record_id` | 10,874 | yes | yes |
| `results/espn/*.jsonl` | `match_date` | `fixture_id`, `espn_event_id` | 4,849 (dedupe by espn id → 4,764) | yes | yes |
| `lineups/history` | `kickoff_utc` | `espn_event_id`, `athlete_id` | 3,717 matches × ~40 players | ESPN id only | via `home_espn_id/away_espn_id` → `espn_map` |
| `lineups/<day>` | `captured_at` | `espn_event_id` | 847 | ESPN id | as above |
| `weather/<day>` | `captured_at` (forecast revisions) | `espn_event_id` | 1,017 / 189 events | ESPN id only | no |
| `dispatch/horizons.jsonl` | horizon delivery per fixture | `fixture_id × horizon` | 345 | yes | — |
| `evaluation/model_health.v1.json` | replaced daily (no series kept) | model×family×horizon | 219 | — | — |
| `data/international/results_v1.csv.gz` | `date` 1872→2026 | `intl:<team>` | 48,249 | no fixture id | yes (names/ids) |

**Caveat on snapshot horizon fields:** 321,520 / 321,621 rows have `minutes_to_kickoff ≥ 1440` and `horizon = T-24h` although `dispatch/horizons.jsonl` proves captures at T-15/T-5 (`achieved_minutes_median` 12.4 for horizon 15). The field is computed from the market's `close_time`/expiration, not the fixture kickoff. Any market time series must recompute time-to-kickoff from `kickoff_utc` on the prediction row or ESPN fixture (the settlement engine already does this; `KNOWN_LIMITATIONS` #25 documents the earlier T-10m mislabel).

### 7.2 Splits inventory
Stored split dimensions: home/away (inherent in every row), `neutral_site` (16 ESPN rows true; intl CSV flag), `competitive` vs friendly (intl CSV), `period` on semantics (regulation / first_half / second_half / including_et / including_pens), `horizon` buckets on settlements (10 labels), `model_family` (4), `competition_id` (11 priced), `family` (10 priced). No handedness/surface/game-state tables. Counts above.

### 7.3 Market-history inventory
Per-ticker quote series **exist**: observations = rows in `snapshots/`, change-suppressed (a row only when the quote changed), granularity ≈ 30 min during the chain (15-min slate refresh re-captures), 2-hourly Tue–Thu outside kickoff windows; retention forever (gzipped monthly, manifest-hashed); size 222.5 MB raw for 7 days (≈ 30 MB/day raw at this week's international-break volume; `docs/ARCHIVE_COMPACTION.md` projects 10–15 MB/day). Fields: bid/ask both sides with sizes, last price, volume, open interest; no order-book depth.

### 7.4 Projection inventory
* Per entity: **per contract (ticker)**, not per team/player; per fixture only the board `summary` (5 scalars).
* Distribution vs point: ledger stores mean/median/80 % interval + parameter sd; current board stores 100 quantiles per contract (`quantile_levels (i+0.5)/100`); realisations never stored.
* Across runs: yes for the ledger (append-only, 43 runs); no for the board/quantiles (overwritten).
* Engines: `world_sim_v2` analytic DC matrix for full-time families (11,707 rows), `minute_engine_v1` simulation (3,850 rows); `n_worlds` 1000, `draws_per_world` 100.

---

## 8. Existing ranking / percentile / league-average code

* Quantiles: `slate/board.py` / `run/simcache.py` write `q[100]` per contract; `evaluation/metrics.py::reliability` (10 bins), `expected_calibration_error`, `interval_calibration`, `bootstrap_mean_ci`, `clv_points`, `sharpness`.
* League-level averages: `model/multi_league.py` league offsets (+ `offset_difference` with sd), `intl_hier.py` confederation offsets and standardised Elo (`team_elo_std`, `elo_mean/elo_sd`); `strength_v2` `weighted_away_level` diagnostic; `research/home_bias.py` H1–H8 (home-advantage heterogeneity, promoted priors, shrinkage).
* Team summaries: `ParameterPosterior.team_summary()` (`strength.py:107`), `MultiLeaguePosterior` equivalent (`multi_league.py:197`) — attack/defence mean+sd, effective matches. Not called by any writer.
* Microstructure percentiles: `evaluation/microstructure_latest.json` (fee impact p10/p90, spread gaps by competition/family/horizon).
* No team rankings, percentiles vs league, or power ratings exist anywhere in code.

---

## 9. Size estimates (research layer, soccer)

| Surface | Basis | Estimate |
|---|---|---|
| (a) Team profiles | 622 registered teams × (id, name, aliases, country, kind, ESPN id, provider ids ≈ 300 B) + per-team W/D/L/GF/GA from `results/espn` (≈ 9.7k team-match rows × 120 B) + refit ratings for the 11 priced competitions (≈ 300 teams × 6 floats) | ≈ 0.2 MB static + 1.2 MB results summaries + 0.05 MB ratings ⇒ **~1.5 MB total, ~2–5 KB per team page** |
| (b) Player profiles | 5,067 + 3,787 ESPN athletes (overlap unknown; ≈ 8k) × (id, name, team, position, appearances, starts, formation places ≈ 200 B) | **~1.6 MB**; appearance logs 3,717 matches × ~44 players × 60 B ≈ **9 MB compact** (30 MB raw) |
| (c) Per-game detail | board entry ≈ 95 KB/fixture with 100 quantiles (3.1 MB / 33), ≈ 10 KB without; ESPN result ≈ 650 B; lineups ≈ 8 KB; weather ≈ 5 revisions × 450 B | **~15–100 KB per fixture page** |
| (d) Metric time series (projections) | 15,557 ledger rows ≈ 3.3 KB raw; compact tuple (fixture, ticker, as_of, p, lo, hi, yes_ask) ≈ 120 B | **~1.9 MB for 7 days compact**; grows ≈ 8.5 MB/day raw, ≈ 0.3 MB/day compact |
| (e) Market history | 321,621 rows / 222 MB raw; compact (ticker, ts, yes_bid, yes_ask, last, volume, oi) ≈ 80 B | **~26 MB for 7 days compact** (≈ 2.2 KB per ticker); must be sharded per event/ticker for lazy loading (≈ 11 KB per event ticker) |

---

## 10. Recommendations for this pass

**Expose (evidence supports it):**
1. **Market price history per ticker/event** from `snapshots/` — VERIFIED, deepest dataset; recompute time-to-kickoff from fixture kickoff; show bid/ask/last/volume/OI with change-suppression semantics explained.
2. **Projection history per fixture×ticker** from `predictions/` (mean + 80 % interval vs Kalshi ask at each run) — VERIFIED; label `model_family` (club vs `.intl_pool`) and `RESEARCH_ONLY`.
3. **Settled outcomes with CLV and calibration** from `settlements/` + `evaluation/model_health.v1.json` — VERIFIED; present per competition/family/horizon with n, mark intl pool as unvalidated.
4. **Game logs and head-to-head** for MLS, Liga MX, Brasileirão, Argentina, Nations Leagues, WCQ, friendlies from `results/espn` (goals only; dedupe by `espn_event_id`, handle Liga MX fixture-id collisions) — PARTIAL (2 seasons, no shots/xG).
5. **National-team history and Elo** from `data/international/results_v1.csv.gz` — PARTIAL (needs an `intl:` → `nat.` map, ~231 rows, trivially derivable from names in the registry).
6. **Lineup sheets / formations / appearance counts** for top-5 (post-hoc, 2 seasons) and prospective XIs with lead time — PARTIAL; clearly labelled "no minutes, no stats".
7. **Rest/congestion context** per fixture from ledger `context` — PARTIAL (research says ≈ no signal).
8. **Current board distributions** (100 quantiles) for live fixtures — PARTIAL (latest only).

**Mark RESEARCH:** xG (FiveThirtyEight 2016–2023 only; no live xG), league-strength offsets (`multi_league_v1`), international hierarchical ratings (`intl_hier_v1`, failed holdout gates), lineup oracle, home-bias and context-feature studies, walk-forward benchmark tables. All frozen JSON under `data/research/` with result hashes.

**Mark UNAVAILABLE:** player metrics/usage/props, injuries, team ratings time series (not persisted; top-5 club history not committed), schedule strength, matchup metrics, venue effects, play-by-play, wager outcomes (ledger empty), sharp reference odds (112 rows; 4 % true-close share).

**Mark PARTIAL with explicit caveat:** team "ratings" — if shown, must be recomputed by the research export (refit `dc_laplace_v2` on `results/espn` per competition; feasible only for ESPN leagues) and dated; never claim they are the production posterior.

---

## 11. Open questions / UNKNOWN

1. **Archive history depth beyond the tip**: fetched at depth 1 (the branch reported a "forced update" relative to the stale local ref, expected for shallow grafts). Whether any day file was ever rewritten could only be checked via `manifest/` verification (`soccer archive verify`), which CI runs on every push — not re-run here.
2. **Runtime club history** (`data/cache/Matches.csv`, football-data CSV): size/columns verified from provider code (`club_football_data.py`) and docs (238,858 matches, 2000→2026-09-03), not from a committed file. A research layer wanting top-5 game logs must either fetch it in the export workflow or persist a trimmed copy.
3. **Overlap between `lineups/history` athletes and daily snapshot athletes** not computed (ids are ESPN-stable, so union ≈ 8k is an upper bound).
4. **`minutes_to_kickoff` semantics in snapshots**: inferred from the data (99.97 % ≥ 1440) and `KNOWN_LIMITATIONS` #25; the exact computation in `kalshi/capture.py` was not read line by line.
5. **Odds API key presence**: `.env.example` lists `ODDS_API_KEY`, `FOOTBALL_DATA_ORG_TOKEN` (names only; no values in repo). 17 raw Odds API responses exist, so a key was configured in Actions at least on 09-29 → 10-03.
6. **Women's competitions** (NWSL, WSL) have results (32 rows) and lineup files but no model runs this week; season id `2026-wo` is undocumented.
7. **`fixtures/<day>/espn-*.json` per-batch dumps** (105 files on 10-02, most with 0 fixtures) appear to be dispatcher side-effects; their retention value is unclear.
8. Tests: 414 test functions in 50 files run in CI (`ci.yml`, py3.11/3.12 + ruff + schema sync + workflow lint + archive verify); `tests/fixtures/app_export/` (116 KB) pins the export end-to-end and `test_real_data_smoke`.
