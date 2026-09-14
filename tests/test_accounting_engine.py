"""Replay determinism, idempotency, identity stability and history refusal."""

from __future__ import annotations

import random
from decimal import Decimal

from kalshi_router.accounting import AccountingEngine, HistoryCompleteness
from kalshi_router.accounting.identity import (
    digest_for,
    episode_source_key,
    fill_source_key,
    order_source_key,
)
from kalshi_router.models import normalize_fill

from .synthetic import SYNTH_TICKER, make_accounting_fill

COMPLETE = HistoryCompleteness.COMPLETE
BOUNDED = HistoryCompleteness.BOUNDED_WINDOW

SCENARIO = [
    {"index": 1, "quantity": "50.00", "yes_price": "0.5000", "order_id": "O1"},
    {"index": 2, "quantity": "50.00", "yes_price": "0.6000", "order_id": "O1"},
    {"index": 3, "quantity": "30.00", "yes_price": "0.7000", "order_id": "O2",
     "action": "sell"},
    {"index": 4, "quantity": "70.00", "yes_price": "0.6500", "order_id": "O3",
     "action": "sell"},
    {"index": 5, "quantity": "25.00", "yes_price": "0.4000", "order_id": "O4"},
]


def build(specs=None):
    return [normalize_fill(make_accounting_fill(**s)) for s in (specs or SCENARIO)]


def fingerprint(result):
    """A comparable summary of everything the replay concluded."""
    return (
        result.fills_replayed,
        result.duplicate_fills_ignored,
        tuple(sorted((t, l.position) for t, l in result.ledgers.items())),
        tuple(t.kind.value for t in result.transitions),
        tuple(t.fill_id for t in result.transitions),
        tuple(sorted(
            (e.source_id, e.remaining_quantity, e.realized_pnl, e.is_open)
            for e in result.episodes
        )),
        tuple(sorted((o.order_id, o.total_quantity, o.vwap_price)
                     for o in result.orders.values())),
    )


# ------------------------------------------------------------- determinism

def test_replaying_the_same_fills_twice_gives_an_identical_result():
    engine = AccountingEngine()
    assert fingerprint(engine.replay(build(), COMPLETE)) == fingerprint(
        engine.replay(build(), COMPLETE)
    )


def test_shuffled_input_produces_identical_state():
    expected = fingerprint(AccountingEngine().replay(build(), COMPLETE))
    for seed in range(25):
        shuffled = build()
        random.Random(seed).shuffle(shuffled)
        assert fingerprint(AccountingEngine().replay(shuffled, COMPLETE)) == expected


def test_one_engine_instance_carries_no_state_between_replays():
    engine = AccountingEngine()
    first = engine.replay(build(), COMPLETE)
    engine.replay(build(SCENARIO[:2]), COMPLETE)
    assert fingerprint(engine.replay(build(), COMPLETE)) == fingerprint(first)


# -------------------------------------------------------------- idempotency

def test_duplicate_fills_do_not_change_state():
    plain = AccountingEngine().replay(build(), COMPLETE)
    doubled = AccountingEngine().replay(build() + build(), COMPLETE)
    assert doubled.duplicate_fills_ignored == len(SCENARIO)
    assert fingerprint(doubled)[2:] == fingerprint(plain)[2:]
    assert doubled.ledgers[SYNTH_TICKER].position == plain.ledgers[SYNTH_TICKER].position


def test_incremental_replay_matches_full_replay():
    """Replaying A then A+B equals replaying A+B once."""
    engine = AccountingEngine()
    engine.replay(build(SCENARIO[:3]), COMPLETE)
    incremental = engine.replay(build(), COMPLETE)
    assert fingerprint(incremental) == fingerprint(AccountingEngine().replay(build(), COMPLETE))


def test_replaying_a_superset_extends_rather_than_restarts_identities():
    partial = AccountingEngine().replay(build(SCENARIO[:2]), COMPLETE)
    full = AccountingEngine().replay(build(), COMPLETE)
    opening = partial.episodes[0].source_id
    assert opening in {e.source_id for e in full.episodes}


# ------------------------------------------------------- stable identities

def test_identities_derive_only_from_immutable_exchange_evidence():
    assert fill_source_key("F1") == "kalshi:fill:F1"
    assert order_source_key("O1") == "kalshi:order:O1"
    assert episode_source_key("KXA-1", "F1") == "kalshi:episode:KXA-1:F1"


def test_identity_digests_are_stable_across_runs():
    assert digest_for("kalshi:order:O1") == digest_for("kalshi:order:O1")
    assert digest_for("kalshi:order:O1") != digest_for("kalshi:order:O2")


def test_episode_identity_is_keyed_on_the_opening_fill():
    result = AccountingEngine().replay(build(), COMPLETE)
    episode = result.episodes[0]
    assert episode.source_key == episode_source_key(SYNTH_TICKER, episode.opening_fill_id)
    assert episode.source_id == digest_for(episode.source_key)


def test_episode_identity_survives_reordered_input():
    straight = AccountingEngine().replay(build(), COMPLETE)
    shuffled_fills = build()
    random.Random(7).shuffle(shuffled_fills)
    shuffled = AccountingEngine().replay(shuffled_fills, COMPLETE)
    assert {e.source_id for e in straight.episodes} == {e.source_id for e in shuffled.episodes}


def test_order_identity_matches_the_exchange_order_id():
    result = AccountingEngine().replay(build(), COMPLETE)
    order = result.orders["O1"]
    assert order.source_key == "kalshi:order:O1"
    assert order.source_id == digest_for("kalshi:order:O1")


# --------------------------------------------------- history completeness

def test_bounded_history_refuses_to_claim_position_state():
    result = AccountingEngine().replay(build(), BOUNDED)
    assert result.claims_complete_position_state is False
    assert all(e.provable is False for e in result.episodes)


def test_complete_history_claims_position_state():
    result = AccountingEngine().replay(build(), COMPLETE)
    assert result.claims_complete_position_state is True
    assert all(e.provable is True for e in result.episodes)


def test_completeness_defaults_to_bounded_so_the_safe_answer_is_the_default():
    assert AccountingEngine().replay(build()).claims_complete_position_state is False


def test_completeness_does_not_change_the_computed_arithmetic():
    """Only the claim changes, never the numbers."""
    bounded = AccountingEngine().replay(build(), BOUNDED)
    complete = AccountingEngine().replay(build(), COMPLETE)
    assert bounded.ledgers[SYNTH_TICKER].position == complete.ledgers[SYNTH_TICKER].position


# ----------------------------------------------------------- invariants

def test_invariant_order_quantities_sum_to_their_fills():
    result = AccountingEngine().replay(build(), COMPLETE)
    for order in result.orders.values():
        assert order.total_quantity == sum(
            (f.count for f in build() if f.fill_id in order.fill_ids), Decimal(0)
        )


def test_invariant_closed_never_exceeds_opened_within_an_episode():
    result = AccountingEngine().replay(build(), COMPLETE)
    for episode in result.episodes:
        assert episode.total_closed_quantity <= episode.total_opened_quantity


def test_invariant_remaining_equals_opened_minus_closed():
    result = AccountingEngine().replay(build(), COMPLETE)
    for episode in result.episodes:
        assert episode.remaining_quantity == (
            episode.total_opened_quantity - episode.total_closed_quantity
        )


def test_invariant_net_position_equals_sum_of_signed_transitions():
    result = AccountingEngine().replay(build(), COMPLETE)
    expected = sum((t.signed_quantity for t in result.transitions), Decimal(0))
    assert result.ledgers[SYNTH_TICKER].position == expected


def test_invariant_decimal_arithmetic_stays_exact():
    fills = build([
        {"index": 1, "quantity": "0.10", "yes_price": "0.1000"},
        {"index": 2, "quantity": "0.20", "yes_price": "0.1000"},
    ])
    result = AccountingEngine().replay(fills, COMPLETE)
    assert result.ledgers[SYNTH_TICKER].position == Decimal("0.30")
    assert result.ledgers[SYNTH_TICKER].position == Decimal("0.1") + Decimal("0.2")
