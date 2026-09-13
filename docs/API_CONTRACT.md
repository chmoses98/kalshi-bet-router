# Kalshi API contract used by Phase 0

This document records the exact API behaviour this code depends on, and — just as
importantly — how confident we are in each item. Assumptions that were not
verified against a live response are marked, so nobody later mistakes a
reasonable inference for a confirmed fact.

## Verification status

The build environment for this work had **outbound egress to every Kalshi domain
blocked** (`docs.kalshi.com`, `external-api.kalshi.com`, `api.elections.kalshi.com`,
`trading-api.kalshi.com` all refused at the proxy). The contract below was
therefore assembled from Kalshi's published API reference as surfaced through
search, cross-checked across several independent secondary sources, rather than
from a live request/response pair made here.

Consequences, handled in code rather than hoped away:

* The base URL is configurable (`KALSHI_API_BASE_URL`) instead of hard-coded.
* Both the integer-cent and decimal-dollar price field families are accepted.
* Both `count` and `count_fp` are recognized, and an unverified fixed-point count
  is **flagged rather than converted** to a contract quantity.
* Series tickers are exact-match only, and reliance on an unverified registry
  entry is reported as an aggregate counter.
* Every response is schema-checked, so a shape we did not anticipate fails closed
  with a clear error instead of producing a wrong number.

Running the workflow against the real account is what converts the ✅-assumed rows
below into confirmed ones.

## Base URL

```
https://external-api.kalshi.com/trade-api/v2
```

Override with `KALSHI_API_BASE_URL`. Kalshi has served the same `/trade-api/v2`
path space from more than one host (`trading-api.kalshi.com` and
`api.elections.kalshi.com` historically; `external-api.kalshi.com` is the
currently documented production root). **Not live-verified here** — if dispatch
fails with a DNS or 404 error, set the variable rather than editing code.

The `/trade-api/v2` prefix is significant: it is part of the signed message.

## Authentication

API-key authentication with RSA-PSS request signing.

| Header                     | Value                                             |
|----------------------------|---------------------------------------------------|
| `KALSHI-ACCESS-KEY`        | API key id                                        |
| `KALSHI-ACCESS-TIMESTAMP`  | Current time, **milliseconds** since epoch        |
| `KALSHI-ACCESS-SIGNATURE`  | Base64 RSA-PSS signature (below)                  |

Signed message:

```
{timestamp_ms}{HTTP_METHOD_UPPERCASE}{path}
```

* `path` starts at the API root and **includes** `/trade-api/v2`.
* `path` **excludes** the query string. This implementation strips any `?…`
  suffix defensively.
* Example: `1703123456789GET/trade-api/v2/portfolio/fills`

PSS parameters: SHA-256 digest, MGF1 with SHA-256, salt length equal to the digest
length (32 bytes). The key must be an unencrypted RSA private key.

Every retry re-signs with a fresh timestamp, because a stale timestamp is
rejected.

## `GET /portfolio/fills`

Retrieves the member's fills. **Read-only.** This is the only portfolio endpoint
this code may reach.

### Query parameters

| Parameter  | Used here | Notes                                                     |
|------------|-----------|-----------------------------------------------------------|
| `limit`    | yes       | Default 100, maximum 1000. Clamped to the remaining budget |
| `cursor`   | yes       | Opaque cursor from the previous response                   |
| `ticker`   | no        | Filter by market ticker                                    |
| `order_id` | no        | Filter by order                                            |
| `min_ts`   | no        | Unix timestamp lower bound                                 |
| `max_ts`   | no        | Unix timestamp upper bound                                 |

Phase 0 samples a **bounded recent window** (default 200 fills, ceiling 500)
rather than the account lifetime, so one audit stays inside a single rate-limit
budget and peak memory stays bounded.

### Pagination

Cursor-based. Each response carries a `cursor`; pass it as the `cursor` parameter
of the next request. An empty string or an absent cursor ends the walk.

Fail-closed guards implemented on top of that contract:

* A repeated cursor raises rather than looping forever.
* An empty page delivered with a live cursor raises.
* A missing or non-list `fills` field raises.
* A non-JSON or empty body raises — a proxy error page must never be mistaken for
  "this account has no fills".

### Fill object

Fields consumed, with the aliases accepted:

| Concept         | Fields accepted                                | Required |
|-----------------|------------------------------------------------|----------|
| Fill identity   | `fill_id` (or `id`)                             | yes      |
| Market          | `ticker` (or `market_ticker`)                   | yes      |
| Direction       | `action` — `buy` \| `sell`                      | yes      |
| Contract leg    | `side` (or `outcome_side`) — `yes` \| `no`      | yes      |
| Order grouping  | `order_id`                                      | no       |
| Trade grouping  | `trade_id`                                      | no       |
| Quantity        | `count`, else `count_fp`                        | one of   |
| Price           | `yes_price`/`no_price` (cents), or `*_dollars`  | no       |
| Liquidity role  | `is_taker`                                      | no       |
| Time            | `created_time`, else `ts`                       | no       |

Unrecognized `action` or `side` tokens are **rejected**, not defaulted —
defaulting would silently corrupt future position accounting.

`count_fp` is a fixed-point encoding whose scale factor we could not verify. No
contract quantity is inferred from it; such fills are counted under
"fills whose contract count needs schema verification".

### Historical fills

Kalshi also documents a separate historical-fills endpoint. Phase 0 does **not**
use it: a bounded recent sample from `/portfolio/fills` is sufficient to prove the
foundation, and the historical endpoint would widen the blast radius of a bug in a
read path that touches private data.

## Metadata endpoints

Fills reference only a market ticker, so each unique ticker is walked
`market → event → series`. Results are cached per ticker, per event and per series
for the lifetime of one audit, so metadata requests scale with unique markets, not
fill volume.

| Endpoint                     | Response object | Fields used                                       |
|------------------------------|-----------------|---------------------------------------------------|
| `GET /markets/{ticker}`      | `market`        | `event_ticker`, `series_ticker`, `category`, titles |
| `GET /events/{event_ticker}` | `event`         | `series_ticker`, `event_ticker`, `category`, titles |
| `GET /series/{series_ticker}`| `series`        | `tags`, `categories`, `category`, `title`, `ticker` |

Kalshi organizes contracts as **Categories → Series → Events → Markets**, and a
series carries discovery tags (Soccer, Basketball, …). `series.categories` — a
list alongside the long-standing single `category` — is a recent addition; both
are read, and `tags` entries are accepted either as bare strings or as
`{"name": …}` objects.

A missing or failed metadata lookup is **not** fatal: the market degrades to
`UNRESOLVED` and a counter is incremented.

## Rate limits

Kalshi meters requests with per-second token-cost budgets by tier (Basic is on the
order of a couple of hundred read tokens per second). A throttled request returns
**429 with no `Retry-After` and no `X-RateLimit-*` headers**, and there is no
cooldown penalty — the bucket simply refills.

Because there is no server-provided pacing signal, this client applies its own:
bounded exponential backoff with full jitter (base 0.5 s, cap 8 s, default 4
retries) on 429 and on 500/502/503/504, and on network-level failures. 401/403 and
other 4xx statuses are **not** retried. Caching metadata by ticker is the main
lever that keeps an audit well inside the budget.

`GET /account/limits` and `GET /account/endpoint_costs` expose the live budget;
Phase 0 does not call them, as they are outside the read-only allowlist.

## Open questions for live verification

1. **Base URL.** Confirm `external-api.kalshi.com` answers for this account.
2. **`count_fp` scale.** Confirm whether `count` is still present; if fills return
   only `count_fp`, determine the scale factor before Phase 1 does any
   quantity arithmetic.
3. **Price fields.** Confirm which of the cent/dollar families the live account
   returns.
4. **Series tags.** Confirm that sports series expose league-level tags (e.g.
   `MLB`, `NFL`, `College Football`) rather than only family-level ones
   (`Baseball`, `Football`). If only families are exposed, the exact-ticker
   registry carries more of the load and must be verified from live data.
5. **Series ticker registry.** Confirm the real tickers and set `verified=True`;
   prune entries that do not exist.
6. **Subaccounts.** `/portfolio/fills` documents a `subaccount` parameter. Confirm
   whether the account uses subaccounts before Phase 1 aggregates positions.
