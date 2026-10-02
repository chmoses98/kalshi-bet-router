# edge_finder_app_fixture.v1.json

One development fixture for a frontend developer: the REAL `edge_finder.app.v1` exports of all seven sports
(2026-10-02, trimmed to three events each, every market / model price / recommendation / thesis / wager /
settlement that references them, plus each sport's full `board`, `health`, `performance` and the chosen events'
`event_detail` documents), the router's `router_health` / `recent_deliveries`, and the sports registry.

Every object validates against the schemas in `contract/edge_finder_contract/schemas/`. A UI that renders this
file renders the live data unchanged: swap the fixture for the raw URLs in `sports_registry`.

Rebuild: `python scripts/build_app_fixture.py --out docs/fixtures/edge_finder_app_fixture.v1.json --sport MLB=<app/latest> ...`
(see the script's docstring). `coverage` in each sport block says how many of each kind the full export held.
