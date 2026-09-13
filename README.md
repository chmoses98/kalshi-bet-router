# kalshi-bet-router

Read-only Kalshi fill ingestion, sport classification, and (eventually) routing to
per-sport betting ledgers.

> **NO AUTOMATIC DOWNSTREAM ROUTING EXISTS YET.**
> Phase 0 proves the foundation only. Nothing in this repository writes to
> `edge-finder-api`, `nfl-edge-finder`, `cfb-edge-finder`, the tennis repository,
> or anywhere else. There is no ledger, no canonical wager, no settlement, and no
> downstream trigger.

---

## 1. System purpose

The long-term objective is a single router that takes executed wagers from a
Kalshi account and delivers each one to the repository that owns that sport:

| Sport  | Destination repository (future)   |
|--------|-----------------------------------|
| MLB    | `chmoses98/edge-finder-api`       |
| NFL    | `chmoses98/nfl-edge-finder`       |
| CFB    | `chmoses98/cfb-edge-finder`       |
| Tennis | the corresponding tennis repo     |

Routing is worthless if classification is unreliable, so Phase 0 builds and
proves classification first.

## 2. Phase 0 scope

Phase 0 answers exactly one question:

> Can this public repository securely authenticate to the owner's Kalshi account,
> retrieve actual executed fills, resolve enough market metadata to classify those
> fills among MLB / NFL / CFB / Tennis / Other, fail closed on ambiguity, and
> expose only privacy-safe aggregate diagnostics?

**In scope:** authentication, bounded fill retrieval, pagination, de-duplication,
public metadata resolution, explainable sport classification, aggregate-only
reporting, a local-only sensitive diagnostic mode, and a test suite built on
synthetic fixtures.

**Out of scope — deliberately not implemented (see §7).**

## 3. The security boundary

This repository is **public**. The owner's betting activity is **private**. The
boundary between them is the core design constraint.

| Public                                          | Private (never committed, never logged)                     |
|-------------------------------------------------|-------------------------------------------------------------|
| All code in `src/`, all tests, all workflow YAML | API key id, private key, signed headers, signatures          |
| Aggregate counts printed by the audit            | Raw fill payloads, fill ids, order ids, trade ids            |
| Synthetic fixtures in `tests/`                   | Tickers the owner traded, prices, contract counts, balances  |

Enforcement is structural, not just editorial:

* `AuditReport` (`src/kalshi_router/aggregate.py`) has **no field that can hold a
  string**. Every field is an `int`, a `bool`, or a `dict[Sport, int]`. A test
  asserts this, so a future field capable of carrying a ticker fails CI.
* Exceptions are constructed from a status code and an operation name only.
  Response bodies are read and discarded, never logged — an error body from a
  portfolio endpoint can echo account state.
* `KalshiSigner.__repr__` and `__str__` are overridden to redact, so the signer
  cannot be accidentally interpolated into a log line.
* The audit process holds fills in memory only. Nothing is written to disk,
  uploaded as an artifact, or placed in an Actions cache.
* `tests/test_workflow_safety.py` parses the committed workflow YAML and fails if
  anyone adds an artifact upload, a cache, a write permission, a fork-PR trigger,
  or the sensitive-output flag.

## 4. Credential handling

Two environment variables, both fail-closed:

| Variable             | Contents                                              |
|----------------------|-------------------------------------------------------|
| `KALSHI_API_KEY_ID`  | The API key id from Kalshi account settings           |
| `KALSHI_PRIVATE_KEY` | The **unencrypted** RSA private key, PEM, multi-line   |

They are stored as GitHub Actions repository secrets and are also read from the
local environment when running the CLI by hand. If either is missing or blank,
the process exits with code `2` **before any request is sent to Kalshi**.

Multi-line PEM secrets are supported natively. For robustness the loader also
accepts a PEM whose newlines were escaped as `\n`, and a base64-wrapped PEM.
Anything else is rejected without echoing the input.

Requests are signed with RSA-PSS (SHA-256, MGF1-SHA256, 32-byte salt) over
`{timestamp_ms}{METHOD}{path}`; see [`docs/API_CONTRACT.md`](docs/API_CONTRACT.md).

**Only read-only endpoints are reachable.** `KalshiReadOnlyClient` hard-codes the
`GET` verb and checks every path against an allowlist
(`/portfolio/fills`, `/markets/`, `/events/`, `/series/`) before signing. A
request to `/portfolio/orders` raises rather than being sent. Trading permissions
are never used.

## 5. Supported classifications

`MLB`, `NFL`, `CFB`, `TENNIS`, `OTHER`, `UNRESOLVED`.

`OTHER` and `UNRESOLVED` are **not** synonyms:

* **`OTHER`** — the system positively identified the market as outside the four
  supported sports (a Basketball tag, an Economics category).
* **`UNRESOLVED`** — the system could not prove ownership. Metadata was missing,
  contradictory, or merely ambiguous. Nothing downstream may act on it.

### Evidence hierarchy

Classification uses Kalshi's own metadata first, and treats ticker text as
supporting evidence except where an exact match makes it deterministic.
Only *authoritative* evidence can decide a sport.

| Rank | Signal                                                   | Strength      |
|------|----------------------------------------------------------|---------------|
| 1    | `series.tags` / `series.categories`                       | authoritative |
| 2    | `series.category`, `event.category`, `market.category`    | authoritative |
| 3    | `series.title`                                            | authoritative |
| 4    | Exact `series_ticker` match in the series registry        | authoritative |
| 5    | `event.title`, `event.sub_title`, market titles/subtitles | supporting    |

Rank 4 is an **exact** match against
[`series_registry.py`](src/kalshi_router/series_registry.py) — never a prefix or a
fuzzy resemblance. A market ticker contributes only via its series prefix, which
Kalshi forms as `SERIES-EVENT-OUTCOME`; `KXNFLGAMEXTRA-…` does not match
`KXNFLGAME`. Registry entries are flagged as unverified (see §15 of the contract
doc), and every audit reports how many classifications leaned on one.

## 6. Fail-closed policy

**Unknown does not mean guess.**

* Two authoritative signals naming different sports → `UNRESOLVED`.
* A sport *family* with no league distinction → `UNRESOLVED`. A market tagged only
  "Football" is never routed to NFL or CFB; a market tagged only "Baseball" is
  never routed to MLB.
* No authoritative signal at all → `UNRESOLVED`. Supporting evidence alone never
  resolves a market.
* A metadata lookup failure → `UNRESOLVED`, with a counter incremented. It never
  crashes the audit and never produces a guess.
* A malformed API envelope or a malformed fill → the audit **raises**. Silently
  skipping a fill would understate the account, so it is treated as a hard error.
* An empty fill history is a valid success state, reported distinctly from a
  failure.

## 7. Deliberately NOT implemented yet

* Any write to any downstream repository.
* Canonical wagers, ledgers, position accounting, settlement.
* Downstream workflow triggers, `repository_dispatch`, GitHub PATs or Apps.
* Order placement, cancellation, modification, or any trading permission.
* Persistence of any kind: no `data/raw-fills/`, `data/account-history/`,
  `data/wagers/`, `data/positions/`, no artifacts, no Actions cache.
  A test asserts none of those directories exist.
* Scheduled polling. Manual dispatch only.

## 8. How to run the tests

The suite is entirely synthetic — fake RSA keys generated at test time, invented
fill ids and tickers, and a fake transport. It never touches the network and never
uses the owner's fills.

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest
```

164 tests cover authentication and secret redaction, missing-credential failure,
pagination, duplicate fills, malformed responses, empty result sets, rate-limit
and retry behaviour, metadata lookup, each of the six classifications, ambiguity
handling, buy/sell, YES/NO, multi-fill orders, privacy-safe logging, the sensitive
local mode, and the workflow's inability to print raw fill objects.

## 9. How to run the safe GitHub Actions audit

Workflow: [`.github/workflows/phase0-readonly-audit.yml`](.github/workflows/phase0-readonly-audit.yml)

1. Actions → **Phase 0 read-only audit** → **Run workflow**.
2. Select branch `main` (the job refuses to run from any other ref).
3. Optionally set `max_fills` (1–500, default 200).

Trigger: `workflow_dispatch` only. Permissions: `contents: read` only. The job
prints aggregate counts and nothing else:

```
fills fetched: 142
unique fills: 142
duplicate fill IDs observed: 0

classification:
  MLB: 41
  NFL: 27
  CFB: 48
  TENNIS: 19
  OTHER: 7
  UNRESOLVED: 0
...
```

Exit code `0` covers a successful audit of an account with **no** fills; an API or
schema failure exits non-zero, so the two states are never confused.

> The workflow file must exist on `main` before GitHub will offer it for dispatch.

## 10. How the owner runs sensitive local diagnostics

**This mode prints the individual markets you traded. Run it only in a private
terminal.**

```bash
export KALSHI_API_KEY_ID=...          # your key id
export KALSHI_PRIVATE_KEY="$(cat kalshi-private-key.pem)"
python -m kalshi_router.cli audit --show-sensitive-details
```

Guarantees:

* Without the explicit `--show-sensitive-details` flag, output is aggregate-only.
* The flag **refuses to run** if any CI marker is present (`GITHUB_ACTIONS`, `CI`,
  `GITHUB_RUN_ID`, and others) — exit code `4`. Adding the flag to a workflow
  cannot leak anything; it just fails the job.
* Output goes to the terminal only. Nothing is written to a tracked file, saved
  into the repository, or uploaded as an artifact.
* The block is prefixed with a loud warning not to paste it anywhere.

### Exit codes

| Code | Meaning                                                        |
|------|----------------------------------------------------------------|
| 0    | Success — including a successful audit of an empty account      |
| 2    | Configuration or credential problem; nothing was sent to Kalshi |
| 3    | Kalshi API or schema failure                                    |
| 4    | Sensitive output requested somewhere it is not allowed          |

## 11. Phase 1 requirements

See [`docs/PHASE_1_REQUIREMENTS.md`](docs/PHASE_1_REQUIREMENTS.md) for the fill-schema
findings (partial fills, buy/sell, YES/NO, order grouping, position exit) and the
list of what a position-accounting layer will need before routing can be built.

## Documentation index

* [`docs/API_CONTRACT.md`](docs/API_CONTRACT.md) — the exact Kalshi API contract used, and its verification status.
* [`docs/PHASE_1_REQUIREMENTS.md`](docs/PHASE_1_REQUIREMENTS.md) — position-accounting research and Phase 1 scope.
