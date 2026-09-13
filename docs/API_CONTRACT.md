# Kalshi API contract used by the bet router

This document records the API behaviour this code depends on, and how each item
was verified. Assumptions that remain unconfirmed are marked, so nobody later
mistakes a reasonable inference for an established fact.

## Verification status

Three sources back this document:

1. **Kalshi's published API reference** (`docs.kalshi.com`), consulted for each
   endpoint below.
2. **The first live credentialed audit**, run `34784811480` on main
   `7c66dbc`, 200 real fills. This is empirical proof, not inference.
3. Cross-checks against independent secondary documentation.

The build environment has outbound egress to every Kalshi domain blocked, so no
request was made from the development container; the live evidence comes from the
GitHub Actions run, which prints aggregate counts only.

### Confirmed by the live run

| Item | Evidence |
|---|---|
| Base URL `https://external-api.kalshi.com/trade-api/v2` | 331 requests, 0 transport failures |
| RSA-PSS signing scheme and header names | 0 auth failures across 331 signed requests |
| `GET /portfolio/fills` returns member fills | 200 fills returned |
| Cursor pagination | 2 pages walked, 0 duplicates, no cursor fault |
| `market -> event -> series` resolution | 154 unique markets, 0 lookup failures |
| **`count_fp` has replaced integer `count`** | **200/200 fills carried no usable `count`** |

### Disproven by the live run

* *"Fills carry an integer `count`."* False for 100% of fills. See fixed point below.
* *"Series tags/categories identify the league."* Not sufficient: classification
  reached only 28% with **zero** `OTHER` verdicts across 154 markets, which is the
  signature of no league-level series metadata being recognized at all.

## Base URL

```
https://external-api.kalshi.com/trade-api/v2
```

Overridable with `KALSHI_API_BASE_URL`. The `/trade-api/v2` prefix is part of the
signed message.

## Authentication

| Header | Value |
|---|---|
| `KALSHI-ACCESS-KEY` | API key id |
| `KALSHI-ACCESS-TIMESTAMP` | Current time in **milliseconds** since epoch |
| `KALSHI-ACCESS-SIGNATURE` | Base64 RSA-PSS signature |

Signed message: `{timestamp_ms}{HTTP_METHOD_UPPERCASE}{path}` where `path` starts
at the API root, **includes** `/trade-api/v2`, and **excludes** the query string.
PSS: SHA-256 digest, MGF1-SHA256, salt length 32. Unencrypted RSA key. Every retry
re-signs with a fresh timestamp.

## Fixed-point representation (Q1-2026 migration)

This is the single most consequential schema fact for this system.

Kalshi migrated prices and quantities from integers to **decimal strings**, and
**removed the legacy integer fields**:

| Field | Form | Meaning |
|---|---|---|
| `count_fp` | decimal string, e.g. `"10.00"` | **10 contracts.** Fractional contracts are representable. |
| `yes_price_dollars` | decimal string, e.g. `"0.6500"` | price in **dollars**, not cents |
| `no_price_dollars` | decimal string | price in dollars |

**`count_fp = "10.00"` represents ten contracts**, not one thousand and not ten
hundredths. The value is a plain decimal count of contracts; the `_fp` suffix
marks the *encoding* (a fixed-point decimal string), not a scaling factor.

Some markets quote sub-penny ticks as fine as **$0.001**, so an integer-cent field
cannot represent every price. That is why the `_dollars` fields exist and why
reading prices from the legacy integer fields is lossy.

### Internal representation rule

`src/kalshi_router/fixedpoint.py` parses every one of these into
`decimal.Decimal` **from its string form**. Binary floating point is never used
for a quantity or a price. A JSON number is tolerated (routed through `repr` so no
binary rounding is baked in) but counted in
`fixed-point fields arriving as JSON numbers`, so a schema drift toward floats is
visible in the next audit rather than silent.

## `GET /portfolio/fills`

Read-only. The only portfolio endpoint this code may reach.

Query parameters used: `limit` (default 100, max 1000; clamped to the remaining
budget) and `cursor`. Also documented but unused here: `ticker`, `order_id`,
`min_ts`, `max_ts`, `subaccount`.

Fields consumed:

| Concept | Fields accepted | Required |
|---|---|---|
| Fill identity | `fill_id` (or `id`) | yes |
| Market | `ticker` (or `market_ticker`) | yes |
| Direction | `action` — `buy` \| `sell` | yes |
| Contract leg | `side` (or `outcome_side`) — `yes` \| `no` | yes |
| Order grouping | `order_id` | no |
| Quantity | `count_fp`, else legacy `count` | one of |
| Price | `yes_price_dollars`/`no_price_dollars`, else legacy cents | no |
| Liquidity role | `is_taker` | no |
| Time | `created_time`, else `ts` | no |

Unrecognized `action` or `side` tokens are rejected, not defaulted.

Pagination guards: a repeated cursor raises; an empty page with a live cursor
raises; a missing or non-list `fills` raises; a non-JSON or empty body raises —
a proxy error page must never look like "this account has no fills".

## Classification metadata endpoints

### `GET /events/{event_ticker}/metadata` — the primary signal

Returns `image_url`, `featured_image_url`, `market_details`, `settlement_sources`,
and critically:

* **`competition`** — the league or tournament, e.g. `"Pro Football"`,
  `"College Football"`, `"Pro Baseball"`, `"ATP Madrid"`.
* **`competition_scope`** — the scope of the contract, e.g. `"Game"`. No exhaustive
  enum is published, so this is treated as supporting/diagnostic evidence only.

`competition` is what separates **Pro Football** from **College Football**, which
is exactly the NFL/CFB ambiguity the Phase 0 classifier had to refuse 144 times.

The documented response carries these at the top level; a wrapped envelope is
also accepted, since the envelope is the one part of this route not confirmed
against a live response. A non-object response fails closed. A **null**
`competition` is a valid shape (many events are not sports) and falls through to
weaker evidence; a **present but unrecognized** competition fails closed.

### `GET /search/filters_by_sport` — the taxonomy

```json
{"filters_by_sports": {"<sport>": { "competitions": [...], "scopes": [...] }},
 "sport_ordering": ["<sport>", ...]}
```

Public catalogue data: fetching it discloses nothing about the account, and it is
fetched **once per audit** and cached in memory only.

It answers "which sport owns this competition?", which is what lets a tennis
market whose competition is a tournament name (`ATP Madrid`, `US Open Men
Singles`) resolve to TENNIS without hard-coding every tournament — and what tells
us an unfamiliar competition sits under Football and must therefore fail closed.

The exact inner shape of each sport's filter object is not fully pinned down in
the published reference, so parsing is structural rather than positional: any
competitions-like or scopes-like list is read, entries may be bare strings or
objects carrying a name field, and unrecognized shapes are skipped and counted.
A malformed top-level envelope raises.

### `GET /milestones` — the backstop

Filters: `category` (`Sports`, `Elections`, `Esports`, `Crypto`), `competition`
(documented examples: *Pro Football*, *Pro Baseball*, *Pro Basketball (M)*,
*Pro Hockey*, *College Football*), `type` (`football_game`, `baseball_game`,
`basketball_game`, `hockey_match`, …), plus cursor pagination.

Each milestone carries `primary_event_tickers` / `related_event_tickers`, linking a
real-world fixture to Kalshi event tickers.

**Privacy property**: the index is built by asking Kalshi for the *public*
milestone list of each competition we care about, then looking the account's event
tickers up **locally**. The account's tickers are never sent to this endpoint.

Scope: a bounded backstop, not a primary signal. It runs only when markets remain
unresolved for a reason milestones could repair, sweeps only the three
competitions that map onto a supported league, and is capped at 24 requests per
audit. Tennis is intentionally not swept — its competitions are per-tournament, so
there is no small fixed set to enumerate, and tennis resolves at the sport level.

### `GET /markets/{ticker}`, `GET /events/{ticker}`, `GET /series/{ticker}`

Retained for `event_ticker`, `series_ticker`, and the series `tags` / `categories`
/ `category` / `title` used as level-4 evidence.

## Rate limits

Token-cost budgets per tier (Basic is on the order of a couple of hundred read
tokens per second). A throttled request returns **429 with no `Retry-After` and no
`X-RateLimit-*` headers**, and there is no cooldown penalty.

With no server-provided pacing signal, this client applies bounded exponential
backoff with full jitter (base 0.5s, cap 8s, 4 retries) on 429 and 5xx and on
network failures. 401/403 and other 4xx are not retried. Caching metadata per
market, per event and per series is the main lever keeping an audit inside budget;
the first live run issued 331 requests for 200 fills across 154 markets.

## Remaining open questions

1. **`competition_scope` value set.** No published enum. Observed: `"Game"`.
   Treated as supporting evidence only, and its presence is reported as a count.
2. **`filters_by_sport` inner object shape.** Parsed structurally; the next live
   run's `competitions in taxonomy` counter will show whether it was read correctly
   (a zero there with a non-zero sport count means the shape differs).
3. **Event metadata envelope.** Top-level vs wrapped; both are accepted.
4. **Coverage of `competition` on older events.** The `events with non-null
   competition` counter measures this directly on the next run.
5. **Price field family in practice.** The next run reports
   `price from *_price_dollars` vs `price from legacy integer cents`.
6. **Subaccounts.** `/portfolio/fills` documents a `subaccount` parameter; unused
   and unexamined. Must be settled before Phase 1 aggregates positions.
7. **Series ticker registry.** Still unverified (`verified=False` throughout). It
   is now the last resort and can never override stronger evidence, and every
   audit reports how many classifications leaned on it.
