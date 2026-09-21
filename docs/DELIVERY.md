# Delivering wagers downstream — Phase H

What a batch is, what makes re-sending one safe, and the two decisions that are
the owner's rather than the router's.

> **Status, 2026-09-15.** Both blockers below are **RESOLVED** — the owner has
> supplied the credential and settled the privacy question (Reading A). The
> transport exists, MLB delivery runs on a 15-minute schedule, and the
> operational record is [OPERATIONS.md](OPERATIONS.md). The blocker sections
> are kept verbatim rather than deleted, because the reasoning is the record of
> *why* the questions were the owner's and not the router's. Read them as
> history, not as current state.

`src/kalshi_router/destination.py` builds the payload. It holds no NETWORK
reach — no HTTP client, no socket, no subprocess — and a structural test
(parsing the module's AST, not grepping its prose) asserts it cannot acquire
one. It does now write the payload to a local file, which it did not when this
document was first written; the transport that carries that file lives in
`.github/workflows/deliver-wagers.yml` and runs the destination's own importer.

## The router must never write the ledger file

MLB's own rule is that **all** writes go through
`lib.edgelab.bets.write_placed_bet`. A cross-repo commit straight into
`data/edgelab/bets/bets.jsonl` would bypass:

* the importer's duplicate detection (`DUPLICATE_NOOP`);
* its ticker resolution, which leaves an ambiguous row **UNRESOLVED with its
  candidates** rather than picking one;
* its validation and receipt.

Those are exactly the properties that make a re-run safe, so the unit of
delivery is a **batch payload handed to the destination's own importer**, never
a file write. A router that wrote the file directly would be faster and would
quietly destroy every invariant the destination was built around.

## The batch id is a constant, and that is the whole design

The destination derives a row's identity from:

```
hash(importBatchId, sourceBetKey, marketTicker, side)
```

`importBatchId` is therefore **part of the primary key**. Two natural choices
are both wrong, and the second is wrong in a way that would take a while to
notice:

| Choice | What happens |
|---|---|
| A timestamp (`router-2026-09-14T19:00`) | Every wager gets a new identity on every run. Each run duplicates the entire ledger. |
| A digest of the batch contents | Stable while the batch is — then **adding one new wager changes the id and re-imports every existing row as new**. |
| **A constant (`kalshi-router-v1`)** | A row's identity depends only on the row. Re-running an identical batch is a pure no-op; adding a wager adds exactly one row. |

So the batch id is a versioned constant. The version exists so a future
deliberate re-identification is possible and *explicit*, rather than something
that happens by accident because a clock moved.

Rows are also sorted by `sourceBetKey`, so the same set of wagers always
produces a byte-identical payload. That is not cosmetic: a diffable,
reproducible payload is what lets a human confirm that a second run really did
propose nothing new.

## Blocker 1 — the credential is the owner's to create

The router cannot create it, and must never be handed one in chat. What it
needs, stated precisely so the scope can be minimal rather than approximate:

* a **fine-grained** personal access token or a GitHub App installation;
* scoped to **exactly one repository** — the destination for the sport being
  routed, and no others;
* repository permission: **Contents: write** (to open a branch carrying the
  payload), and **Pull requests: write** if delivery goes via a pull request;
* **no** organization permissions, **no** workflow scope, **no** access to the
  router repository itself;
* stored as an Actions secret on the router repository, read by a *separate*
  dispatch-only workflow — never by the audit workflow, which keeps
  `permissions: contents: read` and stays read-only by construction.

Until that exists there is nothing to test against, and shipping a writer that
waits for a credential would put a loaded mechanism in a public repository for
no benefit.

## Blocker 2 — every destination repository is PUBLIC *(RESOLVED: Reading A)*

Checked, not assumed:

| Repo | Sport | Visibility |
|---|---|---|
| `chmoses98/edge-finder-api` | MLB | **public** |
| `chmoses98/nfl-edge-finder` | NFL | **public** |
| `chmoses98/cfb-edge-finder` | CFB | **public** (ledger on the `accounting-data` branch) |
| `chmoses98/Tennis-Edge-Finder` | Tennis | **public** |

A canonical wager row carries `stake`, `entryPrice`, `contracts`, `totalFees`,
`grossSettlementPayout`, `netProfitLoss`, `marketTicker` and `gameDate`. That is
personal betting history, and this project's standing rule is that personal
betting data stays private.

The complicating fact, also checked rather than assumed:
`data/edgelab/bets/bets.jsonl` in the MLB repository is **already tracked in
git** and already contains **457 wagers** with stakes, entry prices and payouts.
So the owner has already chosen to publish that ledger.

That makes this a real decision rather than an obvious one, and it is not the
router's to make:

* **Reading A.** ← **the owner chose this one.** The privacy rule governs the router's own outputs — its
  repository, its workflow files, its public Actions logs. Appending to a ledger
  the owner already publishes changes nothing about what is exposed, and routing
  there is the entire point of the system.
* **Reading B.** *(not adopted)* The rule is absolute. Automatically publishing hundreds of
  additional wagers into a public repository is precisely what it forbids, and
  an existing 457 rows is a precedent, not a permission.

The difference is material: one reading routes to `edge-finder-api` as designed,
the other requires a private destination before anything is routed at all. An
agent should not settle that silently by writing the code that assumes an
answer.

**What is NOT in question, under either reading:** the router's own Actions logs
never print a payload, a row, a ticker or an amount. Only counts. That rule is
already enforced structurally by the diagnostics types and their tests, and
Phase H does not touch it.

## What exists today *(updated 2026-09-21)*

* `write_payloads(wagers, out_dir)` — one importer payload per destination.
  Returns **counts, never rows**: a function that returned the rows would
  eventually have them printed by one of its callers.
* `DESTINATION_REPOS` — **two entries now, MLB and CFB**, and both are derived
  from `PROFILES` in `src/kalshi_router/destinations.py` rather than written
  down twice. A sport absent from it is refused upstream by
  `WagerRefusal.NO_DESTINATION_IMPORTER`, never defaulted somewhere plausible.
  What differs per destination, and why CFB's ledger branch needed a validator
  instead of CI, is [DESTINATIONS.md](DESTINATIONS.md).
* No NETWORK reach in this module, by construction and by test. The transport is
  a workflow, and it runs the destination's own importer rather than editing a
  ledger file.

See [OPERATIONS.md](OPERATIONS.md) for how it runs, what it has measured, and
what it currently cannot do.
