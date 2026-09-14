# Kalshi API contract used by the bet router

This document records the API behaviour this code depends on, and how each item
was verified. Assumptions that remain unconfirmed are marked, so nobody later
mistakes a reasonable inference for an established fact.

## Verification status

> **Acceptance run (200 fills, corrected model).** After the price and direction
> corrections, the same bounded window normalized **200 of 200 fills, 0
> rejected**, with **0 canonical-vs-legacy conflicts** — down from 194 rejected.
> Accounting then ran over real data for the first time: 164 orders, **25 of them
> partially filled**, 155 markets, 200 transitions, complete cost basis and
> complete exchange fees on all 155 episodes, and an exact account-level fee
> total. That is the evidence the corrections below rest on.

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
| **`yes_price_dollars` + `no_price_dollars` are complementary legs** | **200/200 pairs summed to exactly `1.00`** |
| **`outcome_side` reports the CONTRACT, not the exposure** | **`outcome_side == side` on 200/200 fills, sells included** |
| **`book_side` tracks the contract, not the buy/sell verb** | **31 buy-NO and 2 sell-NO fills all reported `ask`** |
| `fee_cost` is a decimal **dollar string** | 200/200 fills carried it as a string; 0 as an integer |
| `subaccount_number` is present on current fills | 200/200 present and in range; 0 absent, 0 malformed |
| `created_time` is present on current fills | 200/200; 0 fills needed the `ts` fallback |

### Disproven by the live run

* *"Fills carry an integer `count`."* False for 100% of fills. See fixed point below.
* *"Series tags/categories identify the league."* Not sufficient: classification
  reached only 28% with **zero** `OTHER` verdicts across 154 markets, which is the
  signature of no league-level series metadata being recognized at all.
* *"A fill carries one unified execution price, identical in `yes_price_dollars`
  and `no_price_dollars`."* **False.** The two fields agreed on only 6 of 200
  fills, and all 6 were at even odds — where a complementary pair coincides.
  Zero fills were equal anywhere else. They are leg prices, and reading them as
  a unified price rejected 194 of 200 fills and inverted the sign of realized
  P&L on NO-side trades.
* *"`outcome_side` and `book_side` carry the same bit in two vocabularies, so new
  integrations can read only those two fields."* The first half holds — but both
  fields carry the **contract**, so the pair cannot distinguish a buy from a
  sell. Only the deprecated `action` does. Reading `outcome_side` as exposure
  made the 2 sell-NO fills look self-contradictory and rejected them.

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

### Value domains (not just syntax)

An exactly-parsed decimal is not automatically a valid fill. `models.py` enforces
the domains below; `fixedpoint.py` stays purely about decimal syntax so it does
not become a misleading place to look for financial rules.

| Field | Rule | Basis |
|---|---|---|
| `count_fp` / legacy `count` | must be **> 0** | a fill that executed moved a positive number of contracts; direction lives in `action`/`side`, so a signed quantity would mean the schema is not what we think it is |
| `yes_price_dollars` / `no_price_dollars` | must be **> $0 and < $1** | a contract trades strictly between $0 and $1 and *settles* at $0 or $1; classic range is $0.01–$0.99 at whole-cent ticks, and sub-penny markets taper to deci ($0.001) / centi ($0.0001) ticks below $0.01 and above $0.99 |
| legacy `yes_price` / `no_price` (cents) | must be **> 0 and < 100** | the integer-cent equivalent of the same interval |

The price bounds are written as an **open interval** rather than as a
tick-derived min/max, so a future tick change cannot make this reject a real
fill, while a settlement value, a zero, or a negative is still refused.

Domain errors never echo the offending value — a contract count and a price are
private account data, and the error text reaches a public log.

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

## `GET /portfolio/settlements`

Verified against 755 live rows. Fields observed on **every** row:

| field | meaning | unit |
|---|---|---|
| `ticker`, `event_ticker` | the settled market | — |
| `market_result` | `yes` \| `no` (scalar exists exchange-wide; unobserved here) | — |
| `value` | **the market's** settlement price for a YES contract | **integer cents** (`100` or `0`) |
| `revenue` | **the member's** payout | **integer cents** |
| `yes_count_fp`, `no_count_fp` | contracts settled on each leg | fixed-point |
| `yes_total_cost_dollars`, `no_total_cost_dollars` | cost basis per leg | **dollars** |
| `fee_cost` | settlement fee | dollars |
| `settled_time` | for ordering | timestamp |
| `exchange_index` | — | — |

### Units are mixed within one row

`yes_total_cost_dollars` is dollars, and `revenue` and `value` are **cents**, in
the same object. Kalshi's naming is the tell — dollar-valued fields carry a
`_dollars` suffix and these two do not — and the data confirms it: of 755 rows,
355 paying settlements sit at cents par and **none** at dollar par, while every
one of the 359 non-trivial `value` readings is exactly `100`.

Reading cents as dollars would misstate every settled payout by 100x, silently,
because the wrong numbers are still well-formed decimals.

### `value` is about the market; `revenue` is about the member

These are not two views of one number, and treating them as one makes ordinary
rows look broken. A member holding NO in a market that settled YES is unpaid
while `value` is `100`; a member holding NO in a market that settled NO is paid
while `value` is `0`. The live arithmetic closes exactly on that reading:

```
YES markets 359 + NO markets 396        = 755
member wins (359 - 62) + 58 = 297 + 58  = 355   (= rows with non-zero revenue)
```

### A settlement is a complete accounting event

It carries quantity, per-leg cost basis, fee, payout and timestamp. So replaying
one never requires inventing a closing price — the same rule this system already
applies to fees.

### A settled market leaves `GET /portfolio/positions`

It does **not** appear there with a zero quantity. The account returned **0**
position rows against 155 replayed markets and 755 settlements. A replayed
ticker missing from positions is therefore only evidence of missing history when
no settlement explains it.

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

Both fields are documented as `string | null`, and that contract is enforced
explicitly rather than by duck-typing:

The governing rule: **only an explicit null or an absent field may fall through**
to weaker evidence. A field that is present but unusable is malformed — an
authoritative field may never quietly demote itself into weaker evidence.

| Value | Treatment |
|---|---|
| field absent | valid absence; falls through to weaker evidence |
| `null` | valid absence; falls through |
| non-empty string | valid value |
| empty string `""` | **malformed** |
| whitespace-only string `"   "` | **malformed** |
| any other non-null type (`123`, `12.5`, `[]`, `{}`, `true`) | **malformed** |

Every malformed case fails closed to `UNRESOLVED` with reason
`malformed_event_metadata`, evaluated *before any evidence is gathered*, so it can
never be rescued by L4 series metadata or the L5 registry. Nothing is coerced, and
the error names the field and the shape problem only — never the offending value,
which reaches a public log.

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

**Ownership collisions fail closed.** If one normalized competition name appears
under more than one sport, no sport owns it: `sport_for_competition` returns
`None`, the classifier fails closed, and only an aggregate collision count is
reported. Claimants are collected before any assignment is made, so the outcome
cannot depend on iteration order.

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

**Conflicts fail closed.** If one event ticker surfaces under more than one
competition sweep, the index keeps neither: the event is marked conflicted,
yields no competition, and the classification stays `UNRESOLVED`. Once conflicted
an event stays conflicted, so sweep order cannot change a verdict. Only an
aggregate conflict count is reported.

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
2. **`filters_by_sport` inner object shape.** **Answered, and repaired.** Three
   live runs reported `sports in taxonomy: 22` with `competitions in taxonomy: 0`
   and `taxonomy entries skipped: 0`. The shape probe then named the cause:

   ```
   inner keys observed: competitions:object, scopes:list[str]
   ```

   `competitions` was present all along, as an **object** rather than a list, so a
   reader that only walked lists found nothing — while `scopes`, a list of
   strings, parsed correctly. Both shapes are now read. Note that L2 was therefore
   contributing **zero** classifications from Phase 0.1 until this repair.
3. **Event metadata envelope.** Top-level vs wrapped; both are accepted.
4. **Coverage of `competition` on older events.** The `events with non-null
   competition` counter measures this directly on the next run.
5. **Price field family in practice.** **Answered.** 200/200 fills used
   `*_price_dollars`; 0 used legacy integer cents. The pair is complementary (see
   "Confirmed by the live run").
6. **Subaccounts.** `/portfolio/fills` documents a `subaccount` parameter; unused
   and unexamined. Live fills all carried `subaccount_number` in range, and only
   one distinct subaccount was observed, so the multi-subaccount path is still
   untested against real data. Must be settled before Phase 1 aggregates positions.
7. **Series ticker registry.** Still unverified (`verified=False` throughout). It
   is now the last resort and can never override stronger evidence, and every
   audit reports how many classifications leaned on it.
8. **Buy/sell after `action` removal.** Kalshi deprecated `action`/`side` on
   2026-05-14 with removal not before 2026-05-28. On live evidence `action` is the
   **only** field distinguishing a buy from a sell, so its removal would make
   exposure unobservable from a fill alone. Open, and material: the router refuses
   such a fill rather than guessing. The sell sample is small (2 fills), so this is
   treated as fail-closed guidance rather than a proven exchange-wide rule, and it
   needs confirmation against a larger sell population (see `GET /historical/fills`).
