"""THE SETTLEMENT BATCH THAT IMPORTED CLEANLY AND WOULD NOT MERGE.

On 2026-09-22 the live settle run reached 41 real settled CFB positions,
matched all 41 to canonical wagers, imported them (41 NEW, 0 failed), proved a
second identical import changed nothing, and watched the destination's own
validator accept the tree at 59 wagers / 59 settlements / 0 problems.

Then the gate refused the merge:

    REFUSE EVERY_ADDED_ROW_CARRIES_THE_ROUTER_IDENTITY
    - 41 added row(s) do not carry the router's import batch id 'kalshi-router-v1'

Nothing was wrong with the rows. The gate was reading a SETTLEMENT row with a
WAGER row's rule.

    a wager row      carries import_batch_id -- required by both destination
                     schemas, stamped by `_write_payloads`
    a settlement row carries none -- `_settlement_row` emits none, the
                     destination settlement schemas model none, and their
                     importers REFUSE a row with an unknown field, so adding
                     one would move the refusal from this gate to the importer
                     rather than remove it

A settlement is keyed on the wager it settles: its canonical id is minted from
that wager's `source_bet_key` alone, and it may not be written at all unless
that wager is already in the destination's ledger.

These tests pin the distinction from both ends: the real 41-row shape merges,
and every way a settlement row could fail to be ours still refuses.
"""

import json
from pathlib import Path

from kalshi_router import automerge
from kalshi_router.destination import ROUTER_IMPORT_BATCH_ID, _settlement_row

# Four rows copied verbatim from the diff of chmoses98/cfb-edge-finder#53,
# including the two that share a market ticker with a different wager and the
# one whose net figure is absent with a refusal recorded. Real production
# shape, not a hand-written approximation of one.
PR_53_ROWS = [
    {
        "gross_return": 0.0,
        "market_ticker": "KXNCAAF1QTOTAL-26SEP19UGAARK-11",
        "net_profit_loss": -51.5253,
        "refusals": [],
        "result": "LOST",
        "schema_version": "cfb_wager_settlement.v1",
        "settled_at": "2026-09-19T16:45:34.509166Z",
        "settlement_id": "stl-23dbe2a8dea558d7bc7ab8e1",
        "settlement_status": "SETTLED",
        "side": "YES",
        "source_bet_key": "kalshi:v1:03a8210bb09c533faadd20f5229a471408888089adf7f48c4bc412026468eb45",
        "venue": "kalshi",
    },
    {
        "gross_return": 0.0,
        "market_ticker": "KXNCAAFSPREAD-26SEP19PREWCU-WCU34",
        "net_profit_loss": None,
        "refusals": ["shared_position_fee"],
        "result": "LOST",
        "schema_version": "cfb_wager_settlement.v1",
        "settled_at": "2026-09-20T00:34:34.521071Z",
        "settlement_id": "stl-9a9ca35b56a3a72ab142ee58",
        "settlement_status": "SETTLED",
        "side": "NO",
        "source_bet_key": "kalshi:v1:41b8a143560f919459f5bde7bf2e608e1f0a70b6b64909c68042cdf33ee815ef",
        "venue": "kalshi",
    },
    {
        "gross_return": 0.0,
        "market_ticker": "KXNCAAFSPREAD-26SEP19PREWCU-WCU34",
        "net_profit_loss": None,
        "refusals": ["shared_position_fee"],
        "result": "LOST",
        "schema_version": "cfb_wager_settlement.v1",
        "settled_at": "2026-09-20T00:34:34.521071Z",
        "settlement_id": "stl-20e80ecdc25175e8385e91c2",
        "settlement_status": "SETTLED",
        "side": "NO",
        "source_bet_key": "kalshi:v1:acfdabbfb90d3670a6da8dd03d1e029963895e587642dd942fa68bcf2d218100",
        "venue": "kalshi",
    },
    {
        "gross_return": 135.26,
        "market_ticker": "KXNCAAFTOTAL-26SEP19MOHCIN-51",
        "net_profit_loss": 62.8958,
        "refusals": [],
        "result": "WON",
        "schema_version": "cfb_wager_settlement.v1",
        "settled_at": "2026-09-19T23:14:14.655884Z",
        "settlement_id": "stl-1066112b62ffa3ad9d6ebf3d",
        "settlement_status": "SETTLED",
        "side": "YES",
        "source_bet_key": "kalshi:v1:d3e46a02439b5ce8dab4ca94cf67458dbda1cbf97c71fe37ce30a2612a248e8d",
        "venue": "kalshi",
    },
]


def receipts_for(rows):
    """The CFB settlement importer's own receipt shape, one per row.

    `.get` rather than `[...]` because several tests below deliberately hand
    in a malformed row, and the importer emits a receipt for a refused row
    too -- with the fields it could read and the rest absent."""
    return tuple(
        {
            "row": index,
            "source_bet_key": row.get("source_bet_key"),
            "settlement_id": row.get("settlement_id"),
            "duplicate_status": "NEW",
            "success": True,
        }
        for index, row in enumerate(rows)
    )


def rerun_receipts_for(rows):
    return tuple(
        {**receipt, "duplicate_status": "DUPLICATE_NOOP"} for receipt in receipts_for(rows)
    )


def settlement_facts(**overrides):
    """The real CFB settlement delivery, as the gate saw it on 2026-09-22.

    Every other condition PASSes, so each test below changes exactly one
    thing and the verdict names what it changed."""
    rows = overrides.pop("rows", PR_53_ROWS)
    base = dict(
        sport="CFB",
        destination_repo="chmoses98/cfb-edge-finder",
        pull_number=53,
        state="open",
        draft=False,
        head_ref="kalshi-router/settle-CFB",
        head_sha="b" * 40,
        head_repo="chmoses98/cfb-edge-finder",
        base_ref="accounting-data",
        mergeable=True,
        mergeable_state="clean",
        verified_sha="b" * 40,
        changed_files=("settlements/2026.jsonl",),
        added_ledger_rows=tuple(rows),
        removed_ledger_rows=(),
        receipts=receipts_for(rows),
        rerun_receipts=rerun_receipts_for(rows),
        rerun_changed_the_tree=False,
        check_runs=(),
        partial_delivery=False,
        destination_validator_passed=True,
        branch_kind=automerge.SETTLEMENTS,
    )
    base.update(overrides)
    return automerge.MergeFacts(**base)


# ══════════════════════════════════════════════════════════════════════
# 1 + 11. the real batch merges
# ══════════════════════════════════════════════════════════════════════

def test_the_real_pr_53_settlement_rows_pass_the_gate():
    """The regression. These exact rows were refused in production."""
    verdict = automerge.evaluate(settlement_facts())
    assert "EVERY_ADDED_ROW_CARRIES_THE_ROUTER_IDENTITY" in verdict.passed
    assert verdict.verdict == automerge.MERGE, verdict.render()
    assert sorted(verdict.passed) == sorted(automerge.CONDITIONS)


def test_the_production_refusal_reason_is_gone():
    verdict = automerge.evaluate(settlement_facts())
    assert not any("import batch id" in reason for reason in verdict.reasons), verdict.render()


def test_every_condition_still_reaches_exactly_one_bucket_for_settlements():
    verdict = automerge.evaluate(settlement_facts())
    buckets = verdict.passed + verdict.failed + verdict.waiting_on
    assert sorted(buckets) == sorted(automerge.CONDITIONS)
    assert len(buckets) == len(set(buckets))


# ══════════════════════════════════════════════════════════════════════
# 8 + 9. the guard is not weakened, it is pointed at the right contract
# ══════════════════════════════════════════════════════════════════════

def test_a_settlement_row_carrying_a_foreign_batch_id_still_refuses():
    """Absence is the settlement contract. A STRANGER'S value never is."""
    row = {**PR_53_ROWS[0], "import_batch_id": "somebody-elses-batch"}
    verdict = automerge.evaluate(settlement_facts(rows=[row, *PR_53_ROWS[1:]]))
    assert verdict.verdict == automerge.REFUSE
    assert "EVERY_ADDED_ROW_CARRIES_THE_ROUTER_IDENTITY" in verdict.failed
    assert any("import batch id" in reason for reason in verdict.reasons)


def test_a_settlement_row_with_no_canonical_id_refuses():
    row = {k: v for k, v in PR_53_ROWS[0].items() if k != "settlement_id"}
    verdict = automerge.evaluate(settlement_facts(rows=[row, *PR_53_ROWS[1:]]))
    assert verdict.verdict == automerge.REFUSE
    assert "EVERY_ADDED_ROW_CARRIES_THE_ROUTER_IDENTITY" in verdict.failed
    assert any("no canonical id" in reason for reason in verdict.reasons)


def test_a_settlement_row_with_no_source_key_refuses():
    """The source key is the WHOLE identity of a settlement row. Without it
    there is nothing to join to a wager and nothing to match a receipt on."""
    row = {k: v for k, v in PR_53_ROWS[0].items() if k != "source_bet_key"}
    verdict = automerge.evaluate(settlement_facts(rows=[row, *PR_53_ROWS[1:]]))
    assert verdict.verdict == automerge.REFUSE
    assert "EVERY_ADDED_ROW_CARRIES_THE_ROUTER_IDENTITY" in verdict.failed


def test_a_settlement_row_this_run_did_not_import_refuses():
    """The receipt match is what replaced the batch id as the proof that a
    settlement row is ours, so it has to actually catch a smuggled row."""
    smuggled = {**PR_53_ROWS[0],
                "settlement_id": "stl-smuggled",
                "source_bet_key": "kalshi:v1:never-imported-by-this-run"}
    verdict = automerge.evaluate(
        settlement_facts(added_ledger_rows=tuple([*PR_53_ROWS, smuggled])))
    assert verdict.verdict == automerge.REFUSE
    assert "EVERY_ADDED_ROW_CARRIES_THE_ROUTER_IDENTITY" in verdict.failed
    assert any("no matching receipt" in reason for reason in verdict.reasons)


def test_settlement_rows_with_no_receipts_at_all_refuse():
    """A wager batch has the constant batch id as a second, independent
    answer to 'is this ours'. A settlement batch does not, so an empty
    receipt set must REFUSE rather than skip the check."""
    verdict = automerge.evaluate(settlement_facts(receipts=()))
    assert verdict.verdict == automerge.REFUSE
    assert "EVERY_ADDED_ROW_CARRIES_THE_ROUTER_IDENTITY" in verdict.failed
    assert any("nothing proving" in reason for reason in verdict.reasons)


def test_an_unparseable_settlement_line_refuses():
    verdict = automerge.evaluate(settlement_facts(rows=[{"_unparseable": True}]))
    assert verdict.verdict == automerge.REFUSE
    assert "EVERY_ADDED_ROW_CARRIES_THE_ROUTER_IDENTITY" in verdict.failed


def test_a_settlement_diff_that_rewrites_an_existing_row_still_refuses():
    """Append-only is not kind-specific and must not have become so."""
    verdict = automerge.evaluate(settlement_facts(
        removed_ledger_rows=('{"settlement_id": "stl-older-row"}',)))
    assert verdict.verdict == automerge.REFUSE
    assert "THE_LEDGER_DIFF_IS_APPEND_ONLY" in verdict.failed


def test_a_settlement_batch_touching_a_file_outside_the_ledger_still_refuses():
    verdict = automerge.evaluate(settlement_facts(
        changed_files=("settlements/2026.jsonl", "src/cfb_edge_finder/config.py")))
    assert verdict.verdict == automerge.REFUSE
    assert "ONLY_CANONICAL_WAGER_FILES_CHANGED" in verdict.failed


def test_a_settlement_batch_whose_rerun_wrote_again_still_refuses():
    verdict = automerge.evaluate(settlement_facts(
        rerun_changed_the_tree=True,
        rerun_receipts=receipts_for(PR_53_ROWS),  # NEW, not DUPLICATE_NOOP
    ))
    assert verdict.verdict == automerge.REFUSE
    assert "THE_IMPORT_IS_IDEMPOTENT" in verdict.failed


def test_a_settlement_batch_the_destination_validator_refused_still_refuses():
    verdict = automerge.evaluate(settlement_facts(destination_validator_passed=False))
    assert verdict.verdict == automerge.REFUSE
    assert "CONTINUOUS_INTEGRATION_IS_GREEN" in verdict.failed


def test_a_settlement_batch_on_the_wager_branch_refuses():
    """The branch name and the branch kind must agree; the gate reads the
    kind it was pointed at, so a mismatch has to be caught here."""
    verdict = automerge.evaluate(settlement_facts(head_ref="kalshi-router/CFB"))
    assert verdict.verdict == automerge.REFUSE
    assert "BRANCH_ORIGINATED_FROM_THE_ROUTER_WORKFLOW" in verdict.failed


# ══════════════════════════════════════════════════════════════════════
# 12. wager behaviour is unchanged
# ══════════════════════════════════════════════════════════════════════

def test_a_wager_row_without_a_batch_id_still_refuses():
    """The fix must not have made the batch id optional everywhere. A WAGER
    row's schema requires it, so a wager batch missing it is still foreign."""
    verdict = automerge.evaluate(automerge.MergeFacts(
        sport="MLB",
        destination_repo="chmoses98/edge-finder-api",
        pull_number=218, state="open", draft=False,
        head_ref="kalshi-router/MLB", head_sha="a" * 40,
        head_repo="chmoses98/edge-finder-api", base_ref="main",
        mergeable=True, mergeable_state="clean", verified_sha="a" * 40,
        changed_files=("data/edgelab/bets/bets.jsonl",),
        added_ledger_rows=({"betId": "id1", "sourceBetKey": "k1"},),
        receipts=({"betId": "id1", "sourceBetKey": "k1",
                   "duplicateStatus": "NEW", "success": True},),
        rerun_receipts=({"betId": "id1", "sourceBetKey": "k1",
                         "duplicateStatus": "DUPLICATE_NOOP", "success": True},),
        rerun_changed_the_tree=False,
        check_runs=(("test", "completed", "success"),),
        branch_kind=automerge.WAGERS,
    ))
    assert verdict.verdict == automerge.REFUSE
    assert "EVERY_ADDED_ROW_CARRIES_THE_ROUTER_IDENTITY" in verdict.failed
    assert any("import batch id" in reason for reason in verdict.reasons)


def test_the_kind_is_taken_from_the_delivery_not_inferred_from_the_row():
    """Inferring 'this must be a settlement' from a MISSING batch id would
    let exactly the broken wager row above walk through the settlement
    door. The delivery declares its kind; the row never gets a vote."""
    rows = ({"betId": "id1", "sourceBetKey": "k1"},)
    common = dict(
        sport="MLB", destination_repo="chmoses98/edge-finder-api",
        pull_number=218, state="open", draft=False, head_sha="a" * 40,
        head_repo="chmoses98/edge-finder-api", base_ref="main",
        mergeable=True, mergeable_state="clean", verified_sha="a" * 40,
        changed_files=("data/edgelab/bets/bets.jsonl",),
        added_ledger_rows=rows,
        receipts=({"betId": "id1", "sourceBetKey": "k1",
                   "duplicateStatus": "NEW", "success": True},),
        rerun_receipts=({"betId": "id1", "sourceBetKey": "k1",
                         "duplicateStatus": "DUPLICATE_NOOP", "success": True},),
        rerun_changed_the_tree=False,
        check_runs=(("test", "completed", "success"),),
    )
    as_wagers = automerge.evaluate(automerge.MergeFacts(
        head_ref="kalshi-router/MLB", branch_kind=automerge.WAGERS, **common))
    assert "EVERY_ADDED_ROW_CARRIES_THE_ROUTER_IDENTITY" in as_wagers.failed


# ══════════════════════════════════════════════════════════════════════
# the payload contract this gate now depends on
# ══════════════════════════════════════════════════════════════════════

class _Settlement:
    source_bet_key = "kalshi:v1:abc"
    market_ticker = "KXNCAAFTOTAL-26SEP19MOHCIN-51"
    side = "YES"
    settlement_status = "SETTLED"
    settled_at = "2026-09-19T23:14:14.655884Z"
    result = "WON"
    gross_return = 135.26
    net_profit_loss = 62.8958
    refusals = ()


def test_the_router_settlement_payload_carries_no_import_batch_id():
    """If this ever starts emitting one, the destinations' settlement
    importers REFUSE the row for carrying an unknown field -- the whole
    batch, at the importer, before the gate is even reached. So the
    absence is a contract, not an oversight, and it is pinned here."""
    row = _settlement_row(_Settlement())
    assert "import_batch_id" not in row
    assert "importBatchId" not in row
    assert row["source_bet_key"] == "kalshi:v1:abc"


def test_the_settlement_payload_shape_is_exactly_the_documented_one():
    assert set(_settlement_row(_Settlement())) == {
        "source_bet_key", "market_ticker", "side", "settlement_status",
        "settled_at", "result", "gross_return", "net_profit_loss",
        "refusals", "venue",
    }


def test_no_settlement_economics_appear_in_the_gate_output():
    """This repository's Actions logs are public. The gate prints condition
    names and counts; a payout or a ticker must never reach them."""
    rendered = automerge.evaluate(settlement_facts(receipts=())).render()
    for secret in ("135.26", "62.8958", "-51.5253", "KXNCAAFTOTAL", "KXNCAAF1QTOTAL"):
        assert secret not in rendered, rendered
