"""The production filter: which orders are delivered, and why the rest are not.

Every gate here is a place where letting one order through would put a wrong or
duplicate canonical wager into a public ledger. A refusal is the normal outcome
and never an error -- what it must never be is silent.
"""

from __future__ import annotations

from decimal import Decimal

from kalshi_router.accounting.execution import OrderExecution
from kalshi_router.models import OutcomeSide
from kalshi_router.production import (
    OrderFinality,
    ProductionRefusal,
    evaluate_order,
    evaluate_production,
    production_cutover_seconds,
    production_source_key,
)

MLB = frozenset({"MLB"})
AFTER = int(production_cutover_seconds()) + 3600
BEFORE = int(production_cutover_seconds()) - 3600
NOW = Decimal(AFTER + 10_000)


def order(
    order_id="ORD-1",
    ticker="KXMLBGAME-26SEP20SFLAD-SF",
    when=AFTER,
    price="0.56",
    fee="0.01",
    side=OutcomeSide.YES,
    quantity=10,
):
    return OrderExecution(
        order_id=order_id,
        ticker=ticker,
        outcome_side=side,
        book_side=None,
        subaccount_number=0,
        total_quantity=Decimal(quantity),
        fill_count=1,
        fill_ids=("F1",),
        first_execution_time=Decimal(when),
        last_execution_time=Decimal(when),
        vwap_price=None if price is None else Decimal(price),
        total_fee=None if fee is None else Decimal(fee),
        fills_with_fee=1,
    )


def run(o, sport="MLB", date="2026-09-20", status="settled", destinations=MLB):
    return evaluate_order(o, sport, date, status, NOW, destinations)


# ------------------------------------------------------------- the happy path

def test_an_eligible_order_becomes_a_deliverable_wager():
    wager, refusal, finality = run(order())
    assert refusal is None
    assert finality is OrderFinality.FINAL_MARKET_CLOSED
    assert wager.sport == "MLB"
    assert wager.side == "YES"
    assert wager.contracts == Decimal(10)
    assert wager.vwap_price == Decimal("0.56")
    # stake = contract cost + exchange fees, both exchange-stated.
    assert wager.stake == Decimal("0.56") * 10 + Decimal("0.01")
    assert wager.source_key == production_source_key(
        0, "KXMLBGAME-26SEP20SFLAD-SF", "ORD-1"
    )


def test_a_no_side_order_records_the_contract_price_not_the_yes_axis():
    # The ledger is denominated on the signed YES axis; the destination records
    # what was paid for the contract actually held.
    wager, refusal, _ = run(order(side=OutcomeSide.NO, price="0.56"))
    assert refusal is None
    assert wager.side == "NO"
    assert wager.vwap_price == Decimal("0.44")
    assert wager.stake == Decimal("0.44") * 10 + Decimal("0.01")


# ----------------------------------------------------------------- the gates

def test_a_pre_cutover_order_is_refused_first_and_reports_no_finality():
    """History is not a failure of any later gate.

    Checking the cutover first is free and it rejects almost everything. If a
    later gate reported on historical orders too, the history would look broken.
    """
    wager, refusal, finality = run(order(when=BEFORE), sport=None, date=None, status=None)
    assert refusal is ProductionRefusal.BEFORE_CUTOVER
    assert wager is None
    assert finality is None


def test_an_order_on_an_open_market_is_not_final_and_is_refused():
    _w, refusal, finality = run(order(), status="active")
    assert refusal is ProductionRefusal.ORDER_NOT_FINAL
    assert finality is OrderFinality.UNKNOWN


def test_an_order_with_no_price_is_refused_rather_than_averaged():
    _w, refusal, _f = run(order(price=None))
    assert refusal is ProductionRefusal.NO_EXECUTION_PRICE


def test_an_order_with_an_incomplete_fee_is_refused_rather_than_reconstructed():
    _w, refusal, _f = run(order(fee=None))
    assert refusal is ProductionRefusal.FEES_INCOMPLETE


def test_an_unclassified_market_is_distinct_from_an_unresolved_one():
    # "Never attempted" and "attempted and could not tell" are different
    # problems with different fixes.
    assert run(order(), sport=None)[1] is ProductionRefusal.MARKET_NOT_CLASSIFIED
    assert run(order(), sport="UNRESOLVED")[1] is ProductionRefusal.SPORT_UNRESOLVED


def test_other_is_refused_as_firmly_as_unresolved():
    # OTHER means positively not a sport this router carries. Never route it.
    assert run(order(), sport="OTHER")[1] is ProductionRefusal.SPORT_UNRESOLVED


def test_a_sport_with_no_destination_is_refused():
    """A sport this router cannot speak is refused by design; a sport it CAN
    speak (a row shape exists) but that is not routed here is a gap, refused
    under a reason that needs attention -- never counted as correct behaviour,
    which is how 2026 week 2's NFL wagers were lost without a red run."""
    assert run(order(), sport="TENNIS")[1] is ProductionRefusal.NO_DESTINATION_IMPORTER
    for sport in ("NFL", "CFB"):
        assert run(order(), sport=sport)[1] is ProductionRefusal.DESTINATION_NOT_ACTIVATED


def test_an_unestablished_game_date_is_refused():
    assert run(order(), date=None)[1] is ProductionRefusal.GAME_DATE_NOT_ESTABLISHED
    assert run(order(), date="")[1] is ProductionRefusal.GAME_DATE_NOT_ESTABLISHED


# ------------------------------------------------------------- the aggregate

def test_every_order_is_either_eligible_or_refused_exactly_once():
    orders = [
        order("A", when=AFTER),
        order("B", when=BEFORE),
        order("C", when=AFTER, price=None),
    ]
    wagers, d = evaluate_production(
        orders,
        {"KXMLBGAME-26SEP20SFLAD-SF": "MLB"},
        {"KXMLBGAME-26SEP20SFLAD-SF": "2026-09-20"},
        {"KXMLBGAME-26SEP20SFLAD-SF": "settled"},
        NOW,
        MLB,
    )
    assert d.orders_considered == 3
    assert d.eligible + d.refusals_total == 3
    assert len(wagers) == d.eligible == 1


def test_only_post_cutover_orders_are_counted_as_after_cutover():
    orders = [order("A", when=AFTER), order("B", when=BEFORE)]
    _w, d = evaluate_production(orders, {}, {}, {}, NOW, MLB)
    assert d.orders_after_cutover == 1
    assert d.refused_before_cutover == 1


def test_no_post_cutover_order_is_a_healthy_no_op():
    _w, d = evaluate_production([order("A", when=BEFORE)], {}, {}, {}, NOW, MLB)
    assert d.is_healthy_no_op
    assert d.eligible == 0


def test_a_deferred_post_cutover_order_is_not_a_healthy_no_op():
    """A gate holding something back is the system working -- but it is not
    nothing, and a health signal that called it nothing would hide it."""
    _w, d = evaluate_production(
        [order("A", when=AFTER)], {}, {}, {"KXMLBGAME-26SEP20SFLAD-SF": "active"}, NOW, MLB
    )
    assert not d.is_healthy_no_op
    assert d.refused_order_not_final == 1


def test_the_diagnostics_hold_counts_only():
    _w, d = evaluate_production([order("A")], {}, {}, {}, NOW, MLB)
    for name, value in d.as_dict().items():
        assert isinstance(value, int), f"{name} is not a count"


def test_the_rendered_output_names_no_ticker_key_amount_or_date():
    _w, d = evaluate_production(
        [order("SECRETORDER", ticker="KXSECRET")],
        {"KXSECRET": "MLB"},
        {"KXSECRET": "2026-09-20"},
        {"KXSECRET": "settled"},
        NOW,
        MLB,
    )
    text = d.render()
    for token in ("SECRETORDER", "KXSECRET", "2026-09-20", "0.56", "kalshi:v1:", "$"):
        assert token not in text


# ---------------------------------------------------------------------------
# Phase 12: pre-cutover recovery. Off at every layer unless explicitly asked.
# ---------------------------------------------------------------------------


def test_include_pre_cutover_defaults_to_false_at_every_layer():
    """A caller that has never heard of this flag must get the safe behaviour.

    Discovered from the signatures rather than restated, so a new function in
    the chain that defaults it to True fails here.
    """
    import inspect

    from kalshi_router import audit, production

    for func in (
        production.evaluate_order,
        production.evaluate_production,
        audit.run_audit,
        audit._evaluate_production,
    ):
        parameter = inspect.signature(func).parameters["include_pre_cutover"]
        assert parameter.default is False, f"{func.__name__} defaults to {parameter.default!r}"


def test_every_unresolved_reason_has_a_counter():
    """A reason with no counter would silently vanish into 'reason unavailable'.

    Enumerated from the classifier's own enum, so a reason added there fails
    here rather than being quietly uncounted.
    """
    from kalshi_router.classify import UnresolvedReason
    from kalshi_router.production import UNRESOLVED_COUNTERS, ProductionDiagnostics

    diagnostics = ProductionDiagnostics()
    for reason in UnresolvedReason:
        assert reason.value in UNRESOLVED_COUNTERS, reason
        assert hasattr(diagnostics, UNRESOLVED_COUNTERS[reason.value])


def test_the_unresolved_counters_are_all_distinct():
    """Two reasons sharing a counter would double-count one and hide the other."""
    from kalshi_router.production import UNRESOLVED_COUNTERS

    assert len(set(UNRESOLVED_COUNTERS.values())) == len(UNRESOLVED_COUNTERS)


def test_the_diagnostics_stay_structurally_counts_only():
    """Every field an int, including the new ones. Checked, not assumed."""
    from kalshi_router.production import ProductionDiagnostics

    for name, value in ProductionDiagnostics().as_dict().items():
        assert isinstance(value, int), f"{name} is {type(value).__name__}"


def test_a_pre_cutover_order_is_refused_by_default():
    wager, refusal, finality = evaluate_order(
        order(when=BEFORE), "MLB", "2026-09-01", "settled", NOW, MLB
    )
    assert wager is None
    assert refusal is ProductionRefusal.BEFORE_CUTOVER
    # No finality verdict: the gate ran before finality was even assessed.
    assert finality is None


def test_a_recovery_run_admits_a_pre_cutover_order_and_says_it_did():
    wagers, diagnostics = evaluate_production(
        [order(when=BEFORE)],
        {"KXMLBGAME-26SEP20SFLAD-SF": "MLB"},
        {"KXMLBGAME-26SEP20SFLAD-SF": "2026-09-01"},
        {"KXMLBGAME-26SEP20SFLAD-SF": "settled"},
        NOW,
        MLB,
        include_pre_cutover=True,
    )

    assert len(wagers) == 1
    assert diagnostics.eligible == 1
    assert diagnostics.refused_before_cutover == 0
    # The report must not let history pass as new activity.
    assert diagnostics.pre_cutover_admitted == 1


def test_a_normal_run_never_admits_pre_cutover_history():
    _wagers, diagnostics = evaluate_production(
        [order(when=BEFORE)],
        {"KXMLBGAME-26SEP20SFLAD-SF": "MLB"},
        {"KXMLBGAME-26SEP20SFLAD-SF": "2026-09-01"},
        {"KXMLBGAME-26SEP20SFLAD-SF": "settled"},
        NOW,
        MLB,
    )

    assert diagnostics.pre_cutover_admitted == 0
    assert diagnostics.refused_before_cutover == 1
    assert diagnostics.eligible == 0


def test_a_recovery_run_still_applies_every_other_gate():
    """Admitting history is not a bypass. An unresolved sport still refuses."""
    _wagers, diagnostics = evaluate_production(
        [order(when=BEFORE)],
        {"KXMLBGAME-26SEP20SFLAD-SF": "UNRESOLVED"},
        {"KXMLBGAME-26SEP20SFLAD-SF": "2026-09-01"},
        {"KXMLBGAME-26SEP20SFLAD-SF": "settled"},
        NOW,
        MLB,
        include_pre_cutover=True,
    )

    assert diagnostics.eligible == 0
    assert diagnostics.refused_sport_unresolved == 1
    assert diagnostics.pre_cutover_admitted == 1


def test_the_source_key_of_a_recovered_order_is_unchanged_by_the_flag():
    """Recovery must land on the SAME canonical bet id as a normal delivery.

    If the key moved, a recovery run would duplicate every wager it re-sent
    instead of being answered DUPLICATE_NOOP.
    """
    normal, _r, _f = evaluate_order(
        order(when=AFTER), "MLB", "2026-09-20", "settled", NOW, MLB
    )
    recovered, _r2, _f2 = evaluate_order(
        order(when=AFTER), "MLB", "2026-09-20", "settled", NOW, MLB,
        include_pre_cutover=True,
    )
    assert normal is not None and recovered is not None
    assert normal.source_key == recovered.source_key


# ---------------------------------------------------------------------------
# Phase 13: the health signal.
#
# "HEALTHY NO-OP: False" reads like bad news whether the cause is a wager
# pending settlement or a wager that can never be recorded. Those call for
# opposite responses -- wait, and act -- so they are different states.
# ---------------------------------------------------------------------------


def test_every_refusal_is_classified_exactly_once():
    """A refusal in no bucket would be invisible to the health signal; one in
    two would be counted twice."""
    from kalshi_router.production import (
        BY_DESIGN_REFUSALS,
        NEEDS_ATTENTION_REFUSALS,
        SELF_RESOLVING_REFUSALS,
        ProductionRefusal,
    )

    buckets = [SELF_RESOLVING_REFUSALS, BY_DESIGN_REFUSALS, NEEDS_ATTENTION_REFUSALS]
    union = set().union(*buckets)
    assert union == set(ProductionRefusal)
    assert sum(len(b) for b in buckets) == len(union), "a refusal is in two buckets"


def test_a_new_refusal_would_default_to_needing_attention():
    """NEEDS_ATTENTION is derived by subtraction, not listed.

    So a refusal added later is treated as needing a human until someone
    deliberately says otherwise -- the safe direction for a list whose job is
    to decide what gets ignored.
    """
    from kalshi_router.production import (
        BY_DESIGN_REFUSALS,
        NEEDS_ATTENTION_REFUSALS,
        ProductionRefusal,
        SELF_RESOLVING_REFUSALS,
    )

    explicitly_excused = SELF_RESOLVING_REFUSALS | BY_DESIGN_REFUSALS
    for refusal in ProductionRefusal:
        if refusal not in explicitly_excused:
            assert refusal in NEEDS_ATTENTION_REFUSALS, refusal


def _diagnostics(**kwargs):
    from kalshi_router.production import ProductionDiagnostics

    return ProductionDiagnostics(**kwargs)


def test_a_quiet_account_is_a_healthy_no_op():
    from kalshi_router.production import HealthState

    assert _diagnostics(orders_considered=1755, refused_before_cutover=1755).health is (
        HealthState.HEALTHY_NO_OP
    )


def test_an_order_waiting_to_finalise_is_deferred_not_blocked():
    from kalshi_router.production import HealthState

    health = _diagnostics(orders_after_cutover=1, refused_order_not_final=1).health
    assert health is HealthState.DEFERRED


def test_an_unclassifiable_market_blocks_because_waiting_will_not_help():
    from kalshi_router.production import HealthState

    health = _diagnostics(orders_after_cutover=1, refused_sport_unresolved=1).health
    assert health is HealthState.BLOCKED


def test_a_sport_with_no_importer_is_not_routable_rather_than_broken():
    """The other three repositories mechanically refuse to hold a wager. That
    is their decision, not this system's failure."""
    from kalshi_router.production import HealthState

    health = _diagnostics(
        orders_after_cutover=1, refused_no_destination_importer=1
    ).health
    assert health is HealthState.NOT_ROUTABLE


def test_a_delivered_wager_reports_delivered():
    from kalshi_router.production import HealthState

    assert _diagnostics(orders_after_cutover=1, eligible=1).health is HealthState.DELIVERED


def test_blocked_outranks_delivered_because_only_one_of_them_needs_anyone():
    """A run that delivered one wager and cannot record another is BLOCKED."""
    from kalshi_router.production import HealthState

    health = _diagnostics(
        orders_after_cutover=2, eligible=1, refused_fees_incomplete=1
    ).health
    assert health is HealthState.BLOCKED


def test_blocked_outranks_deferred():
    from kalshi_router.production import HealthState

    health = _diagnostics(
        orders_after_cutover=2, refused_order_not_final=1, refused_sport_unresolved=1
    ).health
    assert health is HealthState.BLOCKED


def test_pre_cutover_refusals_never_count_as_blocked():
    """1755 historical orders must not read as 1755 problems."""
    assert _diagnostics(refused_before_cutover=1755).blocked_orders == 0
    assert _diagnostics(refused_before_cutover=1755).deferred_orders == 0


def test_the_health_state_is_rendered():
    rendered = _diagnostics(orders_after_cutover=1, refused_sport_unresolved=1).render()
    assert "HEALTH: blocked" in rendered
