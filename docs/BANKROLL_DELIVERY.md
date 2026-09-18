# Bankroll delivery

How the MLB handicapper learns the account's real current cash, and why it
travels the way it does.

---

## 1. What changed, and what did not

Phase 0 pinned `/portfolio/balance` shut and documented that *"no balance or
account metadata is persisted anywhere"*. That was right for a repository
that had no use for it.

The destination's real-money handicapping card does have a use for it: a
stake size computed against a remembered number, a hand-typed number, or a
number derived from a ledger whose cash history is known to be incomplete is
not a stake size. So the owner authorised **reading the balance**.

The boundary is exact, and it is enforced by code rather than by convention:

| | |
|---|---|
| **Permitted** | `GET /portfolio/balance` — the account's available cash |
| **Not permitted** | placing, amending or cancelling an order; withdrawing, depositing or transferring; any other account mutation |

`ACCOUNT_READ_ONLY_PATHS` in `src/kalshi_router/client.py` is an
**exact-match** set containing one path. It is deliberately not an entry in
`READ_ONLY_PATH_PREFIXES`, because a prefix `/portfolio/balance` would also
admit `/portfolio/balance/transfer`, and a prefix `/portfolio/` — the lazy
version of this change — would admit order entry outright. Both failures are
pinned by tests rather than reviewed for:

* `test_balance_allowlist_is_exact_match_not_a_prefix`
* `test_account_allowlist_admits_nothing_but_the_balance_read`
* `test_no_mutating_method_exists_on_the_client`
* `TRADING_ROUTES` still pins orders, amend, cancel, queue position,
  resting-order value, **and now transfers, withdrawals and deposits**, as
  unreachable.

## 2. Which field is the bankroll

`GET /trade-api/v2/portfolio/balance` returns:

| Field | Meaning | Used? |
|---|---|---|
| **`balance`** | the member's **available balance, in cents** (integer) | **yes — this is the bankroll** |
| `balance_dollars` | a dollar rendering of the same figure | no |
| `portfolio_value` | **mark-to-market value of open positions**, in cents | **never** |
| `updated_ts` | when the exchange last recomputed it | no |
| `balance_breakdown` | per-exchange-index split of the same balance | no |

The published value is labelled **`KALSHI_AVAILABLE_CASH_BALANCE`**, and that
label is load-bearing. The question a handicapper asks is *"how much can I
deploy on a new wager right now"*. `portfolio_value` answers a different
question — what open positions are currently worth — and staking against it
would double-count exposure already at risk. `parse_balance_response` does
not merely skip it: a response carrying `portfolio_value` and **no**
`balance` is **refused**, with that reason named in the error, rather than
falling back to the larger number.

`balance_dollars` is ignored for a duller reason: two fields that must agree
are a field that can disagree. The integer cents are converted once, here,
with `Decimal`.

> **Verified against the live account on 2026-09-18.** The first authenticated
> run of this workflow
> ([35298910389](https://github.com/chmoses98/kalshi-bet-router/actions/runs/35298910389))
> called `GET /portfolio/balance` with the real credentials and
> `parse_balance_response` accepted the response unchanged. Since that parser
> refuses string cents, float cents, a bool, a negative value, a missing
> `balance` and a non-object body, acceptance is itself the confirmation: the
> live payload carries `balance` as a non-negative integer number of cents,
> exactly as the API reference documents. The amount was withheld from the log,
> as designed. The parser stays strict, so a future contract change surfaces as
> "sizing unavailable" instead of as a plausible wrong number.

## 3. Why the number travels as an encrypted secret

**Both repositories in this system are public:**

```
chmoses98/kalshi-bet-router   public
chmoses98/edge-finder-api     public
```

On a public repository, workflow logs, job summaries and uploaded artifacts
are readable by anyone, with no login. So every obvious delivery mechanism
publishes the owner's bank balance to the internet:

| Channel | On a public repo |
|---|---|
| commit it to either repo | public, and public forever in git history |
| upload an Actions artifact | public download link |
| `echo` it into the job log | public |
| write it to `$GITHUB_STEP_SUMMARY` | public |

A **GitHub Actions secret** is the one channel in this architecture that is
not. It is sealed client-side with libsodium against the destination
repository's Actions public key; the REST API exposes no way to read the
plaintext back; it is decrypted only inside a workflow run of that
repository.

```
kalshi-bet-router                              edge-finder-api
─────────────────                              ───────────────
publish-bankroll.yml  (*/15 * * * *)
  │
  ├─ cli bankroll ──► GET /portfolio/balance
  │                     └─► {bankroll, currency, observedAt,
  │                          source, valueType}   ← 5 fields, nothing else
  │                        written to $RUNNER_TEMP, chmod 600
  │
  └─ publish_bankroll_secret.py
       └─ libsodium sealed box ──────────────►  secret KALSHI_BANKROLL_CONTEXT
                                                          │
                                                          ▼
                                                build-handicapping-card.yml
                                                  reads it from the secret,
                                                  sizes against it,
                                                  COMMITS A REDACTED CARD
```

The card that gets committed carries the bankroll's **status, observation
time, age, source, semantic type and `sizingAllowed`** — everything needed to
trust or distrust the sizing — and **not the amount**.

The destination draws the consequence explicitly, because `sizingAllowed` and
"the reader knows the number" are different facts: the committed card also
carries `numericBankrollAvailable: false` and
`consumerSizingVerdict: NO_DOLLAR_SIZING_FOR_THIS_CONSUMER`, so a chat session
reading the public artifact cannot mistake a true `sizingAllowed: true` — true
of the workflow that held the balance — for permission to invent dollar
stakes. See `edge-finder-api/docs/BANKROLL_CONTEXT.md`.

## 4. What is published, and what is never published

`build_bankroll_context()` emits an **allowlist**, not a redaction pass, so a
field cannot leak by being forgotten:

```json
{"schemaVersion":"1","bankroll":1234.56,"currency":"USD",
 "observedAt":"2026-09-18T14:30:05Z",
 "source":"kalshi_authenticated_balance",
 "valueType":"KALSHI_AVAILABLE_CASH_BALANCE"}
```

Never emitted, never written, never logged: API keys, signatures, account
ids, member ids, subaccount identifiers, the raw API response, fill ids,
positions, `portfolio_value`, `balance_breakdown`, or any other account
metadata. `scripts/publish_bankroll_secret.py` re-checks the key set against
the same allowlist immediately before sending, and refuses on any mismatch —
reporting the *names* of the offending keys, never a value.

`observedAt` is **the instant this process read the balance**, not the
exchange's `updated_ts`. The consumer's freshness window is about how stale
the number in its hands is, and only the read time answers that.

## 5. Credentials this needs

| Secret | Scope | Why separate |
|---|---|---|
| `KALSHI_API_KEY_ID` / `KALSHI_PRIVATE_KEY` | already present | the read-only Kalshi credential |
| `DOWNSTREAM_SECRETS_TOKEN` | fine-grained PAT, **`Secrets: Read and write` on `chmoses98/edge-finder-api` only** | writing a secret and pushing a branch are different powers; `DOWNSTREAM_REPO_TOKEN` must not silently acquire the first |

If `DOWNSTREAM_SECRETS_TOKEN` is absent the job fails loudly with that
instruction rather than falling back to a public channel.

## 6. Cadence and freshness

The workflow runs **every 15 minutes**; the destination refuses to size
against an observation older than **30 minutes**. The headroom covers one
delayed or failed scheduled run. A balance outside the window does not
produce a wrong stake size — it produces **no** stake size, and the card says
so.

Dispatch it manually (`workflow_dispatch`) to refresh immediately before a
slate.
