"""The delivery contract: what makes re-sending a batch a no-op.

Phase H. There is no transport to test, on purpose -- the module ships the
payload contract and nothing that can send it. What IS testable is the property
the whole design turns on: a wager's identity downstream must depend only on
the wager.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from kalshi_router.destination import (
    DESTINATION_REPOS,
    ROUTER_IMPORT_BATCH_ID,
    build_batch,
    plan_delivery,
)
from kalshi_router.sports import Sport
from kalshi_router.wager import GameDateSource, ShadowWager


def wager(key="k1", ticker="KXMLBGAME-26AUG03SFLAD-SF", sport=Sport.MLB):
    return ShadowWager(
        source_bet_key=key,
        market_ticker=ticker,
        event_ticker="KXMLBGAME-26AUG03SFLAD",
        sport=sport,
        game_date="2026-08-03",
        game_date_source=GameDateSource.EVENT_TICKER,
        side="YES",
        contracts=Decimal(10),
        entry_price=Decimal("0.56"),
        contract_cost=Decimal("5.60"),
        total_fees=Decimal("0.01"),
        stake=Decimal("5.61"),
        gross_settlement_payout=Decimal(10),
        net_profit_loss=Decimal("4.39"),
        result="WIN",
        entry_timestamp=Decimal(0),
    )


# -------------------------------------------- the identity property that matters

def test_the_batch_id_does_not_depend_on_the_batch():
    """The whole point. Identity downstream is
    hash(importBatchId, sourceBetKey, marketTicker, side), so a batch id that
    moves gives the same wager a new identity and duplicates the ledger.
    """
    one = build_batch([wager("k1")])
    two = build_batch([wager("k1"), wager("k2")])
    assert one["importBatchId"] == two["importBatchId"] == ROUTER_IMPORT_BATCH_ID


def test_adding_a_wager_leaves_every_existing_row_byte_identical():
    # A content-derived batch id would pass the test above and fail this one:
    # it is stable while the batch is, then re-imports everything as new the
    # moment one wager is added.
    before = build_batch([wager("k1")])
    after = build_batch([wager("k2"), wager("k1")])
    assert before["rows"][0] == after["rows"][0]
    assert len(after["rows"]) == 2


def test_the_payload_is_reproducible_regardless_of_input_order():
    # A diffable, byte-identical payload is what lets a human confirm that a
    # second run really did propose nothing new.
    a = build_batch([wager("k2"), wager("k1"), wager("k3")])
    b = build_batch([wager("k3"), wager("k2"), wager("k1")])
    assert a == b
    assert [row["sourceBetKey"] for row in a["rows"]] == ["k1", "k2", "k3"]


# ------------------------------------------------------------- the destinations

def test_only_mlb_has_a_destination():
    # Phase F read all four repositories. Three have no importer, and inventing
    # their ledger contract is the owner's decision.
    assert set(DESTINATION_REPOS) == {Sport.MLB}


def test_a_sport_without_a_destination_cannot_be_planned():
    # It should have been refused upstream; this asserts the invariant rather
    # than trusting it.
    with pytest.raises(ValueError, match="no destination importer"):
        plan_delivery([wager(sport=Sport.NFL)])


def test_a_plan_reports_counts_and_a_public_repo_name_only():
    plans = plan_delivery([wager("k1"), wager("k2")])
    assert len(plans) == 1
    plan = plans[0]
    assert plan.rows == 2
    assert plan.destination == "chmoses98/edge-finder-api"
    text = plan.render()
    # No ticker, no stake, no date -- the destination name is already a public
    # fact about the system; the wagers are not.
    for token in ("KXMLBGAME", "2026-08-03", "5.61", "0.56", "$"):
        assert token not in text


def test_no_transport_exists_in_this_module():
    """Deliberate: there is nothing here that can send anything.

    Two questions must be settled by the owner first -- a single-repo
    credential, and whether these rows may be published into a PUBLIC
    repository at all. Shipping a writer before either is answered would put
    the decision in the code rather than with the owner.
    """
    import ast

    import kalshi_router.destination as module

    # Structural, not a grep: the prose in this module DISCUSSES http and git,
    # and a text search would match the explanation of why neither is here.
    tree = ast.parse(open(module.__file__).read())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])

    for capability in ("http", "urllib", "requests", "socket", "subprocess", "os"):
        assert capability not in imported, f"{capability} would give this module reach"

    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "open" not in called   # nor can it write a file
