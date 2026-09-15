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
    write_payloads,
)
from kalshi_router.sports import Sport
from kalshi_router.wager import GameDateSource, ShadowWager


def production_wager(key="k1", sport="MLB"):
    from kalshi_router.production import OrderFinality, ProductionWager

    return ProductionWager(
        source_key=key,
        market_ticker="KXMLBGAME-26AUG03SFLAD-SF",
        sport=sport,
        game_date="2026-08-03",
        side="YES",
        contracts=Decimal(10),
        vwap_price=Decimal("0.56"),
        total_fees=Decimal("0.01"),
        stake=Decimal("5.61"),
        first_execution_time=Decimal(0),
        last_execution_time=Decimal(0),
        fill_count=1,
        finality=OrderFinality.FINAL_STABLE,
    )


def shadow_wager(key="k1", ticker="KXMLBGAME-26AUG03SFLAD-SF", sport=Sport.MLB):
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
    one = build_batch([shadow_wager("k1")])
    two = build_batch([shadow_wager("k1"), shadow_wager("k2")])
    assert one["importBatchId"] == two["importBatchId"] == ROUTER_IMPORT_BATCH_ID


def test_adding_a_wager_leaves_every_existing_row_byte_identical():
    # A content-derived batch id would pass the test above and fail this one:
    # it is stable while the batch is, then re-imports everything as new the
    # moment one wager is added.
    before = build_batch([shadow_wager("k1")])
    after = build_batch([shadow_wager("k2"), shadow_wager("k1")])
    assert before["rows"][0] == after["rows"][0]
    assert len(after["rows"]) == 2


def test_the_payload_is_reproducible_regardless_of_input_order():
    # A diffable, byte-identical payload is what lets a human confirm that a
    # second run really did propose nothing new.
    a = build_batch([shadow_wager("k2"), shadow_wager("k1"), shadow_wager("k3")])
    b = build_batch([shadow_wager("k3"), shadow_wager("k2"), shadow_wager("k1")])
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
        plan_delivery([shadow_wager(sport=Sport.NFL)])


def test_a_plan_reports_counts_and_a_public_repo_name_only():
    plans = plan_delivery([shadow_wager("k1"), shadow_wager("k2")])
    assert len(plans) == 1
    plan = plans[0]
    assert plan.rows == 2
    assert plan.destination == "chmoses98/edge-finder-api"
    text = plan.render()
    # No ticker, no stake, no date -- the destination name is already a public
    # fact about the system; the wagers are not.
    for token in ("KXMLBGAME", "2026-08-03", "5.61", "0.56", "$"):
        assert token not in text


def test_this_module_has_no_network_reach():
    """It builds and writes a payload. It cannot SEND one.

    When this test was written both owner decisions were still open, so the
    module was barred from writing anything at all. The owner has since settled
    both, and writing the payload to a local file is now this module's job --
    so the file check is gone and the NETWORK check stays, which was always the
    part that mattered. Delivery is the workflow's job, using a credential this
    module never sees.
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

    for capability in ("http", "urllib", "requests", "socket", "subprocess"):
        assert capability not in imported, f"{capability} would give this module reach"


def test_the_payload_is_written_to_a_file_and_never_returned_for_printing(tmp_path):
    """write_payloads returns COUNTS, never rows.

    The payload carries market, side, stake, contracts, price and fees. A
    function that returned it to a caller would eventually have it printed by
    one of them, and the router's logs are public.
    """
    import json

    counts = write_payloads([production_wager("k1"), production_wager("k2")], str(tmp_path))
    assert counts == {"MLB": 2}
    for value in counts.values():
        assert isinstance(value, int)

    written = json.loads((tmp_path / "MLB.json").read_text())
    assert written["importBatchId"] == ROUTER_IMPORT_BATCH_ID
    assert [row["sourceBetKey"] for row in written["rows"]] == ["k1", "k2"]


def test_the_written_payload_is_byte_identical_run_to_run(tmp_path):
    from kalshi_router.destination import write_payloads as w

    a, b = tmp_path / "a", tmp_path / "b"
    w([production_wager("k2"), production_wager("k1")], str(a))
    w([production_wager("k1"), production_wager("k2")], str(b))
    assert (a / "MLB.json").read_bytes() == (b / "MLB.json").read_bytes()


def test_a_sport_without_a_destination_cannot_be_written(tmp_path):
    from kalshi_router.sports import Sport

    with pytest.raises(ValueError, match="no destination importer"):
        write_payloads([production_wager(sport=Sport.NFL.value)], str(tmp_path))
