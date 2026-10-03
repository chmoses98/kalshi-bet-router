# Edge Finder research graph — `edge_finder.app.v1`, contract 1.1.0 (additive)

The app contract (`docs/EDGE_FINDER_APP_CONTRACT.md`) gives every sport one board, one event detail, one
ledger. The research graph makes that data *navigable*: teams → players → games → opponents → metrics →
rankings → trends → markets → comparisons, with no dead ends and no server. It is published beside the
v1 files, under `app/latest/explorer/`, by the same adapters, and it is governed by the same package:
`contract/edge_finder_contract/research.py`, `packet.py`, `protocols/`.

Everything in 1.1.0 is additive. No v1 document changed shape or meaning; a consumer of 1.0.0 output
reads 1.1.0 output unchanged, and a sport that publishes no explorer is simply a sport without one.

## 1. Where it lives

```
app/latest/
  manifest.json                   v1, unchanged (+ one optional FILE_ENTRY of kind explorer_index)
  ... v1 files ...
  explorer/
    index.json                    kind explorer_index: file table (sha256, bytes, kind, entity_id), counts,
                                  team directory, players_by_team, events, windows, quality
    capabilities.json             kind capability_manifest: every capability answered (see §6)
    metrics.json                  kind metric_registry: what every metric means (see §3)
    search_index.json             kind search_index: what a static client can search (see §7)
    teams/<prt_>.json             kind entity_profile (entity_type TEAM)
    players/<prt_>.json           kind entity_profile (entity_type PLAYER)
    events/<evt_>.json            kind event_research
    rankings/<rnk_>.json          kind ranking (the full comparison universe)
    series/<ser_>.json            kind time_series
    market_history/<evt_>.json    kind market_history (every ticker of one event)
```

A client reads `index.json` first and follows `files`, `teams`, `events` and every document's `links[].path`.
It never guesses a path. Paths are app-root-relative, exactly like the v1 board's `detail_path`.

## 2. Identities

The research graph uses the v1 identities: `prt_` participants, `evt_` events, `mkt_kalshi_<TICKER>`
markets, `mp_` model prices, `wgr_` wagers. It adds:

| id | form | identity |
|---|---|---|
| metric | `met_<sport>.<slug>` | readable and stable, like a market id is its ticker; the slug is the methodology name |
| observation | `obs_` + 20 hex | sport, metric, entity, window label, split label, as_of |
| ranking | `rnk_` + 20 hex | sport, metric, universe label, window label, split label |
| time series | `ser_` + 20 hex | sport, metric, entity, x axis, split label |
| packet | `pkt_` + 20 hex | protocol, scope kind, the scope's event / tray item ids (sorted), data as-of |
| tray item | `try_` + 20 hex | ref kind, id, extra |

Same digest scheme as `ids.py` (domain-separated, length-prefixed sha256). One identity system.

## 3. Metrics mean something: the registry

Every number the explorer shows refers to a `metric_registry` entry:

`metric_id, sport, name, short_name, description, entity_type (TEAM|PLAYER|EVENT|MARKET|MATCHUP), category,
subcategory, unit, stat_type (RATE|COUNT|PERCENT|PROBABILITY|RATING|INDEX|DURATION|CURRENCY|SCORE|OTHER),
higher_is_better (bool|null), comparison_universe, supports {rank, percentile, time_series, windows, splits,
opponent_adjustment, schedule_adjustment, home_away, game_state}, windows[], splits[], source, source_version,
methodology_version, quality (§5), historical_start, update_frequency, freshness, known_limitations[],
related_metrics[], extensions`.

A metric the registry does not list cannot appear in a profile, a ranking or a series (`check_graph` refuses
the publication). Sport-specific detail goes in `extensions`; the common core never varies.

## 4. Numbers have context: observations, rankings, series, splits

**Observation** (`research.observation`): metric, entity, `value`, `adjusted_value` (only when the source
computes one), `window {kind SEASON|LAST_N|DATE_RANGE|GAME|RUN|CUSTOM, n, start, end, label}`, `split
{dimension, value}|null`, `sample_size`, `as_of`, `season`, `event_id`, `opponent_id`, `source`,
`quality_status`, and `context`:

```
context: rank, universe_size, percentile, ranking_id, universe_label, league_average, league_median,
         best_value, worst_value, best_entity_id, worst_entity_id, higher_is_better
```

So "Baltimore pass defense: 27th" is published as 27th of 32, raw value, league average and median, best and
worst with their ids, the ranking universe label ("NFL teams, 2026") and the window — and the `ranking_id`
links to the full universe.

**Ranking** (`research.ranking`): one metric, one universe, one window, one split; `entries[]` sorted by
rank (competition ranking, ties share the best rank; percentile = share of the universe beaten or tied,
100 = best), each with `value`, `adjusted_value`, `sample_size`, `percentile` and a `path` to the entity's
profile; `summary` (mean, median, min, max, stdev, best/worst entity). Entities with no value are excluded
from the universe they were not ranked in. `research.context_from_ranking` derives an observation's context
from the published ranking, so the two can never disagree (tested).

**Time series** (`research.time_series`): one metric, one entity, `x_axis GAME|WEEK|DATE|RUN|CAPTURE`,
`points[]` sorted by time, each with `x`, `t`, `event_id`, `opponent_id`, `value`, `adjusted_value`,
`rolling_value` (trailing mean, `research.rolling`), `sample_size`, `run_id`, `source`, `quality_status`,
and a `path` to the event that produced it. A chart point is clickable into its game.

**Splits** are observations with `split` set, grouped in a profile under `splits[dimension]`. The capability
manifest lists the split dimensions a sport actually stores (`split_dimensions`), with values and status.

**Market history** (`research.market_history`): per event, every ticker's capture series (`captured_at`,
bid, ask, last, volume, open interest, source), sorted.

## 5. Provenance: the quality object

Every research object carries `quality`:

```
status VERIFIED|PARTIAL|RESEARCH|UNAVAILABLE|UNKNOWN, source, source_version, methodology_version,
production (bool), generated_at, data_as_of, coverage, sample_size, missingness (0..1), limitations[]
```

`VERIFIED` = production-generated on a cadence, tested, historical rows present, semantics traceable to code.
`PARTIAL` = real but limited (and must say how). `RESEARCH` = non-production or experimental (and must say
why). The constructor refuses a PARTIAL/RESEARCH object without limitations. Every observation, series
point and projection also carries its own `quality_status`, so a RESEARCH metric stays RESEARCH inside a
VERIFIED profile, inside a packet, inside a chart.

## 6. Honesty: the capability manifest

`capabilities.json` answers every capability in the closed vocabulary (`edge_finder_contract.CAPABILITIES`,
36 entries: team_profiles, player_profiles, event_research, team_metrics, player_metrics, team_game_logs,
player_game_logs, historical_results, opponents, opponent_adjustment, schedule_strength,
recent_form_windows, usage, lineups, injuries, matchup_metrics, projection_distributions,
raw_projections, market_prices, market_price_history, advanced_stats, situational_splits, player_props,
team_props, game_markets, play_by_play, weather, venue_effects, calibration, historical_accuracy, clv,
wager_history, rankings, time_series, comparisons, search) with a status, reasons, limitations, evidence
paths, coverage, since, the metrics / windows / splits involved. An unanswered capability is published as
UNKNOWN, never omitted.

`research.check_capabilities` runs at publish and at verify: VERIFIED/PARTIAL need evidence paths that
exist (and PARTIAL needs limitations); UNAVAILABLE is refused when files of the proving kind are published
(e.g. UNAVAILABLE rankings with ranking files). The UI asks the manifest, not the file system.

## 7. Search without a server

`search_index.json` lists teams, players, events, metrics, rankings and series with `label`, `secondary`,
`aliases[]`, normalised `tokens[]` (lowercase, deduplicated, sorted), `path`, and context (team, position,
league, season). A static client does fuzzy matching locally; every entry's path is verified to exist.

## 8. Lazy, static, cacheable

One small `index.json`; everything else is fetched on navigation. Per-event market history keeps the
large capture series out of profiles. `research.tree_bytes` reports bytes per directory for budgeting.
Files are written compact (sorted keys, no whitespace), the index indented. Same inputs produce
byte-identical trees (`research.digest_tree`, tested).

## 9. Atomic publication, last-known-good

`research.publish_explorer` validates every document, builds the index, checks the graph (no dead links,
every metric registered, every context ranking published, series and histories in time order, rankings in
rank order, search entries resolvable) and the capability manifest, then writes to a staging directory
inside the app root and swaps files in with `index.json` last. Any problem raises `ExplorerError` and
leaves the previous tree untouched (tested). `research.verify_explorer` re-checks a published tree;
`python -m edge_finder_contract verify-explorer <app_root>` does the same from the shell.

Since 1.1.1, `publish.publish` (the v1 publisher) never prunes `explorer/`: the explorer is owned by
`research.publish_explorer` alone. A research export that fails after a successful v1 publish therefore
leaves the last-known-good explorer in place; its index names the v1 run it was built against
(`base_manifest_run_id`).

Every explorer file carries the run id, so a rebuild rewrites the whole tree (27–36 MB for NHL, NBA and
soccer). Workers that republish the v1 payload every few minutes should gate the explorer with
`research.refresh_due(app_root, now=..., min_interval_seconds=...)`: it is due when nothing is published,
when the v1 event set changed (new games need research documents), or when the tree is older than the
interval. Skipping a rebuild keeps the previous tree.

## 10. The handicap packet (`packet.py`)

The app hosts no model. COPY FOR CHATGPT produces one deterministic package built from the published root:

```
packet_id, packet_version, protocol {id, version, extends, principles, steps, outputs_required, forbidden,
evidence_weights}, scope {GAME|SLATE|CUSTOM, event_ids, window, label}, sports, generated_at, data_as_of,
warning, user_focus[], events[] (label, start, status, venue, context notes, repo theses flagged research),
evidence[] (per entity: observations with window/split/rank/universe/league average/quality, availability,
recent series points with opponent and event), markets[] (EVERY market in scope: bid/ask/mid/last,
captured_at, freshness), model_evidence[] (fair vs market, edge, projection, research_only, authority,
freshness), repo_recommendations[] (evidence, not instructions), quality {sources, market_freshness,
model_freshness, research_only_items, missing, capabilities}, budget {max_chars, chars, truncated}
```

Rules: deduplicate by id; focus (tray) entities first, then event participants and their players; include
full current market coverage for the scope; keep price timestamps; mark RESEARCH items; list missing data
(no profile, no event research, unresolved tray item, no markets) rather than inventing it; trim in a fixed
order (recent points, then observations beyond 24 per entity, then repo recommendations) and record what
was trimmed — markets and model evidence are never trimmed. `render_text` is the clipboard form.

Since 1.1.1 the budget is measured on that clipboard text, not on the JSON, and the text is compact:
markets that differ only by their line (spread, total, team-total and player-prop ladders) render as one
line listing every rung with its ticker suffix, bid/ask in cents and the model's fair price; singleton
markets of one series render as one board line; the capture time is stated once and repeated only where it
differs. Every market in scope still appears (tested). On the real NFL week-4 publication this took the
largest single-game packet from 229k to 63k characters (median 23k) with all 756 markets kept. When
evidence still overflows, observations are capped at 8 per player and then 12 per entity; research-tray
items keep theirs. Packet markets gained `period`, `side`, `line` and `threshold` (additive).

Determinism: the same request against the same publication yields the same packet (tested); `packet_id`
depends on the protocol, the scope and `data_as_of`, not on the budget.

## 11. The protocol (`protocols/`)

`edge_finder.handicap.core.v1` and the sport extensions `edge_finder.handicap.{nfl,mlb,nba,nhl,soccer,
tennis,cfb}.v1` (each `extends` the core and adds `sport_notes`). Principles: projections are evidence,
never automatic bets; multiple game scripts; counter-cases; opponent-adjusted information where VERIFIED;
inspect every included market; compare expressions; independent vs correlated edges; independent fair
probability; bet-up-to pricing; PASS is an answer; RESEARCH data is lower-confidence; never invent inputs;
staking and bankroll are out of scope. `packet.protocol_for_sport` picks the extension.

## 12. The research tray (`packet.tray`, `packet.resolve_tray`)

Client-side state (local storage): `{item_id, ref_kind TEAM|PLAYER|EVENT|METRIC|RANKING|SERIES|CHART_POINT
|MARKET|PROJECTION, sport, id, extra {series_id, x, metric_id, market_id, event_id}, note, added_at}`.
References only, never payloads. The packet builder resolves each against the publication, reports the
unresolved ones under `quality.missing`, and tells the model: the user is specifically investigating these
items; evaluate them seriously; do not assume they are good bets; compare against alternative expressions
and opposing evidence.

## 13. Adding a sport's explorer

1. Register metrics (`research.metric`) only for datasets the audit rated VERIFIED/PARTIAL/RESEARCH, with the
   matching quality object. Never a placeholder metric.
2. Build rankings from stored values (`research.ranking`), then observations with
   `context_from_ranking`.
3. Build series from stored per-game / per-run rows; link points to events.
4. Build profiles and event research with `links` to everything above; build market history from captures.
5. Answer every capability (`research.capability`) with evidence paths; leave the rest UNAVAILABLE.
6. `research.publish_explorer(...)` after the v1 `publish.publish(...)`; list `explorer/index.json` in the v1
   manifest if the exporter rebuilds it (optional).
7. Add the sport's real-data smoke test: load the committed inputs, publish to a temp root, assert
   `verify_explorer` is clean and the capability statuses match the audit.

## 14. Why no v2

The graph adds kinds, ids and files; it changes no v1 field. A v2 boundary would be required only if
identities, probability orientation or the manifest's meaning changed. None did.
