# Edge Finder research graph (phase 2) — handoff, 2026-10-03

## A. Verdict

**Shipped for all seven sports.** Every sport repo has merged an explorer adapter on contract 1.1.1. MLB, CFB and tennis have already published a production explorer; the other four publish on their next production cycle, which now runs the merged code. Owner decisions remain on the CFBD licence and on repository growth (section H).

Each explorer sits beside the unchanged v1 app payload under `app/latest/explorer/`:
- entity profiles, event research, rankings with full comparison universes and time series;
- per-event market history, a metric registry and a search index;
- a capability manifest that says, for each of 36 capabilities, whether the sport's committed data supports it.

The contract refuses any publication whose files contradict that manifest. A deterministic "copy for ChatGPT" handicap packet can be built from any published root. No model, threshold, staking or bet-authority behaviour changed. Data a repository does not have is published as UNAVAILABLE with the reason, never invented.

## B. Repository matrix

| Repo | PRs | Merged as | Tests (verified this phase) | Production explorer |
|---|---|---|---|---|
| kalshi-bet-router | #106 contract 1.1.0, #107 contract 1.1.1 | 7e9757a, 00fb2b0 | contract suite and full suite green; CI 3.11 + 3.12 | n/a (authors the contract) |
| edge-finder-api (MLB) | #266 explorer, #267 v1 exporter fix | ad52913, 7842f3d | CI full suite green (12,672+ tests) | published 2026-10-03 22:59 UTC, verifies clean |
| cfb-edge-finder | #84 | df40565 | full suite 3,443 passed | published 2026-10-03 22:57 UTC, verifies clean |
| Tennis-Edge-Finder | #15 explorer, #16 contract 1.1.1 | 63b6739, 4bd491d | full suite 659 passed | published on tennis-data |
| nfl-edge-finder | #92 | 303c430 | full suite 2,752 passed | next RUN NFL cycle |
| NHL-edge-finder | #21 | e51f377 | full suite 523 passed | next capture-worker cycle (queued on merged main) |
| nba-edge-finder | #20 | 4800ab3 | full suite 701 passed | next capture-worker cycle |
| soccer-edge-finder | #34 | f3f8146 | full suite 511 passed (real archive) | next archive publish |

## C. What the audits found

The full reports are in `audits/` beside this file. One line per sport:

- **MLB:** the richest repo. The EdgeLab corpus holds 757k observations, 247k settlements and 304k CLV quotes, beside 63 slate dates with full model inputs, five seasons of team and pitcher game logs, and Statcast for 643 games. Per-ticker price history is shallow (median 3 points).
- **NFL:** deep, verified market history, with quotes every ~12 minutes since 2026-09-04. Opponent-adjusted ratings exist only as the current packet snapshot.
- **NHL:** the best player data (five seasons of official per-player game rows), MoneyPuck team xG, a 10-minute board capture with order books, and per-ticker model-probability history. There is no opponent adjustment anywhere.
- **NBA:** three seasons of ESPN game logs and shots, plus 653k hourly candles. The market beats the model in all eight families.
- **SOCCER:** a seven-day production evidence pipeline, a 622-team registry and ESPN results. Team ratings are never persisted, and there are no player metrics.
- **TENNIS:** per-player rating states for 50k players, 23 days of quote capture and a hash-chained prediction ledger. The canonical match table is on no branch.
- **CFB:** the live surface is market inventory, market history and a 104-wager ledger. Team stats are a frozen 2014–2025 CFBD snapshot, and CFBD's licence forbids redistributing the raw data.
- **ROUTER:** infrastructure only. Before this phase, `price_history` was empty in every sport.

## D. Contract 1.1.1 (additive to edge_finder.app.v1)

- **New kinds:** 12, each with a generated schema: explorer_index, capability_manifest, metric_registry, entity_profile, event_research, ranking, time_series, market_history, search_index, handicap_packet, handicap_protocol and research_tray.
- **New ids:** metrics, observations, rankings, series, packets and tray items, all on the existing digest scheme.
- **`research.py`:**
  - publishes the explorer atomically;
  - refuses dead links, unregistered metrics and capability claims that the published files contradict;
  - keeps the last good tree when a publish fails;
  - provides `refresh_due`, which gates rebuilds.
- **`packet.py`:** builds GAME, SLATE and research-tray packets deterministically, with every market in scope. The budget is measured on the text the user copies.
- **`protocols/`:** one core handicapping protocol and seven sport extensions.
- **CLI:** `validate`, `verify-explorer`, `packet` and `protocols`.
- **Design guide:** `docs/EDGE_FINDER_RESEARCH_GRAPH.md`.
- **Backward compatibility:** no v1 field changed, and a sport with no explorer is valid (tested).

## E. What 1.1.1 fixed after real-data runs

1. **Packet size.** One NFL game packet was 229k characters as text and 564k as JSON, against a 60k budget. Grouping each betting ladder onto one line brings the largest to about 63k and the median to 23k, with all 756 markets kept.
2. **Explorer deleted on every v1 publish.** The v1 publisher pruned `explorer/`, so a failed research export left none. It now never touches `explorer/`.
3. **Explorer index over budget.** CFB's index was 519 KB written indented, against a 300 KB budget. It is now written compact.
4. **Git churn.** Every file carries the run id, so each rebuild rewrites the whole tree. Rebuilds are now gated: hourly for NHL, NBA and soccer, every three hours for MLB and CFB, and immediately whenever the v1 events change.

## F. Production proof (committed trees on main, re-verified with the contract)

| Sport | Run | Explorer | Statuses (VERIFIED / PARTIAL / RESEARCH / UNAVAILABLE) | Single-game packet |
|---|---|---|---|---|
| MLB | run_8ca18aa71ed7c8fa049b | 13 MB; 30 teams, 81 players, 4 events, 76 rankings, 234 series, 59 metrics | 21 / 10 / 2 / 3 | 49.6k characters, nothing missing |
| CFB | run_9a1530ebb0dff7e9d720 | 36 MB; 266 teams, 295 events and market histories | 5 / 5 / 0 / 26 (CFBD off) | 9.3k characters, nothing missing |

For both sports:
- `verify_explorer` and the v1 verifier report no problems.
- The explorer was built from the same run as its v1 manifest.
- No secret-shaped string appears anywhere in the tree.

Real-data runs by the adapters before merge (measured on committed data):

| Sport | Explorer | index.json | Largest single-game packet |
|---|---|---|---|
| NFL, week 4 | 21.9 MB | 142 KB | 63.4k characters |
| NHL | 26.9 MB | 244 KB | 34.2k characters |
| NBA | 29.2 MB | 204 KB | 50.9k characters |
| SOCCER | 35.7 MB | 211 KB | 15.5k characters |

## G. Fixed in passing

- **MLB v1 export, #267.** It had been broken since 2026-10-03 19:04 UTC.
  - **Cause:** `handicapping_card/latest.json` only points to the card, and its `bettingEligibleGames` is a count. The exporter read it as the card itself.
  - **Before it crashed:** while the count was 0, the exporter silently ignored real-money eligibility.
  - **Crash:** once the count reached 2, every run failed.
  - **Fix:** the exporter now follows the pointer to the dated card. The 22:52 UTC production run succeeded.
  - **Visible effect:** the card's two eligible markets now show as RECOMMENDED / MANUAL, as the v1 mapping specifies.
- **CFB team names.** Kalshi titles name ALBY both "University at Albany" and "University". The profile keeps the more complete name.
- **MLB explorer test.** The empty-slate case is pinned to 2026-10-02, because 2026-10-03 gained games after the test was written.

## H. Owner decisions

1. **CFBD licence.** CFBD-derived team stats are off by default. Setting the job env `INCLUDE_CFBD: "true"` in `app-export.yml` turns them on. The index then measures 418 KB, so its 300 KB budget needs revisiting first.
2. **Repository growth.** Gating changes how often the explorer is rewritten, not how much. A rebuild still writes 13–36 MB. If that matters, the options are:
   - move the explorer to its own data branch;
   - shard the tree;
   - drop the per-file run id, which would be a contract change.
3. **NBA quote defect.** `workflows/simulate.py` strips the quote fields, so NBA model prices are missing. Every affected limitation names the defect; it is not fixed here.
4. **MLB historical dates.** The v1 exporter cannot export dates before about 2026-08-16, which store cents instead of probabilities. Production always exports the newest date.

## I. For the app

- **Entry point:** read a sport's `explorer/index.json`, then navigate only by `links[].path`; paths are never guessed.
- **Gating features:** use `capabilities.json` to decide what each sport can show.
- **Search:** `search_index.json` drives local search.
- **Research tray:** the tray is client-side state, and `packet.build(scope_kind="CUSTOM", tray_doc=...)` resolves it.
