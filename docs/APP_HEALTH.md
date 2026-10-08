# App-facing router health (contract `edge_finder.app.v1`)

The Edge Finder app reads the router's state from the orphan `app-data` branch:

| file | kind | what |
|---|---|---|
| `app/latest/router_health.json` | `router_health` | overall status, the router's own health token, last delivery / settlement run, per-sport routing status, counts |
| `app/latest/recent_deliveries.json` | `recent_deliveries` | a rolling window (200) of per-sport delivery and settlement outcomes with failure details |
| `app/latest/sports_registry.json` | `sports_registry` | where every sport publishes its app output (copied from the contract) |

`publish-router-health.yml` runs every 15 minutes with **no Kalshi credential and no downstream token**. It
reads this repository's own PUBLIC workflow run logs (`actions: read`) — the `HEALTH=` token, the
`ROUTER_STATUS_JSON=` line the `deliver` CLI prints, one `ROUTER_DELIVERY_JSON=` line per destination, the
`::error::<SPORT>:` annotations — and restates them through the contract (`scripts/publish_router_health.py`,
tested in `tests/test_router_health_publisher.py`). By construction it publishes nothing that is not already
public; it also scrubs anything ticker-, key- or amount-shaped as a second line of defence.

`overall_status`: UNAVAILABLE (no run), STALE (last poll older than 4 h), DEGRADED (a delivery or settlement run
failed, a destination failed or refused a settlement, or the router is BLOCKED), HEALTHY.

Since contract 1.3.0 the two PENDING states are told apart from failure and never degrade the router on their own:
a sport `AWAITING_MANUAL_MERGE` (wagers delivered to an open proposal of an observation-period destination) and a
sport whose `settlement.status` is `WAITING_FOR_PARENT_WAGER` (settled rows withheld because their wager is on
that proposal). Both are counted (`awaiting_manual_merge`, `waiting_for_parent_wager`) and named in `warnings`.
The publisher reads them from the `ROUTER_RECONCILE_JSON=` line `scripts/reconcile_delivery.py` prints per
destination and kind, and from `ROUTER_SETTLEMENT_PARENTS_JSON=` (`scripts/settlement_parents.py`).

`router_health_state` is the router's own vocabulary (`healthy_no_op`, `delivered`, `not_routable`, `deferred`, `blocked`). The sport table says, per sport, whether
it is classified and profiled, where it goes, whether it auto-merges, and what the last run did.
