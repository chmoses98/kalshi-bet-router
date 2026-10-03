# Phase 2 audit — `chmoses98/kalshi-bet-router` (the router, not a sport)

Audited at `main` = `45f610bf452a1f7c431fda6fcb62a3f40ebc1bb6` (2026-10-02 19:21 -0400), read-only, on 2026-10-03.
Scratch dir: `(local scratch, not committed)`.
Every quote below is from HEAD (the HEAD `MANIFEST.json` sha256s were re-verified against `git show HEAD:` blobs: 42/42 match).

> **Working-tree drift observed during the audit (not on `main`).** While this audit ran, the checkout acquired
> uncommitted changes under `contract/edge_finder_contract/`: `__init__.py` (CONTRACT_VERSION 1.0.0 → 1.1.0, 12 new KINDS,
> `CAPABILITIES`, `QUALITY_STATUSES`), `ids.py` (+`metric_id`, `observation_id`, `ranking_id`, `series_id`, `packet_id`,
> `tray_item_id`), `schema_defs.py` (+280 lines: QUALITY, LINK, OBSERVATION, METRIC, RANKING, TIME_SERIES, ENTITY_PROFILE,
> EVENT_RESEARCH, MARKET_HISTORY, CAPABILITY_MANIFEST, SEARCH_INDEX, EXPLORER_INDEX, HANDICAP_PROTOCOL/PACKET,
> RESEARCH_TRAY), 19 new `schemas/*.schema.json`, new `research.py` (836 lines), `packet.py` (517), `protocols/*.v1.json` (8),
> `tests/contract/research_fixtures.py`. `python -m edge_finder_contract.sync --check` on the working tree reports **33 problems**
> and `tests/contract/test_contract_v1.py::test_vendored_manifest_matches_the_package` **fails on the working tree** (it passes
> on HEAD). This is somebody's in-progress research-graph layer, i.e. the very thing this audit is meant to inform. Everything in
> this report describes `main`; §11 lists what the drift already assumes so the two can be reconciled.

---

## 1. Summary

1. This repository is **infrastructure, not data**: it authors the `edge_finder.app.v1` contract (`contract/edge_finder_contract`,
   42 files, 764 KB, zero third-party deps) and routes Kalshi fills to seven sport repos. It **persists no wager, price, balance or
   sport statistic anywhere** (README §3; `publish_bankroll_secret.py`, `tests/test_privacy.py`, `tests/test_accounting_privacy.py`).
2. The only data the router publishes is the orphan `app-data` branch: `router_health.json` (4.4 KB), `recent_deliveries.json`
   (6.7 KB, rolling 200) and `sports_registry.json` (2.9 KB), **force-pushed as one commit every 15 min** — no history survives.
3. The strongest cross-sport research asset here is the **contract itself**: deterministic ids (`ids.py`), the per-object shapes
   (`schema_defs.py`), the FRESHNESS / health / authority vocabulary, `linkage.py` (temporal, never retroactive), `performance.py`
   (never fabricates money) and `publish.py` (atomic last-known-good). A new additive layer must reuse these verbatim.
4. The second asset is **`docs/fixtures/edge_finder_app_fixture.v1.json`** (13.68 MB pretty / 10.33 MB compact): real exports of all
   seven sports on 2026-10-02 trimmed to ≤3 events each. It is the only place in this repo where sport data can be measured, and it
   shows that **`price_history` is empty in every sport (21/21 event_detail files)**, `projection_value` is null in 1,099/1,099
   model prices, and `wager.recommendation_id` is null in 11/11 wagers.
5. Wager/settlement **history lives in the destination repos' ledger branches**, not here. The router only emits payload rows
   (`production.to_*_import_row`) with 13 accounting fields and refuses model provenance by construction (`routed_ledger.PROVENANCE_FIELDS`).
6. Market → entity linking assets: `series_registry.py` (348 exact series tickers → sport; 253 SOCCER, 60 NBA, 20 NHL verified; MLB/NFL/CFB/TENNIS
   entries unverified), `competitions.py` (Kalshi competition strings → sport), `taxonomy.py` and `milestones.py` (live, in-memory,
   never persisted). None maps a ticker to a **team/player id**; that mapping is done per sport and surfaces only as
   `market.participant_id` / `market.player_id` / `market.extensions`.
7. **CLV / accuracy data: UNAVAILABLE in the router.** No module computes CLV (`grep -rn clv src/ scripts/` → nothing). The contract carries
   `performance.clv {available, wagers_with_clv, mean_clv}` and MLB/NFL exports fill it (135 and 66 wagers with CLV in the fixture).
8. Tests: 62 files, ~1,478 `def test_` (38 in `tests/contract/`), pytest `pythonpath = ["src","contract"]`, **no ruff/flake8/mypy**,
   CI matrix Python 3.11 + 3.12, ~2.5–3.5 min per job (6 recent runs: 2m30s–3m21s). Locally this sandbox is far slower (15 s wall for the
   38 contract tests, >11 min for the full suite) — wall-clock here is not representative.
9. **No size/performance constraint exists in code or tests.** The only size-shaped conventions are `board.TOP_N = 3`,
   `performance.RECENT_N = 50`, `publish_router_health.RECENT_LIMIT = 200`, and compact JSON (`publish.dumps(compact=True)`).
10. Weakest areas for the research graph: no market price time series anywhere in the contract output today; no entity (team/player)
    documents outside `event.participants`; no cross-source id map; the router's `recent_deliveries` window is the only time series it owns.

---

## 2. Data branches and layout

| Branch | Kind | Root | Content | Mutability | Size |
|---|---|---|---|---|---|
| `main` | code | `/` | `contract/`, `src/kalshi_router/`, `scripts/`, `tests/`, `docs/`, 14 workflows | normal | fixture 13.7 MB dominates |
| `app-data` | **data (router's only one)** | `app/latest/` | `router_health.json` 4,417 B; `recent_deliveries.json` 6,736 B; `sports_registry.json` 2,893 B; `README.md` 178 B | **orphan branch re-created and `git push --force` every run** (`.github/workflows/publish-router-health.yml:60-77`); `git rev-list --count origin/app-data` = 1 after `--depth=5` | 14 KB |
| `backfill-engine`, `backfill-inspect`, `backfill-series-verification` | code snapshots (older `main` states) | `/` | source + workflows only, **no data files** | stale | — |
| `claude/*` (≈40) | feature branches | — | code | — | — |

`app-data` mechanics (`publish-router-health.yml`): every 15 min (`cron: "*/15 * * * *"`, main only, `contents: write`, `actions: read`,
**no Kalshi credential, no downstream token**) → fetch previous `recent_deliveries.json` (rolling window) → `scripts/publish_router_health.py`
reads this repo's own public Actions logs via `gh` (`HEALTH=`, `ROUTER_STATUS_JSON=`, `ROUTER_DELIVERY_JSON=`, `::error::<SPORT>:` lines),
scrubs tickers/keys/amounts (`scrub()`, `publish_router_health.py:49-54`) → `python -m edge_finder_contract validate` → orphan commit → force push.
Consequence: **the only historical router record is GitHub Actions run logs (default 90-day retention) and the 200-item rolling window.**

Downstream ledger branches (where wager history actually lives; `src/kalshi_router/destinations.py:191-` and `docs/DESTINATIONS.md`):

| Sport | Repo | Ledger branch | Layout | Economics |
|---|---|---|---|---|
| MLB | `chmoses98/edge-finder-api` | `main` | `data/edgelab/bets/bets.jsonl` (jsonl) — settles itself from MLB Stats API | v1 |
| CFB | `chmoses98/cfb-edge-finder` | `accounting-data` (orphan) | `wagers/<season>.jsonl`, `settlements/<season>.jsonl`, `settlement_amendments/<season>.jsonl` | v2 |
| NFL | `chmoses98/nfl-edge-finder` | `handicap-data` | `data/imported_wagers/<season>/week_<NN>/<id>.json` (`record_layout="json_per_file"`) | v2 |
| NHL | `chmoses98/NHL-edge-finder` | `accounting-data` | `data/accounting/wagers.jsonl`, `settlements.jsonl` | v2 |
| NBA / SOCCER / TENNIS | respective repos | `accounting-data` | same as NHL, via vendored `routed_ledger.py` | v2 |

---

## 3. Identity model (`contract/edge_finder_contract/ids.py`, 127 lines)

**Hashing scheme** (`ids.py:19,44-53`):

```python
_DOMAIN = "edge_finder.app.v1/ids"
def digest(*parts, length=20):
    material = _DOMAIN + "|" + SCHEMA_VERSION + "|"
    for part in parts:
        text = "" if part is None else str(part)
        material += f"{len(text.encode('utf-8'))}:{text}"      # length-prefixed, domain separated
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:length]
def make_id(prefix, *parts): return f"{prefix}_{digest(*parts)}"
```
Every id is `<prefix>_<20 hex>` (24 chars total); schema pattern `^[a-z]{2,4}_[A-Za-z0-9._-]{4,}$` (`schema_defs.py:119`). `None` encodes as the
empty string. Test: `test_length_prefixed_digest_cannot_be_forged_with_separators` (`tests/contract/test_contract_v1.py:88`).

**Constructors** (all signatures exact):

| function | signature | parts hashed | notes |
|---|---|---|---|
| `normalize_sport(value) -> str` | any spelling → one of `SPORTS = ("MLB","CFB","NFL","NBA","NHL","SOCCER","TENNIS")` via `SPORT_ALIASES`; raises `ValueError` | — | `ids.py:22-41` |
| `event_id(sport, source, source_id)` | → `evt_…` | `("event", sport, source, source_id)` | `source` = provider namespace (`mlb_game_pk`, `espn_event_id`, `nflverse_game_id`, `nhl_game_id`, `kalshi_milestone_id`, `fixture_id`, `kalshi_event_ticker`) |
| `participant_id(sport, participant_type, source, source_id)` | → `prt_…` | `("participant", sport, type, source, source_id)` | type ∈ `TEAM`,`PLAYER`,`PAIR` |
| `market_id(kalshi_ticker)` | → `mkt_kalshi_<TICKER>` | **no digest**; ticker upper-cased, must match `^[A-Z0-9][A-Z0-9._-]*$` | `ticker_from_market_id()` inverts |
| `model_price_id(run_id, market_id_value, model_version=None)` | → `mp_…` | `("model_price", run_id, market_id, model_version)` | |
| `thesis_id(sport, run_id, event_id_value, scope="event")` | → `ths_…` | `("thesis", sport, run_id, event_id, scope)` | |
| `recommendation_id(sport, source_repo, native_id=None, *, run_id=None, market_id_value=None, selection=None)` | → `rec_…` | native: `("recommendation", sport, repo, native_id)`; else `(…, repo, run_id, market_id, selection)` | |
| `wager_id(sport, source_bet_key=None, *, source_repo=None, native_id=None)` | → `wgr_…` | `("wager","source_bet_key", key)` — **sport-independent**; fallback `("wager", sport, repo, native_id)` | pinned by `test_ids_are_deterministic_and_distinct` |
| `settlement_id(wager_id_value)` | → `stl_…` | `("settlement", wager_id)` | one wager settles once |
| `run_id(sport, repo, native_run_id=None, *, generated_at=None)` | → `run_…` | `("run", sport, repo, native_run_id, generated_at)` | |

**`source_bet_key`** (the cross-repo wager identity): `production.production_source_key(subaccount_number, ticker, order_id)` →
`"kalshi:v1:" + sha256(length-prefixed fields, domain "kalshi-bet-router/source-key/v1")` (`src/kalshi_router/production.py:40-80`).
Destinations mint their own row ids from it: `routed_ledger.LedgerSpec.mint_wager_id` → `<prefix>w-<24 hex of sha256(key)>`
(`contract/edge_finder_contract/routed_ledger.py:86-89`); NFL mints `routed-<24hex>`, settlement `stl-<24hex>` (seen in fixture).

**Provider id namespaces actually used by the sport exports** (measured in the fixture, `event.source_ids` / `participant.source_ids`):

| Sport | event namespaces | participant namespaces | market subject fields |
|---|---|---|---|
| MLB | `mlb_game_pk`, `edgelab_game_id`, `kalshi_event_key`, `odds_api_event_id` | TEAM: `mlb_team_abbr` | `participant_id` 175/342; `player_id` 0/342 (players only as `extensions.player` text) |
| CFB | `kalshi_milestone_id`, `kalshi_game_key`, `kalshi_main_event_ticker` | TEAM: `kalshi_team_code`, `kalshi_football_team` (uuid) | `participant_id` 583/891; `player_id` 0 |
| NFL | `nflverse_game_id`, `home_team`, `away_team`, `slate_id` | TEAM: `nflverse_team` | `participant_id` 354/1499; **`player_id` 722/1499** |
| NBA | `espn_event_id`, `nba_edge_game_id`, `home_team_id`, `away_team_id`, `home_tricode`, `away_tricode` | TEAM: `nba_team_id`, `tricode` | 8/9; 0 |
| NHL | `nhl_game_id`, `kalshi_event_ticker` | TEAM: `nhl_team_id`, `nhl_abbrev` | 84/544; **`player_id` 389/544** (+ `extensions.nhl_team_id`, `kalshi_entity_uuid`) |
| SOCCER | `fixture_id`, `espn_event_id` | TEAM: `team_id` (`bra.atletico_mineiro`) | 52/151; 0 |
| TENNIS | `kalshi_event_ticker`, `match_code`, `match_key`, `physical_match_id` (`ATP:200096:212311:2026-10-02` = Sackmann ids) | PLAYER: `kalshi_match_winner_ticker`, `kalshi_player_name` (the test fixture uses `sackmann_id`, the real export does not) | 6/6; 0 |

**Collisions / caveats**: `wager_id` ignores sport by design; `event_id` differs per namespace (`espn_event_id` vs `nflverse_game_id` give
different ids for the same game — there is **no cross-source map in this repo**); `market_id` is the ticker so it is globally shared.
Series/competition identity helpers: `series_registry.lookup_series_ticker(series_ticker) -> SeriesEntry|None` (exact match, 348 entries:
MLB 4, NFL 4, CFB 3, TENNIS 4 all `verified=False`; NHL 20, NBA 60, SOCCER 253 `verified=True`), `competitions.sport_from_competition(str)`,
`competitions.sport_from_taxonomy_sport(str)`, `taxonomy.SportTaxonomy.sport_for_competition(str)`, `milestones.MilestoneIndex.competition_for_event(ticker)`.

---

## 4. CAPABILITY MATRIX (cross-sport assets the router itself provides)

| capability | status | origin | coverage | cadence | tests | notes |
|---|---|---|---|---|---|---|
| Contract: object shapes + validator | **VERIFIED** | `contract/edge_finder_contract/schema_defs.py`, `validate.py`, `schemas/*.json` (25) | 16 kinds + 9 objects | on commit (pinned) | `tests/contract/test_contract_v1.py` (30 tests) | generator ↔ disk pinned; keyword subset pinned |
| Contract: deterministic ids | **VERIFIED** | `ids.py` | 10 constructors | — | `test_ids_are_deterministic_and_distinct`, `..._cannot_be_forged…` | |
| Freshness / health vocabulary | **VERIFIED** | `freshness.py`, `health.py` | 7 component thresholds | — | `test_freshness_is_deterministic`, 5 health tests | |
| Atomic publication + manifest | **VERIFIED** | `publish.py`, `integrity.py` | — | — | `test_publish_writes_a_consistent_tree`, `..._last_known_good…`, `..._removes_event_details…` | |
| Temporal linkage | **VERIFIED** | `linkage.py` | — | — | `test_linkage_is_temporal_never_retroactive` | |
| P&L aggregation (perf) | **VERIFIED** (code) | `performance.py` | totals / family / month / source | — | `test_performance_never_fabricates_economics` | CLV + bankroll are pass-through inputs |
| Shared destination ledger | **VERIFIED** | `routed_ledger.py` | NBA/SOCCER/TENNIS | per delivery | `tests/contract/test_routed_ledger.py` (8) | append-only JSONL, provenance refused |
| Router health (`router_health`, `recent_deliveries`) | **VERIFIED** | `scripts/publish_router_health.py` → `app-data` | counts only | every 15 min | `tests/test_router_health_publisher.py` | last run 2026-10-03T02:11:25Z, `overall_status` DEGRADED |
| Sports registry | **VERIFIED** | `registry.json` → `app-data/app/latest/sports_registry.json` | 7 sports + router | copied each publish | `test_registry_lists_every_sport_and_the_router` | |
| Wager history (routed) | **PARTIAL (not here)** | emitted by `deliver-wagers.yml` (`*/15`), stored in destination ledgers | router_health 2026-10-03: delivered MLB 106, CFB 84, NFL 60, NHL 19, SOCCER 9, NBA 0, TENNIS 0 (278 total) | 15 min + conductor 20 min | `tests/test_delivery*.py`, `test_production_*`, `test_nfl_delivery.py`, `test_cfb_delivery_end_to_end.py` | row dialect: 13 accounting fields; no model fields |
| Settlement history (routed) | **PARTIAL (not here)** | `settle-wagers.yml` (`40 */4 * * *`) → destination importers | economics v2 for CFB/NFL/NHL/NBA/SOCCER/TENNIS; MLB settles itself | 4 h | `tests/test_settlement_*.py`, `test_live_settlement.py` | last settle run 2026-10-03 failed (NHL/SOCCER refusals) |
| Bankroll | **UNAVAILABLE to the app** (by design) | `src/kalshi_router/balance.py`, `scripts/publish_bankroll_secret.py`, `publish-bankroll.yml` (`*/15`) | one number, sealed into `edge-finder-api` Actions secret | 15 min | `tests/test_bankroll_publication.py`, `test_bankroll_publisher_repair.py` | never written to any file/branch; `performance.bankroll.history` is empty in all 7 sports |
| Kalshi series taxonomy | **PARTIAL** | `series_registry.py` (static), `series-probe.yml` (manual) | 348 tickers | manual | `tests/test_series_probe.py`, `test_classify.py` | MLB/NFL/CFB/TENNIS entries unverified |
| Kalshi competitions | **PARTIAL** | `competitions.py` (static), `taxonomy.py` (live) | 15 exact strings + heuristics | per audit run, in-memory | `tests/test_taxonomy.py` (203 lines), `test_classify.py` | not persisted |
| Milestones (event ↔ competition) | **RESEARCH** | `milestones.py` (live sweep, `TARGET_COMPETITIONS`, request budget) | backstop only; tennis not swept | per audit | `tests/test_milestones.py` | not persisted |
| CLV | **UNAVAILABLE** | — | — | — | — | contract field only; MLB (135) / NFL (66) wagers carry it from their repos |
| Calibration / accuracy / postmortems | **UNAVAILABLE** | — | — | — | — | `ledger_compare.py` compares router vs ledger rows, not model accuracy |
| Market price history | **UNAVAILABLE** | `EVENT_DETAIL.price_history` exists in the schema; empty in 21/21 fixture details | — | — | — | |
| Team/player metrics, game logs, lineups, injuries, weather, splits, projections | **UNAVAILABLE in router** | only pass-through in `event_detail.context`, `thesis.evidence`, `*.extensions` | see §7 | — | — | sport repos own them |
| Fixture | **PARTIAL** | `scripts/build_app_fixture.py` (no tests) → `docs/fixtures/edge_finder_app_fixture.v1.json` | 7 sports, 2026-10-02 snapshot | one-off | none | 13.7 MB |

---

## 5. Detailed findings

### 5.1 The contract package, module by module (`contract/edge_finder_contract/`, HEAD)

`__init__.py` (45 lines): `SCHEMA_VERSION = "edge_finder.app.v1"`, `CONTRACT_VERSION = "1.0.0"`, `SPORTS` (7), `KINDS` (16):
`manifest, events, markets, model_prices, recommendations, theses, wagers, settlements, runs, health, board, event_detail, performance,
router_health, recent_deliveries, sports_registry`. Docstring: "ONE SOURCE OF TRUTH … vendored byte-for-byte … Zero third-party dependencies".

`timeutil.py` (81): `ISO_UTC_RE = ^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$`, `DATE_RE`; `NaiveTimestampError`; `now_utc()`;
`parse_ts(value) -> datetime` (aware only; `Z` or offset; refuses naive/empty); `to_iso(value, *, precision="seconds"|"microseconds") -> str`;
`to_iso_or_none`; `is_canonical`; `age_seconds(as_of, now=None) -> float|None`; `to_date`; `plus(ts, seconds)`.

`validate.py` (194): `SUPPORTED_KEYWORDS = {$schema,$id,$defs,$ref,title,description,type,properties,required,additionalProperties,items,enum,const,pattern,format,minimum,maximum,exclusiveMinimum,exclusiveMaximum,minLength,maxLength,minItems,anyOf,oneOf,allOf,examples,default,deprecated}`;
`format` supports only `date-time` (= canonical UTC) and `date`; `$ref` local `#/$defs/<name>` only. API: `SchemaError(errors)`, `load_schema(kind)` (lru_cache, reads `schemas/<kind>.schema.json`),
`schema_kinds()`, `errors_for(value, schema|kind) -> list[str]`, `validate(value, schema|kind)`, `validate_document(doc)` (dispatch on `doc["kind"]`), `schema_keywords(schema) -> set`.
Keywords actually used by the 25 committed schemas: `$id,$schema,additionalProperties,anyOf,const,description,enum,format,items,maximum,minimum,pattern,required,title,type` (so `$defs/$ref/allOf/oneOf/min/maxLength/minItems/exclusive*` are supported but currently unused).

`ids.py` — §3 above.

`build.py` (360): converters `from_cents(v)`, `from_percent(v)`, `as_prob(v)` (round 6), `_num`, `_str` (blank→None), `_strs`, `_ids(source_ids)` (values → str|int|None), `normalise_family(v)` (lower snake_case, `"unknown"` default). Constructors (keyword-only; every field emitted; each validates):

| constructor | required kwargs | optional kwargs (default) |
|---|---|---|
| `participant(*, sport, participant_type, source, source_id, display_name, …)` | those 5 | `short_name=None, source_ids=None, metadata=None` |
| `event(*, sport, source, source_id, start_time_utc, participants, …)` | those 5 | `home_participant, away_participant, league, season, competition, status="SCHEDULED", start_time_local, start_time_source, start_time_confidence, effective_start_time_utc, venue, broadcast, source_ids, schedule_updated_at, last_updated_at (→ now), extensions` |
| `market(*, sport, kalshi_ticker, market_family, yes_description, source, …)` | those 5 | `event_id, kalshi_event_ticker (derived: ticker.rsplit("-",1)[0] if ≥2 dashes), kalshi_series_ticker (derived: ticker.split("-",1)[0]), market_type, period, participant_id, player_id, side, line, threshold, no_description, yes_bid, yes_ask, no_bid, no_ask, last_price, volume, open_interest, market_status="OPEN", close_time_utc, captured_at, raw_market_reference, extensions, market_probability (derived = (yes_bid+yes_ask)/2)` |
| `market_stub(*, sport, kalshi_ticker, market_family="unknown", yes_description=None, event_id=None, source="ledger", market_status="SETTLED")` | 2 | a market no longer on the board, prices null |
| `model_price(*, run_id, market_id, fair_probability, generated_at, …)` | those 4 | `event_id, model_version, lower_bound, upper_bound, uncertainty, market_probability, edge (derived fair−market), projection_value, projection_unit, inputs_as_of, freshness_status="UNKNOWN", data_quality_status="UNKNOWN", support_status, extensions` |
| `thesis(*, sport, run_id, event_id, generated_at, …)` | those 4 | `summary, primary_game_script, supporting_factors, opposing_factors, key_dependencies, context_notes (keys injuries/lineups/weather/usage/other), confidence_label, evidence, scope="event"` |
| `recommendation(*, sport, source_repo, event_id, market_id, run_id, selection, market_description, created_at, status, authority, research_only, …)` | those 11 | `native_id, current_probability, current_price, fair_probability, edge, bet_up_to_probability, bet_up_to_price, confidence, stake_units, stake_dollars, bankroll_basis, thesis_id, reason_not_playable, expires_at, data_freshness="UNKNOWN", lineup_status, injury_flags, source_ids, extensions` |
| `wager(*, sport, kalshi_ticker, selection, contracts, stake, average_price, placed_at, source, destination_repo, …)` | those 9 | `source_bet_key, native_id, kalshi_order_id, kalshi_fill_ids, event_id, side, fees, router_ingested_at, model_run_id, model_price_id, recommendation_id, linkage (4 keys), settlement_status="PENDING", settlement_id, payout, profit_loss, source_ids, extensions` |
| `settlement(*, wager_id, market_id, result, settled_at, source, verification_status, …)` | those 6 | `winning_side, settlement_value, gross_payout, fees, net_pnl, refusals, source_ids, extensions` |
| `run(*, sport, repo, completed_at, scope, status="SUCCESS", …)` | those 4 | `native_run_id, commit_sha, workflow_run_id, model_version, started_at, events_requested, events_processed=0, markets_discovered=0, markets_priced=0, recommendations_created=0, data_sources, input_freshness, warnings, errors, source_ids` |
| `collection(kind, sport, run_id, generated_at, items) -> dict` | all | the envelope: `schema_version, kind, sport, run_id, generated_at, count, items` |

`freshness.py` (65): states `FRESH, AGING, STALE, UNKNOWN`; `@dataclass(frozen) Thresholds(fresh_after_seconds:int, stale_after_seconds:int).as_dict()`;
`DEFAULT_THRESHOLDS` (seconds): `market_data (900, 3600)`, `model (3600, 21600)`, `schedule (86400, 259200)`, `recommendations (3600, 21600)`,
`export (1800, 10800)`, `router (1800, 14400)`, `settlement (86400, 259200)`; `classify(age, thresholds)` (None→UNKNOWN, negative→FRESH, ≤fresh→FRESH, ≤stale→AGING, else STALE);
`status_for(as_of, *, component="market_data", now=None, thresholds=None)`; `worst(*states)` (order FRESH<AGING<STALE<UNKNOWN).

`health.py` (121): component statuses `OK, DEGRADED, STALE, UNAVAILABLE, NOT_APPLICABLE, UNKNOWN`; `component(as_of, *, thresholds, now, required=True, detail=None, degraded=False, applicable=True) -> {status, as_of, age_seconds, detail}`;
`overall_status(*, model_status, market_data_status, bet_authority, errors, payload_available, export_failed, model_required=True)` → `UNAVAILABLE` (no payload / required UNAVAILABLE) > `STALE` > `DEGRADED` (export failed, errors, DEGRADED/UNKNOWN component) > `RESEARCH_ONLY` (authority) > `HEALTHY`;
`build_health(*, sport, run_id, bet_authority, last_market_capture, last_model_generated, last_successful_run, payload_run_id, payload_available, export_failed=False, commit_sha=None, next_scheduled_run=None, router_as_of=None, settlement_as_of=None, model_required=True, router_applicable=True, settlement_applicable=True, thresholds=None, warnings=None, errors=None, extra_components=None, now=None, generated_at=None) -> dict` (validated `health`).

`integrity.py` (111): `check_bundle(bundle: dict[kind, doc]) -> list[str]` — one run_id, one schema_version, one sport across files; duplicate primary keys; `home/away_participant ∈ participants`; `market.event_id ∈ events`; `model_price.market_id ∈ markets`, `.event_id`, `.run_id ∈ runs`; `recommendation.market_id/event_id/thesis_id`; `thesis.event_id`; `wager.market_id/event_id/settlement_id/recommendation_id/model_price_id`, `SETTLED ⇒ settlement_id`; `settlement.wager_id ∈ wagers` and market agrees. `check_manifest(manifest, documents, digests)` — every named file exists, run_id/kind/count/sha256 agree, every document is listed (except manifest, health).

`publish.py` (206): `COLLECTION_KINDS` (8), `MANIFEST_NAME`, `HEALTH_NAME`; `dumps(doc, *, compact=True)` (sort_keys, `separators=(",",":")`, ensure_ascii=False, trailing `\n`; pretty = indent 2); `sha256_text`; `PublishError(problems)`;
`build_manifest(*, sport, run_id, generated_at, documents: dict[name,text], parsed, source_repo, source_branch, commit_sha, model_version, status, freshness, warnings, counts=None) -> manifest` (files: `{path: "<name>.json", kind, sha256, bytes, count}`);
`validate_bundle(documents) -> problems`; `publish(*, root, sport, run_id, generated_at, documents: dict[kind|"event_detail/<id>", doc], source_repo, source_branch, commit_sha=None, model_version=None, status="SUCCESS", freshness=None, warnings=None, health=None, compact=True) -> manifest` — validate → integrity → manifest → staging dir beside root → `os.replace` per file, **health then manifest last** → delete files not in the new publication → rmdir empty dirs; collections compact, manifest/health pretty;
`write_health_only(root, health) -> Path`; `read_manifest(root)`; `verify_published(root) -> problems`; `now_iso()`.

`board.py` (120): `TOP_N = 3`; `_REC_ORDER = RECOMMENDED 0 < RESEARCH_CANDIDATE 1 < WATCH 2 < PASS 3 < NOT_PLAYABLE 4 < EXPIRED 5` then `-edge`;
`build_board(*, sport, run_id, generated_at, events, markets, model_prices, recommendations, wagers, health, thresholds=None, now=None)` — per event: `data_freshness = worst(market_data status of latest captured_at (UNKNOWN if no markets), model status of latest generated_at)`, `health_flags ∈ {NO_MARKETS, NO_MODEL_PRICES, STALE_DATA, START_TIME_PLACEHOLDER}`, `detail_path = f"event_detail/{event_id}.json"`, sorted by `(start_time_utc, event_id)`;
`build_event_detail(*, sport, run_id, generated_at, event, markets, model_prices, recommendations, theses, wagers, settlements, context=None, price_history=None, data_freshness="UNKNOWN")` — wagers by `event_id` **or** market membership.

`performance.py` (108): `RECENT_N = 50`; `build_performance(*, sport, run_id, generated_at, wagers, settlements, markets, recommendations=None, bankroll_history=None, bankroll_basis=None, clv_values: dict[wager_id, float]|None=None, notes=None)`; `_totals()` keys = TOTALS; a settlement without `net_pnl` counts in `unknown`/`data_completeness`; `roi = net_pnl / stake of settled-with-economics`; `by_month` keyed `placed_at[:7]`; `by_source` keyed `wager.source`; `by_market_family` via `markets[].market_family` (`"unknown"` if the market is absent).

`linkage.py` (75): `link_model_price(wager, model_prices)` — newest `inputs_as_of or generated_at ≤ placed_at` for the same market; `link_recommendation(wager, recs)` — same market+selection, `created_at ≤ placed_at`; `apply_links(wager, model_prices, recommendations, markets=None)` fills `model_price_id, model_run_id, recommendation_id, linkage{wager_placed_at, model_inputs_as_of, market_captured_at, recommendation_generated_at}` or nulls.

`routed_ledger.py` (517): `ECONOMICS_V2 = "router-settlement-economics.v2"`, `ENTRY_METHOD = "IMPORTED_RECEIPT"`, `VENUE = "kalshi"`, verdicts `NEW | DUPLICATE_NOOP | CONFLICT | REFUSED`;
`WAGER_INPUT_FIELDS = (source_bet_key, import_batch_id, entry_method, game_date, market_ticker, side, executed_at, contracts, execution_price, actual_price, stake, fees_paid, fees_are_estimated, venue, fee_state, execution_action)`; `WAGER_REQUIRED` (12); `SETTLEMENT_INPUT_FIELDS = (source_bet_key, market_ticker, side, settlement_status, settled_at, result, gross_return, net_profit_loss, refusals, venue, economics_version)`;
`PROVENANCE_FIELDS` (recommendation_id, model_version, fair_probability, edge, ev, kelly, thesis_id, authority, confidence, …) + regex `^(model_|fair_|edge_|probabilit|recommendation|prediction_|thesis|authority|confidence)` → **refused**;
`LedgerSpec(sport, id_prefix, wager_schema, settlement_schema, ledger_dir="data/accounting", wagers_file="wagers.jsonl", settlements_file="settlements.jsonl")`; `validate_wager/validate_settlement(spec, rec)`; `build_wager/build_settlement`; `import_wagers(spec, base_dir, rows, import_batch_id)`, `import_settlements(spec, base_dir, rows)` → `ImportResult(rows, written, duplicate, refused)`; `validate_ledger(spec, base_dir, base_texts=None)` (append-only proof via `text.startswith(base)`); CLIs `run_import_cli`, `run_validate_cli` (exit 0/1/2).

`sync.py` (99), `__main__.py` (35), `registry.json`, `MANIFEST.json` — §5.3.

### 5.2 Object shapes, quoted exactly (`schema_defs.py`, HEAD)

Conventions: `obj(props)` ⇒ **every property is required** (`required = sorted(properties)`), `additionalProperties: false` unless noted;
`ns` = string|null, `nnum` = number|null, `nint` = integer|null, `ts` = canonical UTC string, `nts` = ts|null, `prob`/`nprob` = number in [0,1];
`ID` = `^[a-z]{2,4}_[A-Za-z0-9._-]{4,}$`, `NID` nullable; `SOURCE_IDS` = object of string|integer|null; `EXTENSIONS` = free object; `free_object` = any object.
Shared enums: `SPORT` (7); `SELECTION ["YES","NO"]`; `FRESHNESS ["FRESH","AGING","STALE","UNKNOWN"]`;
`AUTHORITY ["RESEARCH_ONLY","SHADOW","LIMITED","TRUSTED","MANUAL","ASSISTED"]`; `OVERALL ["HEALTHY","DEGRADED","STALE","UNAVAILABLE","RESEARCH_ONLY"]`;
`COMPONENT_STATUS ["OK","DEGRADED","STALE","UNAVAILABLE","NOT_APPLICABLE","UNKNOWN"]`.

**PARTICIPANT**: `participant_id ID; display_name s; short_name ns; participant_type enum[TEAM,PLAYER,PAIR]; source_ids SOURCE_IDS; metadata free_object`.

**EVENT**: `event_id ID; sport SPORT; league ns; season ns; competition ns; home_participant NID; away_participant NID; participants [PARTICIPANT];
start_time_utc ts; start_time_local ns; start_time_source ns; start_time_confidence enum[SCHEDULED,VERIFIED,ESTIMATED,PLACEHOLDER,UNKNOWN]|null;
effective_start_time_utc nts; status enum[SCHEDULED,LIVE,FINAL,POSTPONED,CANCELLED,UNKNOWN]; venue ns; broadcast ns; source_ids SOURCE_IDS;
schedule_updated_at nts; last_updated_at ts; extensions EXTENSIONS`.

**MARKET**: `market_id ID; kalshi_ticker s; kalshi_event_ticker ns; kalshi_series_ticker ns; event_id NID; sport SPORT; market_family s (lower snake_case);
market_type ns; period ns; participant_id NID; player_id NID; side enum[HOME,AWAY,DRAW,OVER,UNDER,PARTICIPANT,TIE,OTHER]|null; line nnum; threshold nnum;
yes_description s; no_description ns; market_probability nprob; yes_bid nprob; yes_ask nprob; no_bid nprob; no_ask nprob; last_price nprob; volume nnum;
open_interest nnum; market_status enum[OPEN,CLOSED,SETTLED,UNOPENED,UNKNOWN]; close_time_utc nts; captured_at nts; source s; raw_market_reference ns; extensions`.

**MODEL_PRICE**: `model_price_id ID; event_id NID; market_id ID; run_id ID; model_version ns; fair_probability prob; lower_bound nprob; upper_bound nprob;
uncertainty nnum; market_probability nprob; edge nnum; projection_value nnum; projection_unit ns; generated_at ts; inputs_as_of nts; freshness_status FRESHNESS;
data_quality_status enum[OK,DEGRADED,CANNOT_TRUST_INPUTS,UNSUPPORTED,UNKNOWN]; support_status ns; extensions`.

**THESIS**: `thesis_id ID; event_id ID; run_id ID; summary ns; primary_game_script ns; supporting_factors [s]; opposing_factors [s]; key_dependencies [s];
context_notes obj{injuries ns, lineups ns, weather ns, usage ns, other ns}; confidence_label ns; evidence free_object; generated_at ts`.

**RECOMMENDATION**: `recommendation_id ID; event_id ID; market_id ID; sport SPORT; run_id ID; selection SELECTION; market_description s; current_probability nprob;
current_price nprob; fair_probability nprob; edge nnum; bet_up_to_probability nprob; bet_up_to_price nprob; confidence ns; stake_units nnum; stake_dollars nnum;
bankroll_basis nnum; thesis_id NID; status enum[RECOMMENDED,WATCH,PASS,RESEARCH_CANDIDATE,EXPIRED,NOT_PLAYABLE]; reason_not_playable ns; created_at ts; expires_at nts;
data_freshness FRESHNESS; lineup_status ns; injury_flags [s]; research_only boolean; authority AUTHORITY; source_repo s; source_ids SOURCE_IDS; extensions`.

**LINKAGE**: `wager_placed_at nts; model_inputs_as_of nts; market_captured_at nts; recommendation_generated_at nts`.

**WAGER**: `wager_id ID; source_bet_key ns; kalshi_order_id ns; kalshi_fill_ids [s]; event_id NID; market_id ID; kalshi_ticker s; sport SPORT; selection SELECTION;
side enum[BUY,SELL]|null; contracts number≥0; stake number≥0; average_price prob; fees nnum; placed_at ts; source enum[KALSHI_ROUTER,MANUAL,LEGACY_IMPORT,OTHER];
router_ingested_at nts; destination_repo s; model_run_id NID; model_price_id NID; recommendation_id NID; linkage LINKAGE;
settlement_status enum[PENDING,SETTLED,VOID,UNKNOWN]; settlement_id NID; payout nnum; profit_loss nnum; source_ids SOURCE_IDS; extensions`.

**SETTLEMENT**: `settlement_id ID; wager_id ID; market_id ID; result enum[WON,LOST,PUSH,VOID,SCALAR,UNKNOWN]; winning_side enum[YES,NO]|null; settlement_value nprob;
settled_at ts; gross_payout nnum; fees nnum; net_pnl nnum; source s; source_ids SOURCE_IDS; verification_status enum[EXCHANGE_CONFIRMED,MODEL_DERIVED,UNVERIFIED,REFUSED];
refusals [s]; extensions`.

**RUN**: `run_id ID; sport SPORT; repo s; commit_sha ns; workflow_run_id ns; model_version ns; started_at nts; completed_at ts; status enum[SUCCESS,PARTIAL,FAILED];
scope s; events_requested nint; events_processed integer; markets_discovered integer; markets_priced integer; recommendations_created integer; data_sources [s];
input_freshness object{additionalProperties: ts|null}; warnings [s]; errors [s]; source_ids SOURCE_IDS`.

**Envelope** `envelope(kind, item_schema, extra_props, *, sport_required=True)`: `schema_version const "edge_finder.app.v1"; kind const <kind>; sport SPORT (or SPORT+"ALL" when sport_required=False); run_id ID; generated_at ts;` + for collections `count integer≥0; items [item]`.
Collections (`COLLECTIONS`): `events, markets, model_prices, recommendations, theses, wagers, settlements, runs`.

**MANIFEST** = envelope("manifest") + `commit_sha ns; model_version ns; status enum[SUCCESS,PARTIAL]; source_repo s; source_branch s;
files object{additionalProperties: FILE_ENTRY{path s, kind enum(KINDS), sha256 s, bytes integer≥0, count nint}}; counts object{additionalProperties integer≥0};
freshness object{additionalProperties obj{as_of nts, status FRESHNESS}}; warnings [s]`.

**HEALTH** = envelope("health") + `overall_status OVERALL; model_status COMPONENT_STATUS; market_data_status COMPONENT_STATUS; router_status COMPONENT_STATUS;
settlement_status COMPONENT_STATUS; freshness_status FRESHNESS; bet_authority AUTHORITY; last_successful_run nts; last_export_attempt ts; payload_run_id NID;
last_market_capture nts; last_model_generated nts; next_scheduled_run nts; data_age_seconds nnum;
thresholds object{additionalProperties obj{fresh_after_seconds integer, stale_after_seconds integer}};
components object{additionalProperties COMPONENT{status COMPONENT_STATUS, as_of nts, age_seconds nnum, detail ns}}; warnings [s]; errors [s]; commit_sha ns`.

**BOARD** = envelope("board", BOARD_EVENT) + `overall_status OVERALL; bet_authority AUTHORITY`, where
BOARD_EVENT: `event_id ID; league ns; competition ns; start_time_utc ts; status s; home_participant NID; away_participant NID;
participants [COMPACT_PARTICIPANT{participant_id, display_name, short_name, participant_type}]; data_freshness FRESHNESS; market_captured_at nts; model_generated_at nts;
markets_available integer≥0; markets_priced integer≥0; recommendations_count integer≥0;
top_recommendations [COMPACT_REC{recommendation_id ID, market_id ID, selection SELECTION, market_description s, fair_probability nprob, current_price nprob, edge nnum, status s, authority AUTHORITY, research_only boolean}];
wagers_count integer≥0; health_flags [s]; detail_path s`.

**EVENT_DETAIL** = envelope("event_detail") + `event EVENT; markets [MARKET]; model_prices [MODEL_PRICE]; recommendations [RECOMMENDATION]; theses [THESIS]; wagers [WAGER];
settlements [SETTLEMENT]; context free_object; price_history [obj{market_id ID, captured_at ts, yes_bid nprob, yes_ask nprob, last_price nprob}]; data_freshness FRESHNESS`.

**PERFORMANCE** = envelope("performance") + `as_of ts; totals TOTALS; by_market_family {→TOTALS}; by_month {→TOTALS}; by_source {→TOTALS};
recent_results [obj{wager_id ID, settlement_id ID, market_id ID, kalshi_ticker s, selection SELECTION, result s, net_pnl nnum, settled_at ts}];
pending_wagers [obj{wager_id ID, market_id ID, kalshi_ticker s, selection SELECTION, stake number, placed_at ts}];
recommended_vs_wagered obj{recommendations integer≥0, wagers_linked_to_recommendation integer≥0, wagers_unlinked integer≥0};
clv obj{available boolean, wagers_with_clv integer≥0, mean_clv nnum}; bankroll obj{available boolean, basis ns, history [obj{as_of ts, balance number}]};
data_completeness object{additionalProperties integer|number|string|boolean|null}; notes [s]`,
TOTALS: `wagers, settled, pending, won, lost, push, void, scalar, unknown (integer≥0); stake, gross_payout, fees, net_pnl, roi, open_exposure (nnum); settled_with_economics integer≥0`.

**ROUTER_HEALTH** = envelope("router_health", sport_required=False) + `overall_status OVERALL; router_health_state enum[healthy_no_op,delivered,not_routable,deferred,blocked,unknown];
last_poll_at nts; poll_age_seconds nnum; last_delivery_run RUN_REF; last_settlement_run RUN_REF; bets_discovered nint; delivered nint; failed nint; blocked nint; deferred nint;
by_sport {→SPORT_ROUTE}; thresholds {→obj{fresh_after_seconds, stale_after_seconds}}; warnings [s]; errors [s]; commit_sha ns`;
RUN_REF: `run_id ns; url ns; started_at nts; concluded_at nts; conclusion ns; health_state ns`;
SPORT_ROUTE: `routable boolean; classification enum[SUPPORTED,UNSUPPORTED]; destination_repo ns; ledger_branch ns; auto_merge boolean|null; eligible nint; delivered nint; failed nint;
status enum[DELIVERED,NO_OP,FAILED,REFUSED,NOT_ROUTABLE,DRY_RUN,UNKNOWN]; last_error_type ns`.

**RECENT_DELIVERIES** = envelope("recent_deliveries", DELIVERY, sport_required=False); DELIVERY: `run_id s; run_url ns; started_at nts; sport SPORT; destination ns;
status enum[DELIVERED,MERGED,FAILED,REFUSED,DRY_RUN,NO_OP,SETTLED,PARTIAL]; rows nint; attempt nint; error_type ns; error_message ns; wager_ids [s]; first_failed_at nts;
last_attempt_at nts; retry_status enum[WILL_RETRY,NEEDS_ATTENTION,RESOLVED,NOT_APPLICABLE]|null`.

**SPORTS_REGISTRY** = envelope("sports_registry", sport_required=False) + `sports {→SPORT_LOCATION}; router SPORT_LOCATION`;
SPORT_LOCATION: `repo s; branch s; app_root s; raw_base_url s; status enum[ACTIVE,PLANNED]; notes [s]`.

`registry.json` (HEAD, kind `sports_registry`, run_id `run_registry0000000000000`): MLB `edge-finder-api@main:app/latest`; CFB `cfb-edge-finder@main:app/latest`
("model retired… model_prices.json is empty by design"); NFL `nfl-edge-finder@handicap-reports:app/latest` ("branch is replaced wholesale on every publish");
NBA `nba-edge-finder@data-archive:app/latest`; NHL `NHL-edge-finder@data-archive:app/latest`; SOCCER `soccer-edge-finder@data-archive:app/latest`;
TENNIS `Tennis-Edge-Finder@tennis-data:tennis-edge-finder/data/app/latest`; router `kalshi-bet-router@app-data:app/latest`.

### 5.3 How schemas are generated, pinned, extended and vendored

* **Generation**: `schema_defs.all_schemas()` builds `{kind: schema}` for `COLLECTIONS` (wrapped by `envelope`), `SINGLETONS` and `OBJECTS`, each with
  `title=<kind>`, `$schema = draft/2020-12`, `$id = https://edge-finder.app/schemas/edge_finder.app.v1/<title>` (`with_defs`); `render()` = `json.dumps(indent=2, sort_keys=True)+"\n"`;
  `python -m edge_finder_contract.schema_defs` → `write_all()` writes `schemas/<kind>.schema.json` (25 files at HEAD: 16 kinds + 9 objects; 383,854 bytes; largest `event_detail` 40 KB).
* **Pinning tests** (`tests/contract/test_contract_v1.py`): `test_committed_schemas_equal_the_generator` (byte equality per kind), `test_every_schema_keyword_is_one_the_validator_implements`
  (iterates `validate.schema_kinds()` = every `*.schema.json` on disk, so a **new schema file is automatically covered**), `test_every_kind_has_a_schema` (iterates `efc.KINDS`),
  `test_vendored_manifest_matches_the_package` (`sync.check() == []`), `test_registry_lists_every_sport_and_the_router`.
* **Adding a new kind/schema** (as `main` is built): (1) add the kind string to `KINDS` in `__init__.py` (needed by `FILE_ENTRY.kind enum(KINDS)` in the manifest, and by `test_every_kind_has_a_schema`);
  (2) define the object in `schema_defs.py` and register it in `SINGLETONS`/`COLLECTIONS`/`OBJECTS`; (3) run `python -m edge_finder_contract.schema_defs` (regenerates **all** files, including
  `manifest.schema.json`, whose `kind` enum changes — this is exactly why the working-tree drift shows `manifest.schema.json` as modified); (4) `python -m edge_finder_contract.sync --manifest`;
  (5) optionally a `build.*` constructor + `publish.COLLECTION_KINDS` if it is a collection that integrity should cover; (6) bump `CONTRACT_VERSION` (semver, additive) per `docs/EDGE_FINDER_APP_CONTRACT.md §2`
  ("within v1 a change may only ADD optional (nullable) fields or enum values").
* **Vendoring / sync** (`sync.py`): `digests(root)` = sha256 of every file except `__pycache__`, `*.pyc`, `MANIFEST.json`; `write_manifest()` → `{"schema_version","contract_version","files":{relpath: sha}}`;
  `check(root, other=None)` → problems `missing here / not in the reference / differs`; `copy_to(repo_root)` → `rm -rf` + copytree into `<repo>/contract/edge_finder_contract`.
  CLI: `--manifest`, `--check [PATH]`, `--copy-to PATH`. Per-repo test name in sport repos: `tests/test_app_contract_v1.py` (per `__init__` docstring and the app contract doc §10);
  this repo's equivalent is `tests/contract/test_contract_v1.py::test_vendored_manifest_matches_the_package`. **No script regenerates the manifest other than `python -m edge_finder_contract.sync --manifest`;
  no CI step re-vendors** — propagation to the seven repos is manual (`--copy-to`).
* `__main__.py`: single command `validate <file.json | dir>...` (a dir → `publish.verify_published`), exit 1 on failure, 2 on usage.
* The contract has a single commit of history (`git log -- contract/` → only `45f610b`, PR #105).

### 5.4 Cross-sport research assets in the router

**Wager / settlement history.** The router holds none. Pipeline: `kalshi_router.cli deliver` (`deliver-wagers.yml`, cron `*/15`, plus `router-conductor.yml` cron `11 * * * *` driving
`scripts/router_conductor.py --minutes 330 --interval 120` dispatching delivery every 20 min and settlement every 60 min) → per-sport payload (`production.to_mlb_import_row`, `to_nfl_import_row`,
`to_cfb_import_row`, `to_nhl_import_row`, `_to_shared_ledger_row(sport)` for NBA/SOCCER/TENNIS) → destination importer → PR into the ledger branch → `scripts/merge_delivery_pr.py` gate.
Row dialect (`production.py:823-990`, `routed_ledger.WAGER_INPUT_FIELDS`): `source_bet_key, import_batch_id ("kalshi-router-v1"), entry_method ("IMPORTED_RECEIPT"), game_date (YYYY-MM-DD), market_ticker, side (YES|NO),
executed_at (RFC3339), contracts, execution_price|actual_price (quantity-weighted), stake (= contracts×price+fees), fees_paid, fees_are_estimated=false, venue="kalshi"` (+NFL `fee_state`, `execution_action`).
Settlement dialect: `source_bet_key, market_ticker, side, settlement_status="SETTLED", settled_at, result (WON|LOST|null), gross_return, net_profit_loss, refusals [], venue, economics_version="router-settlement-economics.v2"`.
`scripts/settlement_season.py`, `scripts/payload_season.py` derive the CFB season; `scripts/reconcile_delivery.py` reconciles by identity. Backfill: one-time `backfill-deliver.yml` / `backfill-settle.yml`
with a structurally fixed cutover `PRODUCTION_CUTOVER_ISO = "2026-09-15T00:00:00Z"` (`docs/CLOSEOUT.md`). What the app sees is each sport's `wagers.json`/`settlements.json`; measured in the fixture:
MLB 149 wagers / 144 settlements (sources KALSHI_ROUTER, LEGACY_IMPORT, MANUAL; months 2026-06, 09, 10; `verification_status` MODEL_DERIVED), CFB 97/97 (EXCHANGE…: v2 via ledger), NFL 84/84 (EXCHANGE_CONFIRMED; 2 without economics),
NHL/NBA/SOCCER/TENNIS 0 (NHL note: "accounting ledger is empty" — 17 real wagers blocked on token scope per `docs/EDGE_FINDER_APP_CONTRACT.md §11`; router_health on 2026-10-03 shows NHL 19 and SOCCER 9 delivered since).

**Bankroll.** `kalshi_router.cli bankroll --out <RUNNER_TEMP>/bankroll.json` → `balance.build_bankroll_context` → `{schemaVersion:"1", bankroll:float, currency:"USD", observedAt, source:"kalshi_authenticated_balance", valueType:"KALSHI_AVAILABLE_CASH_BALANCE"}`
→ `scripts/publish_bankroll_secret.py` seals it (libsodium) into `edge-finder-api`'s Actions secret; 30 tests assert the number never reaches stdout, a file in the repo, an artifact or a job summary.
**Not usable by the research layer** (the contract's `performance.bankroll.history` is empty in all 7 exports; `docs/EDGE_FINDER_APP_CONTRACT.md §9` forbids it).

**Kalshi taxonomy for market → entity linking.** (a) `series_registry.SERIES_TICKER_REGISTRY` — 348 exact `KX…` series → `Sport`, with `verified` flags (NHL/NBA/SOCCER from live discovery runs 2026-09-19/27/29; the
rest unverified). Covers the *series* (`market.kalshi_series_ticker`) only; the fixture shows the series actually traded: MLB `KXMLBHRR/TB/HIT/RBI/SB/TEAMTOTAL/TOTAL/SPREAD/F5*/KS/F3/F7/GAME`, NFL `KXNFLRECYDS/REC/RSHYDS/TD/FIRSTTD/TEAMFIRSTTD/…`,
NHL `KXNHLFIRSTGOAL/GOAL/PTS/AST/…`, CFB `KXNCAAF*`, SOCCER `KXUEFANL*/KXBRASILEIRO*`, NBA `KXNBAGAME/1H`, TENNIS `KXATPCHALLENGERMATCH/KXITFMATCH` — of which **`KXMLBHRR/TB/HIT/RBI/SB/F3/F7/KS`, every `KXNCAAF…` period series, `KXNFLRECYDS…`, `KXATPCHALLENGERMATCH`, `KXITFMATCH` are absent from the registry**.
(b) `competitions.py` — `COMPETITION_TO_SPORT` (15 strings: "pro baseball", "pro football", "college football", "pro hockey", "pro basketball (m)", …), `COMPETITION_OUT_OF_SCOPE`, `SPORT_TO_SPORT` (tennis/soccer), `AMBIGUOUS_SPORTS {football, baseball, hockey, basketball}`,
`SPORT_FAMILY_MEMBERS`, `TENNIS_COMPETITION_TOKENS (atp, wta, itf)`, `SOCCER_COMPETITION_TOKENS` (19). (c) `taxonomy.parse_filters_by_sport(payload) -> SportTaxonomy` from `GET /search/filters_by_sport` (live, cached in memory per audit; collisions fail closed).
(d) `milestones.build_milestone_index(client, competitions=TARGET_COMPETITIONS, request_budget, page_limit)` from `GET /milestones?category=Sports&competition=…` → `MilestoneIndex` (event_ticker → competition; conflicts fail closed; tennis not swept).
(e) Event-ticker date parsing: `wager._EVENT_TICKER_DATE = -(\d{2})([A-Z]{3})(\d{2})` → `game_date` (`wager.resolve_game_date`). **None of (a)–(e) is persisted; none yields a team or player id.** CFB's export uses `kalshi_milestone_id` as its event identity and `kalshi_football_team` uuids as team identity — the only sport whose canonical ids are Kalshi's own.

**CLV / accuracy.** None in the router. `ledger_compare.py` and `historical-shadow-compare.yml` (manual) compare router-derived wagers to a destination ledger (identity/economics agreement), not predictions to outcomes.
`docs/HISTORY.md` ("Authoritative history — Phase C") is about fill/settlement completeness of the Kalshi account walk, not research history.

### 5.5 The fixture (`docs/fixtures/edge_finder_app_fixture.v1.json`)

* Size: 13,675,961 B on disk (`json.dumps(indent=1, sort_keys=True, ensure_ascii=False)`), 10,333,018 B compact. One commit (`45f610b`). Not tested. `generated_at 2026-10-02T23:17:58Z`, `kind: "app_fixture"`, `fixture_version: "edge_finder_app_fixture.v1"`.
* Top level: `description, fixture_version, generated_at, kind, missing_sports [] , router {recent_deliveries, router_health, sports_registry: null}, schema_version, sports {7}, sports_registry (copy of registry.json)`.
  Note `router.sports_registry` is **null** (the app-data dir it was built from predates that file; `load()` returns None for a missing file).
* Per sport block (`build_app_fixture.sport_block`): `sport, source {repo, branch, run_id, generated_at, commit_sha}, events, markets, model_prices, recommendations, theses, wagers, settlements, runs, board (filtered items), health, performance (full), event_detail {event_id: doc}, coverage {kind: full-export count}`.
  Event selection (`pick_events`): board rows sorted by `(-recommendations_count, -markets_priced, -markets_available, start_time_utc)`, top N=3, swapping in one wagered event if none chosen has wagers; markets kept if `event_id ∈ keep` **or** referenced by a kept wager.
* Measured content (full-export `coverage` → kept):

| sport | events | markets | model_prices | recs | theses | wagers | settlements | health / authority / freshness | perf totals |
|---|---|---|---|---|---|---|---|---|---|
| MLB | 1→1 | 482→342 | 61→61 | 7→7 | 1→1 | 149→3 | 144→3 | STALE / MANUAL / STALE | 149 wagers, net −268.59, roi −4.1 %, clv mean −0.0054 (135) |
| CFB | 224→3 | 13,770→891 | 0 | 0 | 0 | 97→0 | 97→0 | STALE / RESEARCH_ONLY / STALE | 97 wagers, net −150.85, roi −4.7 % |
| NFL | 51→3 | 10,796→1,499 | 5,612→806 | 0 | 16→2 | 84→8 | 84→8 | HEALTHY / MANUAL / AGING | 84 wagers, net −1,068.35, roi −14.1 %, clv mean −0.70 (66; units differ from MLB) |
| NBA | 40→3 | 4,093→9 | 0 | 0 | 0 | 0 | 0 | UNAVAILABLE / RESEARCH_ONLY / STALE | none |
| NHL | 5→3 | 3,204→544 | 125→75 | 19→12 | 24→15 | 0 | 0 | RESEARCH_ONLY / RESEARCH_ONLY / FRESH | none |
| SOCCER | 37→3 | 1,311→151 | 1,311→151 | 1→1 | 2→1 | 0 | 0 | RESEARCH_ONLY / RESEARCH_ONLY / FRESH | none |
| TENNIS | 116→3 | 471→6 | 264→6 | 41→6 | 0 | 0 | 0 | HEALTHY / ASSISTED / AGING | none |

* Bytes (compact) per sport block: NFL 5.27 MB, CFB 1.84 MB, NHL 1.53 MB, MLB 0.98 MB, SOCCER 0.60 MB, TENNIS 77 KB, NBA 35 KB; router 3.4 KB. Per market ≈1.0–1.15 KB (extensions 10–26 % of that), per model_price ≈0.86–1.13 KB, per event 1.1–2.0 KB.
  `event_detail` per event: NFL 875 KB, MLB 476 KB, CFB 304 KB, NHL 254 KB, SOCCER 99 KB, TENNIS 13 KB, NBA 5 KB.
* Missingness that matters for research: `price_history` **0 points in 21/21** event details; `projection_value` null in 1,099/1,099 model prices; `lower/upper_bound` present only SOCCER (151/151) and TENNIS (6/6); `uncertainty` null everywhere sampled;
  `wager.recommendation_id` null 11/11, `model_price_id` set only MLB 3/3; `market.player_id` set only NFL (722) and NHL (389); `market.captured_at` null NFL 8 (stubs), SOCCER 15; `event.start_time_confidence` = SCHEDULED for all 19 events (no PLACEHOLDER sampled).
* Sport-specific research payload that already travels in free-form fields (the research layer's raw material): MLB `event.extensions {away_pitcher, home_pitcher, park, park_factor, lineup_status, lineup_confirmed, lineup_source}`, `thesis.evidence {projected_runs, ledger_rows}`, `wager.extensions {closing_price, clv_pct_points, clv_convention}`;
  NFL `event_detail.context {capture, coverage{A/B/C/D bucket counts}, game_state}`, `market.extensions {shadow_v2_p_yes, bucket, subject, subject_kind, stat, operator}`; NHL `event_detail.context {goaltending{home,away: player_id, status, confidence}, injuries[{player_name, position, status, return_date, team_id}], model_game{exp_home_goals, exp_away_goals, p_home_win, p_overtime,…}, rest}`, `thesis.evidence {n_sims 10000, scripts[], rest, exp_total}`, `recommendation.extensions {portfolio_impact, growth_bp, p_model_joint_draw, …}`;
  SOCCER `event.extensions.slate {model_family, model_validity, minutes_to_kickoff_at_publish, reference_quality}`, `model_price.extensions {n_worlds 1000, param_sd, interval_level}`; TENNIS `event.extensions {surface, level, round, discipline, data_quality_status}`, `event_detail.context {data_quality_check{matches_a, serve_points_a, days_since_last_a…}, external_context, first_ball, identity_checks}`;
  CFB `event.extensions {conference, division, season_week, family_distribution, milestone_status}`, `event_detail.context {catalog_events, completeness, teams}`; NBA `participant.metadata {conference, division}`.

### 5.6 Test layout, pyproject, CI

* `pyproject.toml`: `requires-python >=3.11`; deps `cryptography>=41`; `dev = pytest>=7, PyYAML>=6, PyNaCl>=1.5`; `[tool.setuptools.packages.find] where = ["src","contract"]`;
  `[tool.pytest.ini_options] testpaths=["tests"], pythonpath=["src","contract"], addopts="-q"`. **No ruff / flake8 / mypy / black configuration anywhere** (`grep -rn ruff pyproject.toml .github/` → nothing).
* Tests: `tests/` (60 files) + `tests/contract/` (`test_contract_v1.py` 30 tests, `test_routed_ledger.py` 8 tests, `fixtures.py` with `SPORT_CASES` for all 7 sports, `NOW/CAPTURED/MODELLED` constants); `tests/conftest.py` (fake Kalshi signer, `AuditConfig`), `tests/synthetic.py`, `tests/github_api_stub.py`.
  ≈1,478 `def test_` across 62 files. Script tests load scripts by path (`importlib.util.spec_from_file_location`, e.g. `tests/test_router_health_publisher.py:15`). `scripts/build_app_fixture.py` has **no test**.
* CI (`.github/workflows/ci.yml`): on push to `main` and all PRs; `permissions: contents: read`; matrix `python-version: ["3.11","3.12"]`; `pip install ".[dev]"`; `python -m pytest`. Recent durations (GitHub, created→updated): 2m37s, 3m19s, 2m45s, 2m34s, 3m21s, 2m30s.
  Other workflows: `deliver-wagers` (`*/15`), `settle-wagers` (`40 */4 * * *`), `publish-bankroll` (`*/15`), `publish-router-health` (`*/15`), `router-conductor` (`11 * * * *`); manual: `backfill-deliver`, `backfill-settle`, `backfill-inspect`, `historical-shadow-compare`, `phase0-readonly-audit`, `series-probe`, `recover-wagers`, `downstream-credential-probe`.
* Local timing in this sandbox is not representative: `pytest tests/contract` 15.2 s wall / 1.5 s CPU; full suite > 11 min wall (CI: ≈3 min).

### 5.7 Size / performance constraints and conventions

* **No byte-size test or limit exists** for any app payload, the fixture, or the contract (`grep -rn -i "MB\b|size budget|too large" docs README` → nothing; tests compare bytes only for idempotency).
* Conventions that bound output: `board.TOP_N = 3`; `performance.RECENT_N = 50` recent results (pending list unbounded); `publish_router_health.RECENT_LIMIT = 200`; `scrub()` truncates error text to 240 chars; collections are written compact (`publish.dumps(compact=True)`), manifest/health pretty.
  Fixture builder defaults `--events-per-sport 3`. `routed_ledger.run_validate_cli` prints at most 50 failures. `taxonomy.MAX_OBSERVED_KEYS = 40`, milestones `request_budget`.
* Publication atomicity convention (manifest last, sha256 per file) is the only "performance" contract a reader relies on.

---

## 6. Opponent-adjustment audit

**None.** No formula, baseline, sample rule or recursion exists in this repository. The only "adjusted" numbers in the fixture are sport-side (`model_price.extensions.p_market_anchored` in NHL, `recommendation.extensions.p_confidence_adjusted`) and are not opponent adjustments.

---

## 7. Time-series, splits, market-history and projection inventories (router scope)

| dataset | x-axis | keys | rows | linkable to event/opponent? | status |
|---|---|---|---|---|---|
| `app-data/recent_deliveries.json` | run `started_at` | `(run_id, sport, status)` | 12 now (max 200), one commit of history | no (counts only) | VERIFIED, rolling |
| `router_health.json` | snapshot | — | 1 | no | VERIFIED, replaced |
| `EVENT_DETAIL.price_history` | `captured_at` | `market_id` | **0 in every export** | yes by design (`market_id → event_id`) | UNAVAILABLE today |
| `performance.by_month` | month (`placed_at[:7]`) | sport → month | MLB 3, CFB 2, NFL 2 months | no | VERIFIED where wagers exist |
| `performance.bankroll.history` | `as_of` | — | 0 everywhere | — | UNAVAILABLE |
| `runs.json` | run | `run_id` | 1 per publication (no run history retained; NFL branch "replaced wholesale") | — | PARTIAL |

Splits: none stored by the router. The contract's only split-like dimensions are `market.period` (values seen: `FULL_GAME/first_half/second_half/*_quarter` CFB; `FULL/1H/2H/1Q-4Q` NFL/NBA; `FULL/P1/P2/P3` NHL; `FULL_GAME/F5/F3/F7` MLB; `regulation/first_half/including_extra_time` SOCCER) and `market.side`.
Market history: no per-ticker quote series anywhere in this repo (the sport repos' capture archives are outside scope). Projections: `model_price.projection_value/unit` exist in the schema but are null in all 1,099 sampled model prices; distributions appear only in `extensions` (SOCCER `n_worlds`, NHL `n_sims`, `scripts`) — per market, point estimates, one run per publication.

---

## 8. Existing ranking / percentile / league-average code

None in the router. `performance.py` computes sums, counts, `roi` and `mean_clv`; `board.py` sorts recommendations by status then `-edge`; `build_app_fixture.pick_events` sorts board rows. No percentile, rank or league-average helper exists on `main`.

---

## 9. Size estimates (for lazy-loading design)

Per-record costs measured on the real exports (compact JSON): market ≈1.0–1.15 KB, model_price ≈0.9–1.1 KB, event ≈1.1–2.0 KB, wager ≈1.2 KB, settlement ≈0.7 KB, recommendation ≈1.4–2.0 KB, thesis 1–3 KB.
Projected sizes if a research layer reused these shapes:

| artefact | basis | estimate |
|---|---|---|
| (a) team profiles | 30 teams × (participant 0.3 KB + ~40 observations × 0.35 KB + links) | ≈15 KB/team, ≈0.5 MB/sport (NFL/NHL/NBA/MLB); CFB ≈130 teams → ≈2 MB |
| (b) player profiles | NFL ~1,700 / NHL ~700 / MLB ~1,200 players × ≈8–10 KB | 7–17 MB/sport if eager; must be per-file |
| (c) per-game detail | today's `event_detail`: NFL 875 KB, MLB 476 KB, CFB 304 KB, NHL 254 KB, SOCCER 99 KB, TENNIS 13 KB, NBA 5 KB per event | already too large to prefetch; markets dominate (NFL 1,499 markets × 1.15 KB) |
| (d) metric time series | ≈0.15 KB/point; 17 games × 40 metrics × 32 teams | ≈3.3 MB/season/sport eager; ≈6 KB per (entity, metric) file |
| (e) market history | `price_history` point ≈0.1 KB; 1 capture/15 min × 48 h = 192 pts × 1,500 markets (NFL week) | ≈29 MB/week eager; ≈19 KB per market file |

Whole-app today: the seven `app/latest` roots sum to ≈28–35 MB of markets alone (CFB 13,770 + NFL 10,796 + NBA 4,093 + NHL 3,204 + SOCCER 1,311 + MLB 482 + TENNIS 471 = 34,127 markets × ≈1.05 KB ≈ 36 MB), which is why anything beyond `board.json` must stay per-event/per-entity.

---

## 10. Recommended research capabilities to expose from this repo in this pass

Expose (evidence supports it):
1. **Contract additions only, additive** — new kinds appended to `KINDS`, new objects in `schema_defs.py`, regenerated schemas, `sync --manifest`, `CONTRACT_VERSION` bump, same `ids.make_id` digest, same FRESHNESS/AUTHORITY/`QUALITY`-style enums, same `publish.publish` staging/atomic rule and manifest `files{sha256,bytes}` entries. (The working-tree drift already does exactly this; §11.)
2. **`sports_registry` as the discovery root** for per-sport research roots (add a nullable `research_root`/notes rather than a new file), published through the existing `app-data` force-push.
3. **Router health/recent-deliveries** as the only router-owned time series: keep it as is; a research "wager_history" capability should point at each sport's `wagers.json`/`performance.json`, not at the router.
4. **Series → sport registry** (`series_registry.py`) as a static lookup for `kalshi_series_ticker` → sport/family grouping, with its `verified` flag surfaced as PARTIAL; extend it from the series actually observed in exports (the ~40 missing MLB/NFL/CFB/TENNIS series listed in §5.4).

Mark RESEARCH / UNAVAILABLE here:
* `clv`, `calibration`, `historical_accuracy`: UNAVAILABLE in router; PARTIAL only via MLB/NFL `performance.clv` (different units: MLB pct-points/100, NFL mean −0.70 in unknown units — reconcile before display).
* `market_price_history`: UNAVAILABLE until a sport fills `price_history`; the schema slot exists.
* `bankroll`: UNAVAILABLE by policy (sealed secret); do not add a capability.
* Taxonomy/milestones: RESEARCH (live, in-memory, budgeted; never persisted).
* Team/player metrics, game logs, lineups, injuries, weather, splits, projections: not router capabilities; the router can only carry whatever each sport publishes in `extensions`/`context`/`evidence`, which today are free-form and sport-specific (§5.5).

---

## 11. Open questions / UNKNOWN items and what was checked

1. **Working-tree drift vs `main`** (header note). What the uncommitted layer already assumes, so the design can confirm or correct it: `CONTRACT_VERSION "1.1.0"`; 12 new kinds (`explorer_index, capability_manifest, metric_registry, entity_profile, event_research, ranking, time_series, market_history, search_index, handicap_packet, handicap_protocol, research_tray`);
   `QUALITY_STATUSES = (VERIFIED, PARTIAL, RESEARCH, UNAVAILABLE, UNKNOWN)` matching this audit's vocabulary; 36 `CAPABILITIES`; `metric_id = "met_<sport>.<slug>"` (readable, like `market_id`), `observation_id/ranking_id/series_id/packet_id/tray_item_id` via `make_id`;
   `WINDOW_KIND (SEASON, LAST_N, DATE_RANGE, GAME, RUN, CUSTOM)`, `X_AXIS (GAME, WEEK, DATE, RUN, CAPTURE)`, `SPLIT {dimension, value}`, `OBS_CONTEXT {rank, universe_size, percentile, league_average, …}`, `MARKET_HISTORY.series[].points[] {captured_at, yes_bid, yes_ask, last_price, volume, open_interest, source}` (a superset of `EVENT_DETAIL.price_history`).
   `sync --check` on that tree: 33 problems; `test_vendored_manifest_matches_the_package` fails until `sync --manifest` is run; `manifest.schema.json` changes because `FILE_ENTRY.kind = enum(KINDS)`.
2. **Full local test-suite runtime**: the background run finished (exit observed only for the pipeline) after >11 min wall; CI is ≈3 min. UNKNOWN why this sandbox is 4–5× slower (contract tests: 15 s wall vs 1.5 s CPU) — not a repo property.
3. **Destination ledger row counts / date ranges** (true wager history) are UNKNOWN from this repo; only `router_health` counts (278 delivered as of 2026-10-03T02:11Z) and the fixture's `coverage` (MLB 149, CFB 97, NFL 84 wagers) were measurable. The ledgers themselves live in other repositories.
4. **`pytest --co` count** could not be captured cleanly in this sandbox (collection stalled twice); the `def test_` count (1,478) is a lower bound.
5. **Registry accuracy**: `registry.json` says NHL's `accounting-data` ledger is empty and NBA/SOCCER/TENNIS deliveries are blocked by token scope (`docs/EDGE_FINDER_APP_CONTRACT.md §11`), but `app-data` on 2026-10-03 shows NHL 19 and SOCCER 9 delivered — the doc's "known gaps" are already partly stale.
6. Checked and found nothing: CLV/accuracy code in `src/`, `scripts/`; size limits in tests/docs; ruff config; tests for `build_app_fixture.py`; any persisted taxonomy/milestone/series output; any data branch other than `app-data`.
