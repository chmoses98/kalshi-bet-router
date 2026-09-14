"""Position authority: a complete walk of fills does not prove the position story.

The defect these tests exist to make impossible
-----------------------------------------------
``claims_complete_position_state`` once derived from fill completeness alone. So
an exhaustive run could report:

    fill history complete            = True
    claims_complete_position_state   = True

while reconciliation simultaneously proved 943 replayed-open markets were absent
from the exchange's current-position view with no settlement to explain them.
The rendered report hid it by printing a gated expression, but ``as_dict()``
still exported the raw ``True`` -- one true value and one false presentation
under the same name, and the machine consumers read the false one.

Worse, ``provable`` was set from completeness too, so a contradicted market
still received a STABLE, importable episode identity.

Three concepts, kept apart
--------------------------
1. **Fill-history completeness** -- did both routes exhaust with nothing
   rejected?
2. **Position authority** -- does the replayed position reconcile with exchange
   truth, or was it closed by an authoritative event?
3. **Episode importability** -- is this specific episode proven enough for
   downstream use?

(1) is necessary for (2) and (3), and sufficient for neither.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from kalshi_router.accounting import AccountingEngine, HistoryCompleteness
from kalshi_router.accounting.diagnostics import build_diagnostics
from kalshi_router.accounting.identity import (
    IdentityNotImportable,
    ProvisionalIdentity,
    StableIdentity,
    require_importable_identity,
)
from kalshi_router.accounting.position import PositionAuthority
from kalshi_router.models import normalize_fill, normalize_settlement
from kalshi_router.reconcile import exchange_position_view

from .synthetic import SYNTH_TICKER, make_accounting_fill

COMPLETE = HistoryCompleteness.COMPLETE
BOUNDED = HistoryCompleteness.BOUNDED_WINDOW
OTHER_TICKER = "KXSYNTH-OTHER"


def fill(index, quantity="10.00", ticker=SYNTH_TICKER, action="buy", side="yes"):
    return normalize_fill(
        make_accounting_fill(
            index, quantity, ticker=ticker, action=action, side=side, minute=index
        )
    )


def settlement_row(ticker=SYNTH_TICKER, **overrides):
    row = {
        "ticker": ticker,
        "settled_time": "2026-09-02T00:00:00Z",
        "market_result": "yes",
        "revenue": "1000",
        "value": "100",
        "fee_cost": "0.0700",
        "yes_count_fp": "10.00",
        "no_count_fp": "0.00",
    }
    row.update(overrides)
    return row


def replay(fills, exchange_positions=None, settlements=(), completeness=COMPLETE):
    return AccountingEngine().replay(
        fills,
        completeness,
        settlements=[normalize_settlement(s) for s in settlements],
        exchange_positions=exchange_positions,
    )


def episode_on(result, ticker=SYNTH_TICKER, subaccount=0):
    return result.ledger_for(ticker, subaccount).episodes[0]


# =========================================================== 1, 2, 3: the claim

def test_complete_fills_plus_an_unexplained_open_market_withholds_the_claim():
    """(1) fill_history_complete True, claims_complete_position_state False."""
    result = replay([fill(1)], exchange_positions={})   # exchange reports nothing
    assert result.fill_history_complete is True
    assert result.claims_complete_position_state is False
    assert episode_on(result).authority is PositionAuthority.UNEXPLAINED


def test_as_dict_reports_the_same_false_the_object_does():
    """(2) No raw true value survives into the machine-readable export."""
    diagnostics = build_diagnostics(replay([fill(1)], exchange_positions={}))
    data = diagnostics.as_dict()
    assert data["fill_history_complete"] is True
    assert data["claims_complete_position_state"] is False
    # And no second field carries the ungated answer under another name.
    for retired in ("position_state_is_authoritative", "history_is_complete"):
        assert retired not in data


def test_rendered_output_reports_the_same_false_too():
    """(3) One value, three surfaces, no disagreement between them."""
    diagnostics = build_diagnostics(replay([fill(1)], exchange_positions={}))
    text = diagnostics.render()
    assert "fill history is complete: True" in text
    assert "position state claimed as authoritative: False" in text


def test_the_three_surfaces_can_never_disagree():
    """The contradiction itself, pinned shut across every case."""
    cases = [
        ([fill(1)], None),                                   # never reconciled
        ([fill(1)], {}),                                     # unexplained
        ([fill(1)], {SYNTH_TICKER: Decimal("99.00")}),       # conflicted
        ([fill(1)], {SYNTH_TICKER: Decimal("10.00")}),       # reconciled
        ([fill(1), fill(2, action="sell")], None),           # closed by fills
    ]
    for fills, view in cases:
        result = replay(fills, exchange_positions=view)
        diagnostics = build_diagnostics(result)
        claim = result.claims_complete_position_state
        assert diagnostics.claims_complete_position_state is claim
        assert diagnostics.as_dict()["claims_complete_position_state"] is claim
        assert f"position state claimed as authoritative: {claim}" in diagnostics.render()


# ================================================ 4: no identity without authority

def test_an_unexplained_market_exposes_no_importable_identity():
    """(4) Structural refusal, not a warning boolean beside a usable key."""
    episode = episode_on(replay([fill(1)], exchange_positions={}))
    assert episode.provable is True              # the OPENING is proven
    assert episode.authority is PositionAuthority.UNEXPLAINED
    assert episode.is_importable is False
    assert isinstance(episode.identity, ProvisionalIdentity)
    assert episode.source_key is None
    assert episode.source_id is None
    with pytest.raises(IdentityNotImportable):
        require_importable_identity(episode.identity)


def test_a_provisional_identity_from_unearned_authority_has_no_key_attribute():
    identity = episode_on(replay([fill(1)], exchange_positions={})).identity
    assert not hasattr(identity, "source_key")
    assert not hasattr(identity, "source_id")


# ============================================== 5, 6, 7: authority that is earned

def test_a_reconciled_open_position_is_authoritative():
    """(5) The exchange confirms the same net position."""
    result = replay([fill(1)], exchange_positions={SYNTH_TICKER: Decimal("10.00")})
    episode = episode_on(result)
    assert episode.authority is PositionAuthority.RECONCILED_CURRENT
    assert episode.is_importable is True
    assert isinstance(episode.identity, StableIdentity)
    assert result.claims_complete_position_state is True


def test_a_settlement_explained_closed_market_is_authoritative():
    """(6) An authoritative closure needs no current-position view at all."""
    result = replay([fill(1)], settlements=[settlement_row()])
    episode = episode_on(result)
    assert not episode.is_open
    assert episode.authority is PositionAuthority.EXPLAINED_SETTLED
    assert episode.is_importable is True
    assert result.claims_complete_position_state is True


def test_a_market_closed_to_flat_by_fills_alone_stays_proven():
    """(7) Both boundaries observed inside a complete history."""
    result = replay([fill(1), fill(2, action="sell")])
    episode = episode_on(result)
    assert not episode.is_open
    assert episode.authority is PositionAuthority.CLOSED_BY_FILLS
    assert episode.is_importable is True
    assert result.claims_complete_position_state is True


def test_a_closed_by_fills_episode_under_a_bounded_window_is_still_refused():
    # The two gates are independent: an earned position story does not rescue an
    # unprovable opening.
    result = replay([fill(1), fill(2, action="sell")], completeness=BOUNDED)
    episode = episode_on(result)
    assert episode.authority is PositionAuthority.CLOSED_BY_FILLS
    assert episode.provable is False
    assert episode.is_importable is False


# ================================== 8: one bad market does not condemn the others

def test_one_unexplained_market_does_not_revoke_authority_from_reconciled_ones():
    """(8) Global claim withheld; per-market evidence preserved."""
    result = replay(
        [fill(1, ticker=SYNTH_TICKER), fill(2, ticker=OTHER_TICKER)],
        exchange_positions={OTHER_TICKER: Decimal("10.00")},   # SYNTH absent
    )
    unexplained = episode_on(result, SYNTH_TICKER)
    reconciled = episode_on(result, OTHER_TICKER)

    assert unexplained.authority is PositionAuthority.UNEXPLAINED
    assert unexplained.is_importable is False

    assert reconciled.authority is PositionAuthority.RECONCILED_CURRENT
    assert reconciled.is_importable is True
    assert require_importable_identity(reconciled.identity) == reconciled.source_key

    # The account-level claim is still withheld, because part of the state is
    # unknown -- but the known part was not destroyed to say so.
    assert result.claims_complete_position_state is False


def test_a_settled_market_keeps_its_identity_beside_an_unexplained_one():
    result = replay(
        [fill(1, ticker=SYNTH_TICKER), fill(2, ticker=OTHER_TICKER)],
        exchange_positions={},
        settlements=[settlement_row(ticker=SYNTH_TICKER)],
    )
    assert episode_on(result, SYNTH_TICKER).is_importable is True
    assert episode_on(result, OTHER_TICKER).is_importable is False


# ==================================================== 9: conflict fails closed

def test_a_quantity_disagreement_is_a_conflict_and_fails_closed():
    """(9) The exchange reports a position, and it is not the computed one."""
    result = replay([fill(1)], exchange_positions={SYNTH_TICKER: Decimal("7.00")})
    episode = episode_on(result)
    assert episode.authority is PositionAuthority.CONFLICTED
    assert episode.is_importable is False
    assert result.claims_complete_position_state is False


def test_an_unreadable_exchange_quantity_is_a_conflict_not_an_absence():
    # Dropping the row would make the market look absent, and absent is how the
    # exchange reports a market it has SETTLED -- so a parse failure would read
    # as a contradiction nobody observed.
    view = exchange_position_view([{"ticker": SYNTH_TICKER, "position_fp": "not a number"}])
    assert view == {SYNTH_TICKER: None}
    episode = episode_on(replay([fill(1)], exchange_positions=view))
    assert episode.authority is PositionAuthority.CONFLICTED
    assert episode.is_importable is False


def test_a_refused_settlement_is_a_conflict_not_an_unasked_question():
    # The exchange says the market ended; the replay still holds it. That is a
    # disagreement with exchange truth even with no position view supplied.
    result = replay([fill(1)], settlements=[settlement_row(yes_count_fp="7.00")])
    episode = episode_on(result)
    assert result.settlements_refused_unreconciled == 1
    assert episode.authority is PositionAuthority.CONFLICTED
    assert episode.is_importable is False
    assert result.claims_complete_position_state is False


# ======================================== 10: the old field cannot be a bypass

def test_the_raw_completeness_field_cannot_be_read_as_authority():
    """(10) The ungated value exists, is named for what it is, and grants nothing."""
    result = replay([fill(1)], exchange_positions={})
    assert result.fill_history_complete is True
    assert result.claims_complete_position_state is False
    assert result.completeness is HistoryCompleteness.COMPLETE

    # Reading raw completeness cannot produce an identity, which is the only
    # thing a downstream importer can actually act on.
    for episode in result.episodes:
        assert episode.source_key is None
        with pytest.raises(IdentityNotImportable):
            require_importable_identity(episode.identity)


def test_no_diagnostics_field_exports_an_ungated_position_claim():
    data = build_diagnostics(replay([fill(1)], exchange_positions={})).as_dict()
    assert "position_state_is_authoritative" not in data   # the old second name
    assert "history_is_complete" not in data               # the old duplicate
    assert data["claims_complete_position_state"] is False


def test_an_unsupplied_exchange_view_is_not_a_successful_reconciliation():
    # Absence of a check is not the result of a check. Nothing was asked, so
    # nothing was earned.
    result = replay([fill(1)], exchange_positions=None)
    assert result.exchange_view_supplied is False
    assert episode_on(result).authority is PositionAuthority.NOT_RECONCILED
    assert episode_on(result).is_importable is False
    assert result.claims_complete_position_state is False


def test_the_authority_states_partition_every_episode():
    # Every episode lands in exactly one state, so no case can slip through
    # uncounted and read as though nothing went wrong.
    result = replay(
        [fill(1, ticker=SYNTH_TICKER), fill(2, ticker=OTHER_TICKER)],
        exchange_positions={OTHER_TICKER: Decimal("10.00")},
    )
    diagnostics = build_diagnostics(result)
    total = (
        diagnostics.episodes_closed_by_fills
        + diagnostics.episodes_explained_settled
        + diagnostics.episodes_reconciled_current
        + diagnostics.episodes_unexplained
        + diagnostics.episodes_conflicted
        + diagnostics.episodes_not_reconciled
    )
    assert total == diagnostics.episodes_observed == 2
