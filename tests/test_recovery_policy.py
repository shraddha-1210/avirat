"""Layer 4a/4b — risk scorecard and action mapping.

No database and no LLM: this is pure deterministic arithmetic plus a lookup
table, and it is tested as such. The money-movement seam (4c) is covered in
test_idempotency.py against real Postgres.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from config import settings
from layers.recovery_policy import (
    ALT_RAIL_COST_RUPEES,
    RiskScore,
    decide,
    map_diagnosis_to_action,
    score_recovery_risk,
)

# A deliberately calm event: reliable mandate, small amount, cycle far away.
_LOW_RISK = dict(days_to_next_cycle=28, mandate_reliability=0.97, amount=120)
# A deliberately alarming one: flaky mandate, large amount, cycle tomorrow.
_HIGH_RISK = dict(days_to_next_cycle=1, mandate_reliability=0.55, amount=4800)


def test_weights_sum_to_one():
    """If the weights drift off 1.0 the score is no longer on a [0,1] scale and
    the firing threshold silently changes meaning."""
    total = (
        settings.risk_weight_urgency
        + settings.risk_weight_reliability
        + settings.risk_weight_amount
        + settings.risk_weight_cost_benefit
    )
    assert total == pytest.approx(1.0)


def test_score_is_deterministic_and_reproducible():
    """Same inputs -> byte-identical score. No randomness, no model, no clock."""
    a = score_recovery_risk(**_HIGH_RISK)
    b = score_recovery_risk(**_HIGH_RISK)
    assert a == b


def test_score_is_hand_computable_from_its_components():
    """An auditor must be able to re-derive the total from the parts.

    This is the whole point of returning components: the score is defensible
    arithmetic, not an opaque number.
    """
    r = score_recovery_risk(days_to_next_cycle=15, mandate_reliability=0.8, amount=2500)
    expected = (
        settings.risk_weight_urgency * r.urgency
        + settings.risk_weight_reliability * r.unreliability
        + settings.risk_weight_amount * r.amount_tier
        + settings.risk_weight_cost_benefit * r.cost_benefit
    )
    assert r.score == pytest.approx(expected, abs=1e-4)
    assert r.urgency == pytest.approx(0.5)          # 1 - 15/30
    assert r.unreliability == pytest.approx(0.2)    # 1 - 0.8
    assert r.amount_tier == pytest.approx(0.5)      # 2500/5000


def test_components_are_clamped_to_unit_interval():
    """Out-of-range inputs must not push a component outside [0,1] and skew the
    weighted total — a 50,000-rupee amount is top tier, not tier 10."""
    r = score_recovery_risk(days_to_next_cycle=-5, mandate_reliability=1.5, amount=50_000)
    for value in (r.urgency, r.unreliability, r.amount_tier, r.cost_benefit):
        assert 0.0 <= value <= 1.0
    assert 0.0 <= r.score <= 1.0


def test_low_risk_routes_to_safe_hold_and_never_dispatches_alt_rail():
    """testing.md Phase 4 Test 1: low-risk -> SAFE_HOLD, alt-rail never invoked."""
    mock_alt_rail_dispatch = MagicMock()

    decision = decide(
        cause="mandate_revoked",  # an alt-rail-ELIGIBLE cause ...
        diagnosis_tier=2,
        diagnosis_status="resolved",
        **_LOW_RISK,              # ... that the scorecard still refuses to escalate
    )
    if decision.action == "ALT_RAIL":  # pragma: no cover - guarded below
        mock_alt_rail_dispatch()

    assert decision.action == "SAFE_HOLD"
    assert decision.risk.score < settings.risk_firing_threshold
    mock_alt_rail_dispatch.assert_not_called()


def test_high_risk_eligible_cause_fires_alt_rail():
    decision = decide(
        cause="mandate_revoked", diagnosis_tier=2, diagnosis_status="resolved", **_HIGH_RISK
    )
    assert decision.action == "ALT_RAIL"
    assert decision.risk.score >= settings.risk_firing_threshold
    assert decision.risk.cost_benefit_passed is True


def test_cost_benefit_gate_blocks_alt_rail_even_at_high_score():
    """The cost-benefit gate must veto the alt rail on its own, independent of score.

    Constructed directly rather than via `score_recovery_risk`, because under the
    current weights no real input reaches a passing score while failing
    cost-benefit — see `test_cost_benefit_gate_is_currently_unreachable`. This
    asserts the gate LOGIC, so it stays honest if the weights are retuned.
    """
    risk = RiskScore(
        score=0.99,                 # score gate would pass ...
        urgency=1.0,
        unreliability=1.0,
        amount_tier=0.002,
        cost_benefit=0.0,
        expected_loss=4.0,          # ... but ₹4 recovered cannot justify ₹12 spent
        alt_rail_cost=ALT_RAIL_COST_RUPEES,
        cost_benefit_passed=False,
    )
    decision = map_diagnosis_to_action(
        cause="mandate_revoked", diagnosis_tier=2, diagnosis_status="resolved", risk=risk
    )
    assert decision.action == "SAFE_HOLD"
    assert "cost-benefit" in decision.reason


def test_cost_benefit_gate_is_currently_unreachable():
    """Documents a real property of the CURRENT weights: the gate is redundant.

    `cost_benefit_passed` is False only when expected_loss <= ₹12, i.e. amount
    <= ₹30. But amount carries weight 0.4, so a ₹30 event tops out at a score of
    ~0.507 — under the 0.60 firing threshold. The score gate therefore always
    trips first and the AND never has to arbitrate.

    The gate is kept as defence in depth, not deleted. If the weights, the
    threshold, the alt-rail cost or the recovery-rate assumption are retuned,
    this test flips to failing and tells you the gate has become load-bearing
    and now needs its own live coverage.
    """
    worst = max(
        (
            score_recovery_risk(days_to_next_cycle=0, mandate_reliability=0.0, amount=amount)
            for amount in range(0, 31)
        ),
        key=lambda r: r.score,
    )
    assert worst.cost_benefit_passed is False
    assert worst.score < settings.risk_firing_threshold, (
        "the cost-benefit gate has become reachable — it now independently blocks "
        "alt-rail and needs a test driven by real inputs, not a constructed RiskScore"
    )


def test_maximum_urgency_alone_does_not_fire_alt_rail():
    """The explicit 'never on urgency alone' guarantee, isolated.

    Urgency is pinned at its maximum while every other signal is at its
    minimum; the weighted score must stay under the threshold.
    """
    risk = score_recovery_risk(days_to_next_cycle=0, mandate_reliability=1.0, amount=0)
    assert risk.urgency == pytest.approx(1.0)
    assert risk.score < settings.risk_firing_threshold

    decision = map_diagnosis_to_action(
        cause="mandate_revoked", diagnosis_tier=2, diagnosis_status="resolved", risk=risk
    )
    assert decision.action != "ALT_RAIL"


@pytest.mark.parametrize(
    "cause,expected",
    [
        ("bank_downtime", "RETRY"),
        ("technical_decline", "RETRY"),
        ("insufficient_funds", "NUDGE_BALANCE"),
        ("payer_limit_exceeded", "NUDGE_BALANCE"),
    ],
)
def test_non_alt_rail_causes_map_regardless_of_risk(cause, expected):
    """Retries and nudges are cheap and reversible, so they are not risk-gated."""
    for profile in (_LOW_RISK, _HIGH_RISK):
        decision = decide(
            cause=cause, diagnosis_tier=1, diagnosis_status="resolved", **profile
        )
        assert decision.action == expected


def test_retry_carries_the_configured_retry_budget():
    decision = decide(
        cause="bank_downtime", diagnosis_tier=1, diagnosis_status="resolved", **_HIGH_RISK
    )
    assert decision.action == "RETRY"
    assert decision.max_retries == settings.max_retries


def test_quarantined_diagnosis_never_gets_an_automated_money_action():
    """Tier 3 means we do not know why it failed. A person decides, not the policy."""
    decision = decide(
        cause=None, diagnosis_tier=3, diagnosis_status="QUARANTINE", **_HIGH_RISK
    )
    assert decision.action == "MANUAL_REVIEW"
    assert decision.max_retries == 0


def test_unmapped_cause_falls_to_manual_review_not_a_guess():
    decision = decide(
        cause="some_future_cause", diagnosis_tier=2, diagnosis_status="resolved", **_HIGH_RISK
    )
    assert decision.action == "MANUAL_REVIEW"
    assert "no mapped action" in decision.reason


def test_decision_serialises_with_full_audit_trail():
    """Every decision must be reconstructible from its own record."""
    d = decide(
        cause="mandate_revoked", diagnosis_tier=2, diagnosis_status="resolved", **_HIGH_RISK
    ).to_dict()
    assert d["action"] == "ALT_RAIL"
    assert d["risk"]["components"]["firing_threshold"] == settings.risk_firing_threshold
    for key in ("urgency", "unreliability", "amount_tier", "cost_benefit", "expected_loss"):
        assert key in d["risk"]
    assert d["reason"]
